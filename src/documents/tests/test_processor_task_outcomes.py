"""Processor failure completion does not depend on another Celery delivery."""

from datetime import timedelta
from types import SimpleNamespace

import pytest
from django.utils import timezone

from documents import jobs
from documents.models import DocumentJob
from documents.models import PaperlessTask
from documents.search_operations import run_search
from documents.search_operations import submit_search
from paperless_testing.factories import PaperlessTaskFactory
from paperless_testing.factories import UserFactory
from paperless_testing.queued import drive_processors

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("paperless_dirs"),
]


def test_endpoint_exhaustion_completes_task_without_coordinator(mocker):
    """Transient failures stay started; the third records failure and duration."""
    provider = SimpleNamespace(identity="search:1", configuration={}, replay_safe=True)
    mocker.patch(
        "documents.search.get_backend", return_value=SimpleNamespace(provider=provider)
    )
    mocker.patch("documents.search_operations.run_search.apply_async")
    mocker.patch("documents.jobs._dispatch")
    mocker.patch(
        "documents.search_operations._execute",
        side_effect=RuntimeError("endpoint down"),
    )
    job = submit_search(provider, "optimize", {})
    started = timezone.now() - timedelta(seconds=10)
    task = PaperlessTaskFactory(
        task_id=str(job.pk),
        status=PaperlessTask.Status.STARTED,
        date_started=started,
        date_created=started - timedelta(seconds=2),
    )
    run_search(str(job.pk))
    processor = UserFactory(is_superuser=True)
    for attempt in range(3):
        with pytest.raises(RuntimeError, match="endpoint down"):
            drive_processors(run_search, processor)(str(job.pk))
        task.refresh_from_db()
        if attempt < 2:
            assert task.status == PaperlessTask.Status.STARTED
            assert task.date_done is None
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.FAILED
    assert task.status == PaperlessTask.Status.FAILURE
    assert task.result_data["error_message"] == "endpoint down"
    assert task.date_done >= started
    assert task.duration_seconds >= 10
    assert task.wait_time_seconds == 2
    completed = task.date_done
    drive_processors(run_search, processor)(str(job.pk))
    task.refresh_from_db()
    assert task.date_done == completed


@pytest.mark.parametrize("status", ["failed", "rejected", "attention"])
def test_recovery_repairs_stranded_task_and_preserves_final_history(mocker, status):
    """Restart recovery completes old records without overwriting a final receipt."""
    updated = timezone.now()
    job = DocumentJob.objects.create(
        timeout=30, status=status, error="retained error", updated=updated
    )
    task = PaperlessTaskFactory(
        task_id=str(job.pk),
        status=PaperlessTask.Status.STARTED,
        date_started=updated - timedelta(seconds=10),
    )
    completed_job = DocumentJob.objects.create(
        timeout=30, status=status, error="older error"
    )
    completed = PaperlessTaskFactory(
        task_id=str(completed_job.pk),
        status=PaperlessTask.Status.SUCCESS,
        result_data={"preserved": True},
    )
    mocker.patch("documents.jobs._dispatch")
    jobs.recover_document_jobs()
    task.refresh_from_db()
    completed.refresh_from_db()
    assert task.status == PaperlessTask.Status.FAILURE
    assert task.date_done == updated
    assert task.result_data["error_message"] == "retained error"
    assert completed.status == PaperlessTask.Status.SUCCESS
    assert completed.result_data == {"preserved": True}


def test_duplicate_celery_delivery_preserves_processor_failure():
    """Late Celery lifecycle events cannot reopen or replace a terminal receipt."""
    from documents.signals.handlers import task_postrun_handler
    from documents.signals.handlers import task_prerun_handler

    task = PaperlessTaskFactory(
        status=PaperlessTask.Status.FAILURE,
        date_done=timezone.now(),
        result_data={"error_message": "endpoint down"},
    )
    completed = task.date_done
    task_prerun_handler(task_id=task.task_id)
    task_postrun_handler(task_id=task.task_id, state="SUCCESS", retval={"late": True})
    task.refresh_from_db()
    assert task.status == PaperlessTask.Status.FAILURE
    assert task.date_done == completed
    assert task.result_data == {"error_message": "endpoint down"}


@pytest.mark.parametrize("delivery", ["signal", "lean"])
@pytest.mark.parametrize("status", ["waiting", "queued", "running", "succeeded"])
def test_native_failure_does_not_complete_recoverable_job(delivery, status, tmp_path):
    """A failed Celery attempt cannot freeze durable history before recovery."""
    from documents.signals.handlers import task_failure_handler
    from documents.signals.handlers import task_postrun_handler
    from paperless_worker.outcome_helper import drain
    from paperless_worker.outcomes import OutcomeWriter

    job = DocumentJob.objects.create(timeout=30, status=status)
    task = PaperlessTaskFactory(
        task_id=str(job.pk),
        task_type=PaperlessTask.TaskType.SEARCH_MAINTENANCE,
        status=PaperlessTask.Status.STARTED,
    )
    if delivery == "signal":
        task_failure_handler(task_id=str(job.pk), exception=RuntimeError("temporary"))
    else:
        writer = OutcomeWriter(
            tmp_path,
            {"documents.search_operations.run_search": task.task_type},
        )
        writer.record_failed(
            str(job.pk),
            "documents.search_operations.run_search",
            "WorkerLostError",
            "temporary",
        )
        assert drain(writer.directory)
    task.refresh_from_db()
    assert task.status == PaperlessTask.Status.STARTED
    assert task.date_done is None
    job.status = DocumentJob.Status.SUCCEEDED
    job.save(update_fields=["status"])
    task_postrun_handler(task_id=str(job.pk), state="SUCCESS", retval={})
    task.refresh_from_db()
    assert task.status == PaperlessTask.Status.SUCCESS


@pytest.mark.parametrize("status", ["waiting", "queued", "running"])
def test_recoverable_consumption_does_not_start_native_errbacks(mocker, status):
    """Celery ignores a failed attempt while the journal still owns continuation."""
    from documents.tasks import consume_file

    job = DocumentJob.objects.create(
        kind="consume",
        timeout=30,
        status=status,
        dispatch_options={"link_error": [{"task": "example.error_callback"}]},
    )
    mocker.patch(
        "documents.ingestion_jobs.run_consumption",
        side_effect=RuntimeError("temporary"),
    )
    task = PaperlessTaskFactory(
        task_id=str(job.pk),
        task_type=PaperlessTask.TaskType.CONSUME_FILE,
        status=PaperlessTask.Status.STARTED,
    )
    result = consume_file.apply(kwargs={"job_id": str(job.pk)}, task_id=str(job.pk))
    assert result.state == "IGNORED"
    job.refresh_from_db()
    task.refresh_from_db()
    assert "delivery" not in job.result
    assert task.status == PaperlessTask.Status.STARTED
    assert task.date_done is None
