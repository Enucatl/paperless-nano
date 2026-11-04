"""Durable ingestion phases and opaque invocation outcomes under the job lock."""

import datetime
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any
from uuid import UUID, uuid5

from django.conf import settings
from django.contrib.auth.models import User
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.utils import timezone
from documents.data_models import (
    ConsumableDocument,
    DocumentMetadataOverrides,
    DocumentSource,
)
from documents.jobs import (
    RejectedResult,
    _check_permission,
    _copy,
    _revision,
    job_directory,
)
from documents.models import Document, DocumentJob
from documents.utils import compute_checksum, copy_basic_file_stats
from documents.versioning import latest_version

PHASES = ("accepted", "preparing", "prepared", "committed", "completing", "complete")
STATUSES = ("queued", "running", "prepared", "committed", "committed", "succeeded")


def transition(job: DocumentJob, phase: str) -> None:
    """Advance one legal phase without discarding the phase on operational failure."""
    job.refresh_from_db()
    current = job.result.get("phase", "accepted")
    if phase != current and PHASES.index(phase) != PHASES.index(current) + 1:
        raise ValueError(f"Illegal ingestion transition: {current} -> {phase}")
    if (
        phase == "prepared"
        and "prepared" not in job.result
        and "payload" not in job.result
    ):
        raise ValueError(
            "Prepared requires retained outputs or a terminal stop outcome"
        )
    if phase in {"committed", "completing", "complete"} and "payload" not in job.result:
        raise ValueError("Committed ingestion requires its document or stop outcome")
    if phase == "complete" and any(
        entry["status"] != "succeeded"
        for entry in job.result.get("invocations", {}).values()
    ):
        raise ValueError("Complete requires successful invocation outcomes")
    job.result["phase"] = phase
    job.status = STATUSES[PHASES.index(phase)]
    job.updated = timezone.now()
    job.error = ""
    job.save(update_fields=["result", "status", "updated", "error"])


def invoke(
    job: DocumentJob,
    key: str,
    identity: str,
    inputs: dict,
    execute: Callable[[str], Any],
    *,
    replay_safe: bool = False,
    reconcile: Callable[[str], Any] | None = None,
    validate: Callable[[Any], None] | None = None,
) -> Any:
    """Record an opaque call; an exception never proves absence of external effects.

    The coordinator must hold the job's process lock. Reconciliation returns a
    validated outcome or raises; it never grants permission for unsafe replay.
    """

    job.refresh_from_db()
    invocations = job.result.setdefault("invocations", {})
    entry = invocations.setdefault(
        key,
        {
            "id": str(uuid5(job.pk, key)),
            "plugin": identity,
            "input": json_value(inputs),
            "status": "pending",
            "result": None,
            "error": "",
            "replay_safe": replay_safe,
        },
    )
    if entry["plugin"] != identity or entry["input"] != json_value(inputs):
        raise NeedsAttention(f"Invocation {key} implementation or inputs changed")
    if entry["status"] == "succeeded":
        return entry["result"]
    replay_safe = replay_safe and entry["replay_safe"]
    if entry["status"] in {"running", "unknown", "failed"} and not replay_safe:
        entry["status"] = "unknown"
        job.save(update_fields=["result"])
        if reconcile is None:
            raise NeedsAttention(
                f"Invocation {key} ({entry['id']}) has an unknown outcome"
            )
        execute = reconcile
    entry["status"] = "running"
    job.save(update_fields=["result"])
    try:
        result = json_value(execute(entry["id"]))
        if validate is not None:
            validate(result)
    except Exception as error:
        job.refresh_from_db()
        entry = job.result["invocations"][key]
        entry["status"] = "failed" if replay_safe else "unknown"
        entry["error"] = str(error)
        job.save(update_fields=["result"])
        raise
    job.refresh_from_db()
    entry = job.result["invocations"][key]
    entry.update(status="succeeded", result=result, error="")
    job.save(update_fields=["result"])
    return result


class NeedsAttention(Exception):
    """An external effect may have happened and must not be repeated automatically."""


