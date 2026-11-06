"""Durable finite search maintenance, separate from interactive queries."""

import logging
from uuid import uuid4

from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from filelock import FileLock
from filelock import Timeout

from documents.job_state import PendingOperation
from documents.job_state import track_terminal_task
from documents.models import DocumentJob
from documents.operation_journal import NeedsAttention
from documents.operation_journal import invoke
from documents.operation_journal import json_value
from documents.operation_journal import operation_failure_status
from documents.processor_notifications import defer_notifications


def provider_snapshot(provider) -> dict:
    """Retain provider-declared configuration without recognizing implementations."""
    return json_value(
        {
            "implementation": settings.SEARCH_PROVIDER,
            "identity": getattr(provider, "identity", settings.SEARCH_PROVIDER),
            "configuration": getattr(provider, "configuration", {}),
        }
    )


def submit_search(provider, operation: str, inputs: dict) -> DocumentJob:
    """Persist finite maintenance before dispatch, including its immutable inputs."""
    from documents.classification_operations import snapshot_document
    from documents.models import Document

    inputs = dict(inputs)
    if operation == "rebuild":
        inputs["root_ids"] = list(
            Document.objects.filter(root_document__isnull=True)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
    if operation == "write":
        inputs["removed_revisions"] = {
            str(pk): snapshot_document(document)
            if (document := Document.objects.filter(pk=pk).first())
            else None
            for pk in inputs["remove"]
        }
    job = DocumentJob.objects.create(
        id=uuid4(),
        kind="search",
        timeout=settings.CELERY_TASK_TIME_LIMIT,
        input=json_value({"operation": operation, "actor_id": None, **inputs}),
        configuration=provider_snapshot(provider),
    )

    def dispatch() -> None:
        """Leave broker failures recoverable from the journal."""
        try:
            run_search.apply_async(
                args=[str(job.pk)], task_id=str(job.pk), time_limit=job.timeout
            )
        except Exception:
            logging.getLogger("paperless.jobs").exception("Search dispatch failed")

    transaction.on_commit(dispatch)
    return job


def validate_inputs(job: DocumentJob) -> None:
    """Reject changed content, ownership, grants and deleted or superseded rows."""
    from documents.classification_operations import snapshot_document
    from documents.models import Document
    from documents.search._persistence import documents_for_ids
    from documents.search._persistence import index_records

    if (
        "root_ids" in job.input
        and list(
            Document.objects.filter(root_document__isnull=True)
            .order_by("pk")
            .values_list("pk", flat=True)
        )
        != job.input["root_ids"]
    ):
        raise NeedsAttention("Search rebuild scope changed while pending")
    for pk, revision in job.input.get("removed_revisions", {}).items():
        document = Document.objects.filter(pk=pk).first()
        if (snapshot_document(document) if document else None) != revision:
            raise NeedsAttention("Search removal target changed while pending")
    expected = job.input.get("records", [])
    ids = [record["id"][0] for record in expected]
    current = json_value(list(index_records(documents_for_ids(ids))))

    def by_id(records: list[dict]) -> dict:
        """Compare immutable records independently of query ordering."""
        return {record["id"][0]: record for record in records}

    if by_id(current) != by_id(expected):
        raise NeedsAttention("Search input changed while maintenance was pending")


def _restore_record(record: dict) -> dict:
    """Restore the date values declared by the search record contract."""
    from datetime import datetime

    record = dict(record)
    for field in ("created", "added", "modified"):
        if field in record:
            record[field] = [datetime.fromisoformat(value) for value in record[field]]
    return record


def _execute(provider, operation: str, inputs: dict) -> None:
    """Run the provider's existing finite contract locally."""
    if operation == "write":
        with provider.batch_update() as batch:
            for record in inputs["records"]:
                batch.add_or_update(_restore_record(record))
            for document_id in inputs["remove"]:
                batch.remove(document_id)
    elif operation == "rebuild":
        provider.rebuild(
            (_restore_record(record) for record in inputs["records"]),
            writer_heap_bytes=inputs["writer_heap_bytes"],
        )
    elif operation == "optimize":
        provider.optimize()
    elif operation == "wipe":
        provider.wipe()
    else:
        raise ValueError("Unknown search operation")


@shared_task(ignore_result=True)
@defer_notifications()
@track_terminal_task
def run_search(job_id: str) -> None:
    """Release workers while pending and validate declarations on completion."""
    from documents.job_storage import job_directory
    from documents.search import get_backend

    try:
        with FileLock(
            job_directory(job_id).with_suffix(".lock"), timeout=0, is_singleton=True
        ):
            job = DocumentJob.objects.get(pk=job_id)
            if job.status in {
                DocumentJob.Status.WAITING,
                DocumentJob.Status.SUCCEEDED,
                DocumentJob.Status.CANCELLED,
                DocumentJob.Status.REJECTED,
                DocumentJob.Status.ATTENTION,
                DocumentJob.Status.FAILED,
            }:
                return
            provider = get_backend().provider
            try:

                def validate(result) -> None:
                    """Reject changed implementations and malformed completion receipts."""
                    current = provider_snapshot(provider)
                    if result is not None or current != job.configuration:
                        raise NeedsAttention(
                            "Search provider configuration or result changed"
                        )
                    validate_inputs(job)

                validate(None)
                operation = job.input["operation"]
                invoke(
                    job,
                    "maintenance",
                    job.configuration["implementation"],
                    job.input,
                    lambda invocation: _execute(provider, operation, job.input),
                    replay_safe=getattr(provider, "replay_safe", False) is True,
                    operation=f"search.{operation}",
                    provider=provider,
                    validate=validate,
                )
                job.refresh_from_db()
                job.status = DocumentJob.Status.SUCCEEDED
                job.updated = timezone.now()
                job.cleaned = True
                job.save(update_fields=["status", "updated", "cleaned"])
            except PendingOperation:
                return
            except Exception as error:
                job.refresh_from_db()
                job.status = operation_failure_status(job)
                job.error = str(error)
                job.save(update_fields=["status", "error"])
                raise
    except Timeout:
        return


class DeferredSearchBatch:
    """Collect one atomic provider batch for durable admission on successful exit."""

    def __init__(self, provider) -> None:
        self.provider = provider
        self.records = []
        self.removed = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        if exc_type is None:
            submit_search(
                self.provider,
                "write",
                {"records": self.records, "remove": self.removed},
            )

    def add_or_update(self, record: dict) -> None:
        """Retain plain core facts rather than ORM instances."""
        document_id = record["id"][0]
        self.removed = [pk for pk in self.removed if pk != document_id]
        self.records = [item for item in self.records if item["id"][0] != document_id]
        self.records.append(record)

    def remove(self, document_id: int) -> None:
        """Retain deletion identities even after authoritative rows disappear."""
        self.records = [item for item in self.records if item["id"][0] != document_id]
        if document_id not in self.removed:
            self.removed.append(document_id)
