"""Durable reprocessing survives delivery, publication, and completion failures."""

from __future__ import annotations

import json
from datetime import timedelta
from typing import TYPE_CHECKING
from unittest.mock import Mock
from uuid import UUID

import pytest
from django.conf import settings
from django.utils import timezone
from filelock import FileLock
from filelock import Timeout

from documents import jobs
from documents.models import Document
from documents.models import DocumentJob
from documents.utils import compute_checksum
from paperless.config import OcrConfig
from paperless_testing.factories import DocumentFactory
from paperless_testing.factories import UserFactory
from paperless_testing.permissions import grant_global

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture

pytestmark = pytest.mark.django_db(transaction=True)


class WorkerStopped(BaseException):
    """Simulate termination without running ordinary exception recovery."""


@pytest.fixture
def parser(
    mocker: MockerFixture,
    simple_digital_pdf_file: Path,
    thumbnail_webp_file: Path,
) -> type:
    """Supply real output files without running OCR or external services."""

    class Parser:
        """The existing parser protocol backed by fixed, valid test outputs."""

        name = "Test parser"
        version = "1.0"
        requires_pdf_rendition = False
        can_produce_archive = True
        configure = Mock()
        parse = Mock()
        get_text = Mock(return_value="reprocessed content")
        get_thumbnail = Mock(return_value=thumbnail_webp_file)
        get_archive_path = Mock(return_value=simple_digital_pdf_file)

        def __enter__(self) -> Parser:
            return self

        def __exit__(self, *args) -> None:
            pass

    mocker.patch(
        "paperless.parsers.providers.get_parser_registry",
    ).return_value.get_parser_for_file.return_value = Parser
    mocker.patch("documents.search.get_backend")
    return Parser


def test_staged_input_survives_loss_of_original(
    sample_doc: Document,
    parser: type,
) -> None:
    """Parsing reads the durable snapshot even if its original location vanishes."""
    original_bytes = sample_doc.source_path.read_bytes()
    job = jobs.stage_reprocess(sample_doc.pk)
    staged = jobs.job_directory(job.pk) / job.input["staged_name"]
    sample_doc.source_path.unlink()

    def read_snapshot(source: Path, *args, **kwargs) -> None:
        """Require the actual parser input to retain the admitted bytes."""
        assert source.read_bytes() == original_bytes

    parser.parse.side_effect = read_snapshot

    assert staged.read_bytes() == original_bytes
    with pytest.raises(OSError, match="Original file missing"):
        jobs.run_reprocess(job.pk)

    parser.parse.assert_called_once()
    parser_input = parser.parse.call_args.args[0]
    assert parser_input.is_relative_to(jobs.job_directory(job.pk))
    assert parser_input != staged
    assert parser_input.read_bytes() == original_bytes
    job.refresh_from_db()
    sample_doc.refresh_from_db()
    assert job.status == DocumentJob.Status.SUCCEEDED
    assert not job.cleaned
    assert sample_doc.content == "reprocessed content"
    assert sample_doc.archive_checksum == compute_checksum(sample_doc.archive_path)
    assert staged.read_bytes() == original_bytes
    json.dumps(job.input)
    json.dumps(job.configuration)
    json.dumps(job.provider)
    json.dumps(job.result)
    assert job.provider["implementation"]["version"] == "1.0"
    assert "remote_ocr_api_key" not in job.configuration["settings"]["remote"]


def test_parser_settings_snapshot_is_not_replaced_with_later_configuration(
    sample_doc: Document,
    parser: type,
) -> None:
    """Use the parser's constructed OCR settings and reject a changed snapshot."""
    job = jobs.stage_reprocess(sample_doc.pk)
    parser.settings = OcrConfig()
    parser.settings.language = "fra"

    with pytest.raises(jobs.RejectedResult, match="configuration changed"):
        jobs.run_reprocess(job.pk)

    job.refresh_from_db()
    sample_doc.refresh_from_db()
    assert job.configuration["settings"]["ocr"]["language"] != "fra"
    assert sample_doc.content == "test content"
    assert (jobs.job_directory(job.pk) / job.input["staged_name"]).is_file()