def json_value(value: object) -> object:
    """Normalize model inputs to JSON primitives, including dates and decimal values."""
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def validate_application(job_id: str | UUID) -> None:
    """Recheck parser outputs, the original actor and version revision before storing."""
    job = DocumentJob.objects.get(pk=job_id)
    from documents.parser_operations import validate_parser_job

    validate_parser_job(job)
    actor_id = job.input["overrides"].get("actor_id")
    root_id = job.input["root_document_id"]
    if root_id:
        qs = (
            Document.objects.select_for_update()
            if transaction.get_connection().in_atomic_block
            else Document.objects
        )
        root = qs.filter(pk=root_id).first()
        if (
            root is None
            or _revision(root) != job.input["revision"]
            or latest_version(root).pk != job.input["latest_id"]
        ):
            raise RejectedResult("Version root changed after admission")
        _check_permission(root, actor_id)
    elif actor_id is not None:
        actor = User.objects.filter(pk=actor_id, is_active=True).first()
        if actor is None or not actor.has_perm("documents.add_document"):
            raise RejectedResult("The requesting user can no longer add documents")


def record_progress(job_id: str, phase: str) -> None:
    """Keep phase progress available after worker or websocket loss."""
    job = DocumentJob.objects.get(pk=job_id)
    job.result["progress"] = phase
    job.save(update_fields=["result"])


def commit_consumption(job_id: str, document: Document) -> None:
    """Record document ownership and required completion in its database commit."""
    job = DocumentJob.objects.get(pk=job_id)
    document = Document.global_objects.get(pk=document.pk)
    job.result["published"] = {
        "original": {"path": str(document.source_path), "checksum": document.checksum},
        "archive": {
            "path": str(document.archive_path),
            "checksum": document.archive_checksum,
        }
        if document.archive_path
        else None,
        "thumbnail": {
            "path": str(document.thumbnail_path),
            "checksum": compute_checksum(document.thumbnail_path),
        },
    }
    job.result.update(
        {
            "payload": {"document_id": document.pk},
            "remove_source": True,
            "progress": "completion",
            "root_id": document.root_document_id or document.pk,
            "followup": [
                "metadata",
                "workflows",
                "extensions",
                "post_consume",
                "index",
                "cache",
                "delivery",
                "cleanup",
            ],
            "post_consume": "pending"
            if job.result.get("scripts", {}).get(
                "post", job.configuration.get("post_consume_script")
            )
            else "done",
        }
    )
    if job.result.get("lifecycle"):
        job.save(update_fields=["result"])
        transition(job, "committed")
    else:
        job.status = DocumentJob.Status.COMMITTED
        job.save(update_fields=["status", "result"])


def run_post_consume(job_id: str, callback: Callable[[], None]) -> None:
    """Never rerun a script whose external effects have an ambiguous outcome."""
    job = DocumentJob.objects.get(pk=job_id)
    state = job.result.get("post_consume", "done")
    if state in {"done", "cancelled"}:
        return
    if state == "started":
        raise NeedsAttention("Post-consume script may already have run")
    if (
        job.result.get("scripts", {}).get(
            "post", job.configuration.get("post_consume_script")
        )
        != settings.POST_CONSUME_SCRIPT
    ):
        raise NeedsAttention("Post-consume script configuration changed")

    def execute(_identifier: str) -> None:
        """Retain the legacy marker alongside the identified hook outcome."""
        job.refresh_from_db()
        job.result["post_consume"] = "started"
        job.save(update_fields=["result"])
        callback()
        job.refresh_from_db()
        job.result["post_consume"] = "done"
        job.save(update_fields=["result"])

    if job.result.get("lifecycle"):
        invoke(
            job,
            "complete:post_consume",
            "paperless.post_consume",
            {"script": settings.POST_CONSUME_SCRIPT},
            execute,
        )
    else:
        execute(str(job.pk))


def restore_input(
    job: DocumentJob,
) -> tuple[ConsumableDocument, DocumentMetadataOverrides]:
    """Reconstitute legacy parser input only inside the execution adapter."""
    directory = job_directory(job.pk)
    working = directory / "work" / job.input["filename"]
    if not working.exists() and (directory / "input").exists():
        _copy(directory / "input", working)
        copy_basic_file_stats(directory / "input", working)
    metadata = dict(job.input["overrides"])
    if metadata.get("created"):
        metadata["created"] = datetime.date.fromisoformat(metadata["created"])
    input_doc = ConsumableDocument(
        source=DocumentSource(job.input["source"]),
        original_file=working,
        original_path=Path(job.input["original_path"])
        if job.input["original_path"]
        else None,
        source_path=Path(job.input["source_path"]),
        root_document_id=job.input["root_document_id"],
        mailrule_id=job.input["mailrule_id"],
        job_id=str(job.pk),
        defer_mime_type=True,
    )
    return input_doc, DocumentMetadataOverrides(**metadata)
