"""Drive real durable transitions for tests focused on local provider behavior."""

from functools import wraps
from unittest.mock import patch

from celery.exceptions import Ignore


def drive_processors(function, processor):
    """Resume coordinators after each separately claimed server computation."""

    @wraps(function)
    def run(job_id, *args, **kwargs):
        from documents.deferred_operations import server_operation
        from documents.models import DocumentJob
        from documents.server_processor import process

        if server_operation.get() is not None:
            return function(job_id, *args, **kwargs)
        for _ in range(100):
            ignored = False
            try:
                result = function(job_id, *args, **kwargs)
            except Ignore:
                ignored = True
            job = DocumentJob.objects.get(pk=job_id)
            if job.status != DocumentJob.Status.WAITING:
                if ignored:
                    raise Ignore
                return result
            pending = [
                key
                for key, entry in job.result.get("invocations", {}).items()
                if entry["status"] == "pending" and "policy" in entry
            ]
            if not pending:
                from datetime import timedelta

                from django.utils import timezone

                from documents.deferred_operations import recover_waiting

                leased = [
                    entry
                    for entry in job.result["invocations"].values()
                    if entry["status"] == "leased"
                ]
                assert leased, "Waiting job has neither pending nor leased work"
                # A resumed crash test represents a later worker after lease expiry.
                for entry in leased:
                    entry["lease_until"] = (
                        timezone.now() - timedelta(seconds=1)
                    ).isoformat()
                job.save(update_fields=["result"])
                recover_waiting()
                continue
            assert len(pending) == 1
            with patch("documents.jobs._dispatch"):
                process(str(job.pk), pending[0], processor)
        raise AssertionError("Queued job did not finish within 100 operations")

    return run