@pytest.fixture
def changed_thumbnail(tmp_path: Path) -> Path:
    """Create a different valid thumbnail to distinguish competing results."""
    from PIL import Image

    path = tmp_path / "changed.webp"
    Image.new("RGB", (16, 16), "red").save(path, format="WEBP")
    return path


def test_timeout_retains_input_and_leaves_document_unchanged(
    sample_doc: Document,
    parser: type,
    mocker: MockerFixture,
) -> None:
    """An overlong parser result cannot publish after the recorded deadline."""
    job = jobs.stage_reprocess(sample_doc.pk)
    after_deadline = timezone.now() + timedelta(seconds=job.timeout + 1)

    def exceed_timeout(*args, **kwargs) -> None:
        """Advance the job clock while the parser is running."""
        mocker.patch("documents.jobs.timezone.now", return_value=after_deadline)

    parser.parse.side_effect = exceed_timeout

    with pytest.raises(TimeoutError, match="recorded timeout"):
        jobs.run_reprocess(job.pk)

    job.refresh_from_db()
    sample_doc.refresh_from_db()
    assert job.status == DocumentJob.Status.QUEUED
    assert job.attempts == 1
    assert sample_doc.content == "test content"
    assert (jobs.job_directory(job.pk) / job.input["staged_name"]).exists()


def test_three_processing_failures_stop_retries_without_deleting_input(
    sample_doc: Document,
    parser: type,
) -> None:
    """Repeated failures become explicit terminal state, preserving recovery data."""
    job = jobs.stage_reprocess(sample_doc.pk)
    parser.parse.side_effect = RuntimeError("engine unavailable")

    for _ in range(3):
        with pytest.raises(RuntimeError, match="engine unavailable"):
            jobs.run_reprocess(job.pk)
    with pytest.raises(jobs.RejectedResult, match="exhausted three attempts"):
        jobs.run_reprocess(job.pk)

    job.refresh_from_db()
    sample_doc.refresh_from_db()
    assert job.status == DocumentJob.Status.FAILED
    assert job.attempts == 3
    assert parser.parse.call_count == 3
    assert sample_doc.content == "test content"
    assert (jobs.job_directory(job.pk) / job.input["staged_name"]).exists()


def test_new_version_during_parsing_rejects_result(
    sample_doc: Document,
    parser: type,
) -> None:
    """A result admitted for the root cannot replace a newly selected version."""
    job = jobs.stage_reprocess(sample_doc.pk)
    parser.parse.side_effect = lambda *args, **kwargs: DocumentFactory(
        root_document=sample_doc,
        version_index=1,
    )

    with pytest.raises(jobs.RejectedResult, match="current version changed"):
        jobs.run_reprocess(job.pk)

    job.refresh_from_db()
    sample_doc.refresh_from_db()
    assert job.status == DocumentJob.Status.REJECTED
    assert sample_doc.content == "test content"


def test_deleted_actor_is_not_reinterpreted_as_trusted_job(
    sample_doc: Document,
    parser: type,
) -> None:
    """Deleting the initiating user does not remove the authorization requirement."""
    user = UserFactory()
    grant_global(user, "change_document")
    job = jobs.stage_reprocess(sample_doc.pk, actor_id=user.pk)
    parser.parse.side_effect = lambda *args, **kwargs: user.delete()

    with pytest.raises(jobs.RejectedResult, match="can no longer change"):
        jobs.run_reprocess(job.pk)

    sample_doc.refresh_from_db()
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.REJECTED
    assert job.input["actor_id"] is not None
    assert sample_doc.content == "test content"


