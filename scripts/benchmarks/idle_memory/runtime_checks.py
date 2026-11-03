"""Exercise recovery after idle, only against the disposable benchmark clone."""

# ruff: noqa: INP001

from __future__ import annotations

import io
import json
import os
import signal
import sys
import time
from contextlib import redirect_stdout
from datetime import timedelta
from unittest.mock import patch


def deadline_expired(signum: int, frame: object) -> None:
    """Stop the entire helper if its bounded verification deadline expires."""
    raise TimeoutError("Isolated runtime checks exceeded 300 seconds")


def main() -> dict[str, bool]:
    """Verify real queue recovery without replaying uncertain ingestion effects."""
    if os.environ.get("BENCH_ISOLATED") != "1" or os.getuid() != 1000:
        raise RuntimeError("Runtime checks require the isolated clone and uid 1000")
    signal.signal(signal.SIGALRM, deadline_expired)
    signal.alarm(300)
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "paperless.settings")
    import django

    django.setup()

    from django.contrib.auth import get_user_model
    from django.core.management import call_command
    from django.utils import timezone

    from documents import jobs
    from documents.models import Document
    from documents.models import DocumentJob
    from documents.tasks import recover_document_jobs
    from documents.utils import compute_checksum

    actor = get_user_model().objects.get(
        username="benchadmin",
        email="benchadmin@example.invalid",
        is_superuser=True,
        is_active=True,
    )
    document = Document.objects.get(
        pk=int(os.environ["BENCH_DOCUMENT_ID"]),
        owner=actor,
        mime_type="application/pdf",
        page_count__gte=1,
        page_count__lte=10,
    )

    def permanent_files() -> dict[str, str]:
        """Capture permanent file integrity without printing private paths."""
        document.refresh_from_db()
        return {
            str(path): compute_checksum(path)
            for path in (
                document.source_path,
                document.archive_path,
                document.thumbnail_path,
            )
            if path is not None
        }

    def wait_success(job: DocumentJob) -> None:
        """Observe durable completion by the actual broker and lean worker."""
        while True:
            job.refresh_from_db()
            if job.status == DocumentJob.Status.SUCCEEDED and job.cleaned:
                assert job.result["followup"] == []
                return
            assert job.status not in {
                DocumentJob.Status.FAILED,
                DocumentJob.Status.REJECTED,
                DocumentJob.Status.ATTENTION,
            }, "Runtime check reached an unexpected terminal job state"
            time.sleep(1)

    def retained_input(job: DocumentJob) -> bool:
        """Check recovery input integrity without removing retained artifacts."""
        path = jobs.job_directory(job.pk) / job.input["staged_name"]
        return path.is_file() and compute_checksum(path) == job.input["checksum"]

    # Leave a failed, admitted attempt for a real recovery sweep to redispatch.
    before_files = permanent_files()
    before_revision = jobs._revision(document)
    retry = jobs.stage_reprocess(document.pk, actor_id=actor.pk)
    with patch(
        "documents.jobs.run_parser", side_effect=RuntimeError("injected parser failure")
    ):
        try:
            jobs.run_reprocess(retry.pk)
        except RuntimeError as error:
            assert str(error) == "injected parser failure"
        else:
            raise AssertionError("Injected parser failure was not observed")
    retry.refresh_from_db()
    assert retry.status == DocumentJob.Status.QUEUED and retry.attempts == 1
    assert permanent_files() == before_files
    assert jobs._revision(document) == before_revision
    assert retained_input(retry)
    DocumentJob.objects.filter(pk=retry.pk).update(
        updated=timezone.now() - timedelta(minutes=6),
    )
    recover_document_jobs.delay()
    wait_success(retry)
    assert retry.attempts == 2

    # Publication has committed; only safe completion may resume, without parsing.
    completion = jobs.stage_reprocess(document.pk, actor_id=actor.pk)
    with patch(
        "documents.jobs._finish",
        side_effect=RuntimeError("injected completion failure"),
    ):
        try:
            jobs.run_reprocess(completion.pk)
        except RuntimeError as error:
            assert str(error) == "injected completion failure"
        else:
            raise AssertionError("Injected completion failure was not observed")
    completion.refresh_from_db()
    assert completion.status == DocumentJob.Status.COMMITTED
    assert completion.attempts == 1 and completion.result["followup"]
    assert retained_input(completion)
    document.refresh_from_db()
    assert document.content_job == completion.pk
    committed_revision = jobs._revision(document)
    archive_checksum = (
        compute_checksum(document.archive_path) if document.archive_path else None
    )
    # No --acknowledge-effects: this is reprocessing, with no ingestion callbacks.
    call_command(
        "document_jobs", job=str(completion.pk), resume=True, stdout=io.StringIO()
    )
    wait_success(completion)
    document.refresh_from_db()
    assert completion.attempts == 1
    assert jobs._revision(document) == committed_revision
    assert (
        compute_checksum(document.archive_path) if document.archive_path else None
    ) == archive_checksum

    # Reject a real prepared result after a newer edit, keeping recovery inputs.
    before_files = permanent_files()
    stale = jobs.stage_reprocess(document.pk, actor_id=actor.pk)
    apply_result = jobs._apply
    newer_content = "Isolated benchmark newer edit must survive stale publication."

    def edit_before_apply(job: DocumentJob) -> None:
        """Inject a concurrent edit immediately before real result publication."""
        assert job.status == DocumentJob.Status.PREPARED
        Document.objects.filter(pk=document.pk).update(content=newer_content)
        apply_result(job)

    with patch("documents.jobs._apply", side_effect=edit_before_apply):
        try:
            jobs.run_reprocess(stale.pk)
        except jobs.RejectedResult:
            pass
        else:
            raise AssertionError("Stale result was not rejected")
    stale.refresh_from_db()
    assert stale.status == DocumentJob.Status.REJECTED
    assert permanent_files() == before_files
    assert document.content == newer_content
    assert retained_input(stale)
    signal.alarm(0)
    return {
        "parser_retry_via_recovery_sweep": True,
        "committed_completion_resumed_without_reparse": True,
        "stale_result_rejected_and_input_retained": True,
    }


if __name__ == "__main__":
    with redirect_stdout(sys.stderr):
        checks = main()
    sys.stdout.write(json.dumps({"runtime_checks": checks}) + "\n")
