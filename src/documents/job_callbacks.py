"""Prevent repeated delivery from repeating concrete ingestion callback effects."""

import json
from collections.abc import Callable
from datetime import date
from datetime import time
from typing import Any
from uuid import UUID
from uuid import uuid4

from celery.exceptions import Ignore
from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.utils import timezone
from filelock import FileLock
from filelock import Timeout
from kombu.utils.json import dumps as transport_dumps
from kombu.utils.json import loads as transport_loads

from documents.job_context import active_ingestion_job
from documents.jobs import job_directory
from documents.models import Document
from documents.models import DocumentJob


def queue_ingestion_followup(
    task, *, args: tuple = (), kwargs: dict | None = None
) -> bool:
    """Record a workflow task in its ingestion transaction instead of dispatching."""
    identifier = active_ingestion_job.get()
    if identifier is None:
        return False
    job = DocumentJob.objects.get(pk=identifier)
    followups = job.result.setdefault("workflow_followups", [])
    followups.append(
        json.loads(
            transport_dumps(
                {
                    "id": str(uuid4()),
                    "task": task.name,
                    "args": args,
                    "kwargs": kwargs or {},
                }
            )
        )
    )
    job.save(update_fields=["result"])
    return True


def finish_ingestion_followups(job: DocumentJob) -> None:
    """Complete recorded AI/webhook actions without repeating ambiguous effects."""
    from documents.ingestion_jobs import NeedsAttention
    from documents.tasks import apply_ai_suggestions
    from documents.workflows.webhooks import send_webhook
    from paperless_ai.exceptions import LLMTimeoutError

    tasks = {task.name: task for task in (apply_ai_suggestions, send_webhook)}
    token = active_ingestion_job.set(str(job.pk))
    try:
        for stored in job.result.get("workflow_followups", []):
            followup = transport_loads(json.dumps(stored))
            task = tasks[followup["task"]]
            execute = getattr(task, "_orig_run", task.run)
            try:
                run_callback(
                    followup["id"],
                    followup["task"],
                    tuple(followup["args"]),
                    followup["kwargs"],
                    lambda: execute(*followup["args"], **followup["kwargs"]),
                )
            except LLMTimeoutError:
                # Built-in AI raises this before creating objects or applying fields.
                callback = DocumentJob.objects.get(pk=followup["id"])
                if task is apply_ai_suggestions and callback.attempts <= 3:
                    callback.status = DocumentJob.Status.QUEUED
                    callback.save(update_fields=["status"])
                    raise
                raise NeedsAttention(
                    "Workflow AI exhausted its safe timeout retries"
                ) from None
            except Exception as error:
                raise NeedsAttention(
                    f"Workflow follow-up {followup['id']} requires reconciliation"
                ) from error
    finally:
        active_ingestion_job.reset(token)


def deletion_revision(document: Document) -> dict:
    """Snapshot every persisted document value that deleting this source would lose."""

    def fields(instance: Document) -> dict:
        """Read concrete values without database-generated cache columns."""
        return {
            field.attname: getattr(instance, field.attname)
            for field in Document._meta.concrete_fields
            if not field.generated
        }

    def encode(value: object) -> object:
        """Keep full timestamp precision while normalizing UUID and decimal fields."""
        if isinstance(value, (date, time)):
            return value.isoformat()
        return DjangoJSONEncoder().default(value)

    snapshot = {
        "fields": fields(document),
        "tags": list(document.tags.order_by("pk").values_list("pk", flat=True)),
        "custom_fields": list(document.custom_fields.order_by("pk").values()),
        "notes": list(document.notes.order_by("pk").values()),
        "barcodes": list(document.barcodes.order_by("pk").values()),
        "versions": [fields(version) for version in document.versions.order_by("pk")],
    }
    return json.loads(json.dumps(snapshot, default=encode))


def run_callback(
    task_id: str | None,
    task_name: str,
    args: tuple,
    kwargs: dict,
    callback: Callable[[], Any],
) -> Any:
    """Run an identified effect once, retaining ambiguous outcomes for inspection."""
    identifier = UUID(task_id) if task_id else uuid4()
    payload = json.loads(
        transport_dumps({"task": task_name, "args": args, "kwargs": kwargs})
    )
    job, _ = DocumentJob.objects.get_or_create(
        pk=identifier,
        defaults={
            "kind": "callback",
            "timeout": settings.CELERY_TASK_TIME_LIMIT,
            "input": payload,
        },
    )
    if job.kind != "callback" or job.input != payload:
        raise ValueError("Callback identifier belongs to a different operation")
    lock = FileLock(job_directory(identifier).with_suffix(".lock"), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        raise Ignore from None
    try:
        job.refresh_from_db()
        if job.status == DocumentJob.Status.SUCCEEDED:
            return transport_loads(json.dumps(job.result["payload"]))
        if job.status != DocumentJob.Status.QUEUED:
            job.status = DocumentJob.Status.ATTENTION
            job.error = "Callback may already have run; reconcile its external effects before recovery"
            job.updated = timezone.now()
            job.save(update_fields=["status", "error", "updated"])
            raise Ignore
        job.status = DocumentJob.Status.RUNNING
        job.attempts += 1
        job.updated = timezone.now()
        job.save(update_fields=["status", "attempts", "updated"])
        try:
            result = callback()
            job.result = {"payload": json.loads(transport_dumps(result))}
            job.status = DocumentJob.Status.SUCCEEDED
            job.cleaned = True
            job.error = ""
            job.updated = timezone.now()
            job.save(update_fields=["result", "status", "cleaned", "error", "updated"])
            return result
        except Exception as error:
            job.refresh_from_db()
            if job.status != DocumentJob.Status.SUCCEEDED:
                job.status = DocumentJob.Status.ATTENTION
                job.error = str(error)
                job.updated = timezone.now()
                job.save(update_fields=["status", "error", "updated"])
            raise
    finally:
        lock.release()