def test_metadata_edit_after_commit_keeps_pending_thumbnail(
    sample_doc: Document,
    parser: type,
    changed_thumbnail: Path,
    mocker: MockerFixture,
) -> None:
    """An unrelated edit after commit must not silently drop thumbnail completion."""
    job = jobs.stage_reprocess(sample_doc.pk)
    parser.get_thumbnail.return_value = changed_thumbnail
    finish = mocker.patch("documents.jobs._finish", side_effect=WorkerStopped)
    with pytest.raises(WorkerStopped):
        jobs.run_reprocess(job.pk)
    mocker.stop(finish)
    Document.objects.filter(pk=sample_doc.pk).update(
        title="newer title",
        modified=timezone.now(),
    )

    jobs.run_reprocess(job.pk)

    sample_doc.refresh_from_db()
    assert sample_doc.title == "newer title"
    assert sample_doc.thumbnail_path.read_bytes() == changed_thumbnail.read_bytes()
    parser.parse.assert_called_once()


def test_older_committed_job_cannot_overwrite_newer_thumbnail(
    sample_doc: Document,
    parser: type,
    changed_thumbnail: Path,
    mocker: MockerFixture,
) -> None:
    """Completion of an older result must respect a newer committed generation."""
    older = jobs.stage_reprocess(sample_doc.pk)
    finish = mocker.patch("documents.jobs._finish", side_effect=WorkerStopped)
    with pytest.raises(WorkerStopped):
        jobs.run_reprocess(older.pk)
    mocker.stop(finish)
    parser.get_thumbnail.return_value = changed_thumbnail
    parser.get_text.return_value = "newer result"
    newer = jobs.stage_reprocess(sample_doc.pk)
    jobs.run_reprocess(newer.pk)

    jobs.run_reprocess(older.pk)

    sample_doc.refresh_from_db()
    assert sample_doc.content == "newer result"
    assert sample_doc.content_job == newer.pk
    assert sample_doc.thumbnail_path.read_bytes() == changed_thumbnail.read_bytes()
    assert parser.parse.call_count == 2


def test_duplicate_delivery_does_not_reapply_or_parse(
    sample_doc: Document,
    parser: type,
) -> None:
    """A completed job is a no-op even when its staging has already been removed."""
    job = jobs.stage_reprocess(sample_doc.pk)
    jobs.run_reprocess(job.pk)
    sample_doc.refresh_from_db()
    completed_modified = sample_doc.modified
    completed_archive = sample_doc.archive_filename

    jobs.run_reprocess(str(job.pk))

    parser.parse.assert_called_once()
    sample_doc.refresh_from_db()
    assert sample_doc.modified == completed_modified
    assert sample_doc.archive_filename == completed_archive
    assert Document.objects.count() == 1


def test_queryset_edit_during_parsing_rejects_stale_result(
    sample_doc: Document,
    parser: type,
) -> None:
    """Content edits that bypass auto_now still invalidate the staged revision."""
    job = jobs.stage_reprocess(sample_doc.pk)
    old_archive = sample_doc.archive_path.read_bytes()
    old_thumbnail = sample_doc.thumbnail_path.read_bytes()
    parser.parse.side_effect = lambda *args, **kwargs: Document.objects.filter(
        pk=sample_doc.pk,
    ).update(content="newer user edit")

    with pytest.raises(jobs.RejectedResult, match="changed"):
        jobs.run_reprocess(job.pk)

    sample_doc.refresh_from_db()
    job.refresh_from_db()
    assert sample_doc.content == "newer user edit"
    assert sample_doc.archive_path.read_bytes() == old_archive
    assert sample_doc.thumbnail_path.read_bytes() == old_thumbnail
    assert job.status == DocumentJob.Status.REJECTED
    assert (jobs.job_directory(job.pk) / job.input["staged_name"]).exists()


