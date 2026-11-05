"""Durable ingestion phases and opaque invocation outcomes under the job lock."""

import datetime
from collections.abc import Callable
from pathlib import Path
from uuid import UUID

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.utils import timezone

from documents.data_models import ConsumableDocument
from documents.data_models import DocumentMetadataOverrides
from documents.data_models import DocumentSource
from documents.job_state import RejectedResult
from documents.job_state import check_permission as _check_permission
from documents.job_state import document_revision as _revision
from documents.job_storage import atomic_copy as _copy
from documents.job_storage import job_directory
from documents.models import Document
from documents.models import DocumentJob
from documents.operation_journal import NeedsAttention
from documents.operation_journal import invoke
from documents.operation_journal import json_value  # noqa: F401
from documents.operation_journal import operation_failure_status  # noqa: F401
from documents.utils import compute_checksum
from documents.utils import copy_basic_file_stats
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

    def validate_receipt(result) -> None:
        """A completed hook returns no metadata or other application state."""
        if result is not None:
            raise ValueError("Post-consume completion must return null")

    if job.result.get("lifecycle"):
        invoke(
            job,
            "complete:post_consume",
            "paperless.post_consume",
            {"script": settings.POST_CONSUME_SCRIPT},
            execute,
            validate=validate_receipt,
            allow_deferred=False,
        )
        job.refresh_from_db()
        job.result["post_consume"] = "done"
        job.save(update_fields=["result"])
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
