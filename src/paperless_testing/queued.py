"""Run durable tasks with an explicitly authorized local processor in tests."""

from datetime import timedelta
from functools import wraps
from unittest.mock import patch

from django.test import override_settings
from django.utils import timezone


def drive_processors(function, processor):
    """Enable server placement and model later delivery after simulated crashes."""

    @wraps(function)
    def run(job_id, *args, **kwargs):
        from documents.deferred_operations import recover_waiting
        from documents.deferred_operations import resume
        from documents.models import DocumentJob

        with override_settings(
            PROCESSOR_USER=processor.username,
            PROCESSOR_CAPABILITIES=("parser", "metadata", "search", "ingestion"),
        ):
            job = DocumentJob.objects.get(pk=job_id)
            leased = [
                entry
                for entry in job.result.get("invocations", {}).values()
                if entry["status"] == "leased"
            ]
            if leased:
                # Resuming a simulated crash represents delivery after lease expiry.
                for entry in leased:
                    entry["lease_until"] = (
                        timezone.now() - timedelta(seconds=1)
                    ).isoformat()
                job.save(update_fields=["result"])
                recover_waiting()
                job.refresh_from_db()
            if job.status == DocumentJob.Status.WAITING:
                with patch("documents.jobs._dispatch"):
                    resume(job)
            return function(job_id, *args, **kwargs)

    return run