def test_permission_revoked_during_parsing_rejects_result(
    sample_doc: Document,
    parser: type,
) -> None:
    """Authorization is checked again before publication, with a fresh user."""
    user = UserFactory()
    grant_global(user, "change_document")
    job = jobs.stage_reprocess(sample_doc.pk, actor_id=user.pk)
    parser.parse.side_effect = lambda *args, **kwargs: user.user_permissions.clear()

    with pytest.raises(jobs.RejectedResult, match="can no longer change"):
        jobs.run_reprocess(job.pk)

    sample_doc.refresh_from_db()
    job.refresh_from_db()
    assert sample_doc.content == "test content"
    assert job.status == DocumentJob.Status.REJECTED
    assert (jobs.job_directory(job.pk) / job.input["staged_name"]).exists()


def test_crash_after_publication_before_commit_reuses_output(
    sample_doc: Document,
    parser: type,
    mocker: MockerFixture,
) -> None:
    """A published generation cannot replace the old archive until its DB commit."""
    job = jobs.stage_reprocess(sample_doc.pk)
    old_archive_path = sample_doc.archive_path
    old_archive_bytes = old_archive_path.read_bytes()
    save = DocumentJob.save

    def stop_at_commit(instance: DocumentJob, *args, **kwargs) -> None:
        if instance.status == DocumentJob.Status.COMMITTED:
            raise WorkerStopped
        save(instance, *args, **kwargs)

    stop = mocker.patch.object(DocumentJob, "save", stop_at_commit)
    with pytest.raises(WorkerStopped):
        jobs.run_reprocess(job.pk)
    mocker.stop(stop)

    job.refresh_from_db()
    sample_doc.refresh_from_db()
    assert job.status == DocumentJob.Status.PREPARED
    assert sample_doc.content == "test content"
    assert sample_doc.archive_path == old_archive_path
    assert old_archive_path.read_bytes() == old_archive_bytes
    assert (jobs.job_directory(job.pk) / job.input["staged_name"]).exists()
    published_name = job.result["archive_filename"]

    jobs.run_reprocess(job.pk)

    job.refresh_from_db()
    sample_doc.refresh_from_db()
    assert job.status == DocumentJob.Status.SUCCEEDED
    assert sample_doc.archive_filename == published_name
    assert sample_doc.archive_checksum == compute_checksum(sample_doc.archive_path)
    assert not old_archive_path.exists()
    parser.parse.assert_called_once()


def test_crash_after_commit_resumes_required_completion(
    sample_doc: Document,
    parser: type,
    mocker: MockerFixture,
) -> None:
    """A committed result retains staged outputs until its completion is durable."""
    job = jobs.stage_reprocess(sample_doc.pk)
    finish = mocker.patch("documents.jobs._finish", side_effect=WorkerStopped)
    with pytest.raises(WorkerStopped):
        jobs.run_reprocess(job.pk)
    mocker.stop(finish)

    job.refresh_from_db()
    sample_doc.refresh_from_db()
    assert job.status == DocumentJob.Status.COMMITTED
    assert job.result["followup"]
    assert sample_doc.content == "reprocessed content"
    assert (jobs.job_directory(job.pk) / job.input["staged_name"]).exists()

    jobs.run_reprocess(job.pk)

    parser.parse.assert_called_once()
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.SUCCEEDED
    assert job.result["followup"] == []
    assert job.cleaned


def test_index_failure_keeps_committed_job_recoverable(
    sample_doc: Document,
    parser: type,
    mocker: MockerFixture,
) -> None:
    """An index writer failure cannot be mistaken for completed follow-up work."""
    backend = mocker.patch("documents.search.get_backend").return_value
    backend.batch_update.return_value.__enter__.side_effect = RuntimeError("index busy")
    job = jobs.stage_reprocess(sample_doc.pk)

    with pytest.raises(RuntimeError, match="index busy"):
        jobs.run_reprocess(job.pk)

    job.refresh_from_db()
    assert job.status == DocumentJob.Status.COMMITTED
    assert job.result["followup"]
    assert (jobs.job_directory(job.pk) / job.input["staged_name"]).exists()
    backend.batch_update.return_value.__enter__.side_effect = None

    jobs.run_reprocess(job.pk)

    parser.parse.assert_called_once()
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.SUCCEEDED


