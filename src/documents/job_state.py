"""Shared document revision and authorization checks for durable operations."""

from functools import wraps

from django.contrib.auth.models import User
from django.utils import timezone

from documents.models import Document
from documents.models import DocumentJob
from documents.models import PaperlessTask
from documents.permissions import permitted_document_ids

REVISION_FIELDS = (
    "content",
    "checksum",
    "archive_checksum",
    "archive_filename",
    "filename",
    "modified",
    "mime_type",
    "root_document_id",
    "content_job",
)


class RejectedResult(Exception):
    """The input, authorization, or output no longer permits application."""


def document_revision(document: Document) -> dict:
    """Capture fields whose replacement would invalidate a parsing result."""
    revision = {field: getattr(document, field) for field in REVISION_FIELDS}
    revision["modified"] = document.modified.isoformat()
    revision["content_job"] = (
        str(document.content_job) if document.content_job else None
    )
    return revision


def check_permission(document: Document, actor_id: int | None) -> None:
    """Recheck the original caller; None explicitly denotes a trusted CLI job."""
    if actor_id is None:
        return
    actor = User.objects.filter(pk=actor_id, is_active=True).first()
    if (
        actor is None
        or not actor.has_perm("documents.change_document")
        or not permitted_document_ids(actor, perm="change_document")
        .filter(pk=document.pk)
        .exists()
    ):
        raise RejectedResult("The requesting user can no longer change this document")


class PendingOperation(Exception):
    """The accepted invocation is waiting without holding execution resources."""


def requester_id(job: DocumentJob) -> int | None:
    """Read the admission actor for both ingestion and document operations."""
    return job.input.get("actor_id", job.input.get("overrides", {}).get("actor_id"))


def execution_policy(operation: str, identity: str, provider=None) -> dict | None:
    """Queue finite provider work; lifecycle hooks remain core-owned."""
    from documents.server_processor import capability_for

    capability = capability_for(operation)
    if capability not in {"parser", "metadata", "search", "ingestion"}:
        return None
    return {"permission": f"documents.process_{capability}"}


def wait(job: DocumentJob, entry: dict) -> None:
    """Persist waiting separately from the coordinator's resumable phase."""
    job.result.setdefault("resume_status", job.status)
    job.status = DocumentJob.Status.WAITING
    job.updated = timezone.now()
    job.save(update_fields=["result", "status", "updated"])
    from documents.processor_notifications import notify_pending

    notify_pending(job.pk)
    raise PendingOperation(entry["id"])


def task_history_status(status: str) -> str | None:
    """Translate durable job completion; nonterminal jobs have no final outcome."""
    return {
        DocumentJob.Status.SUCCEEDED: PaperlessTask.Status.SUCCESS,
        DocumentJob.Status.CANCELLED: PaperlessTask.Status.REVOKED,
        DocumentJob.Status.FAILED: PaperlessTask.Status.FAILURE,
        DocumentJob.Status.REJECTED: PaperlessTask.Status.FAILURE,
        DocumentJob.Status.ATTENTION: PaperlessTask.Status.FAILURE,
    }.get(status)


def sync_terminal_task(job: DocumentJob) -> None:
    """Record terminal processor failures without requiring a Celery callback."""
    from paperless_worker.outcome_helper import apply_outcome

    if task_history_status(job.status) != PaperlessTask.Status.FAILURE:
        return
    apply_outcome(
        str(job.pk),
        PaperlessTask.Status.FAILURE,
        {"error_type": "DocumentJobError", "error_message": job.error},
        job.updated,
    )


def track_terminal_task(runner):
    """Complete terminal task history even when runners execute outside Celery."""

    @wraps(runner)
    def run(job_id, *args, **kwargs):
        try:
            return runner(job_id, *args, **kwargs)
        finally:
            sync_terminal_task(DocumentJob.objects.get(pk=job_id))

    return run
