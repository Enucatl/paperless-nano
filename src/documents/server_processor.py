"""Consume durable operations on shared server storage."""

from documents.deferred_operations import authorize
from documents.deferred_operations import server_operation
from documents.job_state import ProcessorFinished
from documents.models import DocumentJob
from documents.processor_notifications import defer_notifications
from documents.processor_notifications import notify_pending


@defer_notifications()
def process(job_id, key: str, user) -> bool:
    """Replay retained preparation only as far as the selected finite operation."""
    from filelock import FileLock

    from documents.enrichment_jobs import run_enrichment
    from documents.ingestion_jobs import run_consumption
    from documents.job_storage import job_directory
    from documents.jobs import run_reprocess
    from documents.search_operations import run_search

    with FileLock(
        job_directory(job_id).with_suffix(".lock"), timeout=0, is_singleton=True
    ):
        return _process_locked(
            job_id,
            key,
            user,
            {
                "reprocess": run_reprocess,
                "consume": run_consumption,
                "enrich": run_enrichment,
                "search": run_search,
            },
        )


def _process_locked(job_id, key, user, runners):
    """Keep cancellation and competing processors outside traversal."""
    job = DocumentJob.objects.get(pk=job_id)
    entry = job.result.get("invocations", {}).get(key)
    if not entry or entry["status"] != "pending" or entry.get("version") != 2:
        return False
    authorize(entry, user)
    if job.status != DocumentJob.Status.WAITING:
        return False
    previous = {
        name: dict(value) for name, value in job.result.get("invocations", {}).items()
    }
    attempts = job.attempts
    job.status = job.result.get("resume_status", DocumentJob.Status.QUEUED)
    job.save(update_fields=["status"])
    token = server_operation.set({"job_id": str(job.pk), "key": key, "user": user})
    try:
        runners[job.kind](str(job.pk))
    except ProcessorFinished:
        pass
    finally:
        server_operation.reset(token)
        job.refresh_from_db()
        job.attempts = attempts
        for name, invocation in job.result.get("invocations", {}).items():
            if (
                "policy" not in invocation
                and invocation["status"] == "running"
                and previous.get(name, {}).get("status") == "pending"
            ):
                job.result["invocations"][name] = previous[name]
        current = job.result["invocations"][key]
        if current["status"] == "leased" or (
            current["status"] == "pending"
            and (
                current.get("attempts", 0) > previous[key].get("attempts", 0)
                or job.status
                not in {
                    DocumentJob.Status.REJECTED,
                    DocumentJob.Status.ATTENTION,
                    DocumentJob.Status.FAILED,
                }
            )
        ):
            job.status = DocumentJob.Status.WAITING
        job.save(update_fields=["attempts", "status", "result"])
        notify_pending(job.pk)
    return True