def test_cleanup_failure_retries_only_cleanup(
    sample_doc: Document,
    parser: type,
    mocker: MockerFixture,
) -> None:
    """Failure deleting staging cannot turn durable success into another update."""
    job = jobs.stage_reprocess(sample_doc.pk)
    cleanup = mocker.patch("documents.jobs.shutil.rmtree", side_effect=OSError("busy"))

    with pytest.raises(OSError, match="busy"):
        jobs.run_reprocess(job.pk)
    mocker.stop(cleanup)

    job.refresh_from_db()
    assert job.status == DocumentJob.Status.SUCCEEDED
    assert not job.cleaned
    finish = mocker.patch("documents.jobs._finish")

    jobs.run_reprocess(job.pk)

    finish.assert_not_called()
    parser.parse.assert_called_once()
    job.refresh_from_db()
    assert job.cleaned
    assert not jobs.job_directory(job.pk).exists()


def test_invalid_output_is_rejected_before_publication(
    sample_doc: Document,
    parser: type,
    tmp_path: Path,
) -> None:
    """Files pretending to be PDF output must not replace a valid archive."""
    invalid = tmp_path / "invalid.pdf"
    invalid.write_text("not a PDF")
    parser.get_archive_path.return_value = invalid
    job = jobs.stage_reprocess(sample_doc.pk)
    archive_bytes = sample_doc.archive_path.read_bytes()

    with pytest.raises(jobs.RejectedResult, match="not application/pdf"):
        jobs.run_reprocess(job.pk)

    job.refresh_from_db()
    sample_doc.refresh_from_db()
    assert job.status == DocumentJob.Status.REJECTED
    assert sample_doc.content == "test content"
    assert sample_doc.archive_path.read_bytes() == archive_bytes
    assert (jobs.job_directory(job.pk) / job.input["staged_name"]).exists()


def test_failed_dispatch_remains_available_for_recovery(
    sample_doc: Document,
    mocker: MockerFixture,
) -> None:
    """The job and input exist before a broker failure, and the sweep republishes it."""
    send = mocker.patch(
        "documents.tasks.update_document_content_maybe_archive_file.apply_async",
        side_effect=RuntimeError("broker unavailable"),
    )
    job = jobs.submit_reprocess(sample_doc.pk)
    assert job.status == DocumentJob.Status.QUEUED
    assert (jobs.job_directory(job.pk) / job.input["staged_name"]).exists()
    send.assert_called_once()
    send.reset_mock(side_effect=True)
    DocumentJob.objects.filter(pk=job.pk).update(
        updated=timezone.now() - timedelta(minutes=6),
    )

    jobs.recover_document_jobs()

    send.assert_called_once()
    assert send.call_args.kwargs["kwargs"] == {
        "document_id": sample_doc.pk,
        "job_id": str(job.pk),
    }
    assert send.call_args.kwargs["task_id"] == str(job.pk)


def test_media_contention_rolls_back_application_and_reuses_prepared_result(
    sample_doc: Document,
    parser: type,
    mocker: MockerFixture,
) -> None:
    """A busy media lock after DB locking must release the transaction for retry."""
    job = jobs.stage_reprocess(sample_doc.pk)
    competing_lock = FileLock(settings.MEDIA_LOCK)
    save = DocumentJob.save

    def occupy_media_after_manifest(instance: DocumentJob, *args, **kwargs) -> None:
        """Represent another writer taking media between planning and applying."""
        save(instance, *args, **kwargs)
        if (
            kwargs.get("update_fields") == ["result"]
            and "archive_filename" in instance.result
        ):
            competing_lock.acquire(timeout=0)

    hook = mocker.patch.object(DocumentJob, "save", occupy_media_after_manifest)
    try:
        with pytest.raises(Timeout):
            jobs.run_reprocess(job.pk)
    finally:
        competing_lock.release()
        mocker.stop(hook)

    job.refresh_from_db()
    sample_doc.refresh_from_db()
    assert job.status == DocumentJob.Status.PREPARED
    assert sample_doc.content == "test content"
    assert sample_doc.content_job is None
    assert not (settings.ARCHIVE_DIR / job.result["archive_filename"]).exists()

    jobs.run_reprocess(job.pk)

    job.refresh_from_db()
    assert job.status == DocumentJob.Status.SUCCEEDED
    parser.parse.assert_called_once()


@pytest.mark.parametrize("delete", [False, True])
def test_index_completion_reads_state_after_acquiring_writer(
    sample_doc: Document,
    parser: type,
    mocker: MockerFixture,
    *,
    delete: bool,
) -> None:
    """Waiting for the index writer cannot resurrect an old title or deleted root."""
    backend = mocker.patch("documents.search.get_backend").return_value
    writer = Mock()

    def acquire_writer() -> Mock:
        """Finish a concurrent change before granting the index writer."""
        if delete:
            sample_doc.delete()
        else:
            Document.objects.filter(pk=sample_doc.pk).update(title="newer title")
        return writer

    backend.batch_update.return_value.__enter__.side_effect = acquire_writer
    job = jobs.stage_reprocess(sample_doc.pk)

    jobs.run_reprocess(job.pk)

    if delete:
        writer.remove.assert_called_once_with(sample_doc.pk)
        writer.add_or_update.assert_not_called()
    else:
        writer.add_or_update.assert_called_once()
        assert writer.add_or_update.call_args.args[0].title == "newer title"
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.SUCCEEDED


def test_concurrent_delivery_is_ignored_without_reporting_success(
    sample_doc: Document,
    parser: type,
) -> None:
    """A duplicate cannot parse or report completion while another owner holds it."""
    from celery.exceptions import Ignore

    from documents.tasks import update_document_content_maybe_archive_file

    job = jobs.stage_reprocess(sample_doc.pk)
    with FileLock(jobs.job_directory(job.pk).with_suffix(".lock")):
        with pytest.raises(Ignore):
            update_document_content_maybe_archive_file(
                sample_doc.pk,
                job_id=str(job.pk),
            )
    parser.parse.assert_not_called()
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.QUEUED


def test_legacy_delivery_reuses_admission_id(
    sample_doc: Document,
    parser: type,
) -> None:
    """Old task messages with a stable delivery ID cannot create a second job."""
    from uuid import uuid4

    from documents.tasks import update_document_content_maybe_archive_file

    task_id = str(uuid4())
    for _ in range(2):
        update_document_content_maybe_archive_file.apply(
            args=[sample_doc.pk],
            task_id=task_id,
            throw=True,
        ).get()
    assert DocumentJob.objects.count() == 1
    assert DocumentJob.objects.get().pk == UUID(task_id)
    parser.parse.assert_called_once()


@pytest.mark.parametrize("removed", [False, True])
def test_default_parser_rechecks_selected_instance_before_publication(
    sample_doc: Document,
    parser: type,
    mocker: MockerFixture,
    *,
    removed: bool,
) -> None:
    """The stable default wrapper cannot hide a changed underlying parser."""
    job = jobs.stage_reprocess(sample_doc.pk)
    stopped = mocker.patch("documents.jobs._apply", side_effect=WorkerStopped)
    with pytest.raises(WorkerStopped):
        jobs.run_reprocess(job.pk)
    mocker.stop(stopped)
    if removed:
        mocker.patch(
            "paperless.parsers.providers.get_parser_registry"
        ).return_value.get_parser_for_file.return_value = None
    else:
        parser.version = "2.0"
    with pytest.raises(jobs.RejectedResult, match="Selected parser changed"):
        jobs.run_reprocess(job.pk)
    parser.parse.assert_called_once()
    sample_doc.refresh_from_db()
    assert sample_doc.content == "test content"
