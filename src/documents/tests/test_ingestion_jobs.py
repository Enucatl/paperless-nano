"""Ingestion keeps recovery inputs and never blindly repeats external effects."""

from __future__ import annotations

import datetime
import shutil
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from celery import signature
from celery.exceptions import Ignore

from documents import ingestion_jobs
from documents import ingestion_storage
from documents import tasks
from documents.consumer import ConsumerError
from documents.data_models import ConsumableDocument
from documents.data_models import DocumentMetadataOverrides
from documents.data_models import DocumentSource
from documents.jobs import RejectedResult
from documents.jobs import job_directory
from documents.models import Document
from documents.models import DocumentJob
from documents.models import Workflow
from documents.models import WorkflowAction
from documents.models import WorkflowRun
from documents.models import WorkflowTrigger
from paperless_testing.factories import UserFactory
from paperless_testing.fakes.progress import FakeProgressManager
from paperless_testing.permissions import grant_global

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_django.fixtures import Settings
    from pytest_mock import MockerFixture

    from paperless_testing.dirs import PaperlessDirs

pytestmark = pytest.mark.django_db(transaction=True)


class WorkerStopped(BaseException):
    """Terminate at a crash point without ordinary exception cleanup."""


@pytest.fixture
def incoming(
    paperless_dirs: PaperlessDirs,
    simple_digital_pdf_file: Path,
) -> ConsumableDocument:
    """An upload initially belongs to the web process's scratch directory."""
    source = paperless_dirs.scratch_dir / "incoming.pdf"
    shutil.copy2(simple_digital_pdf_file, source)
    return ConsumableDocument(DocumentSource.ApiUpload, original_file=source)


@pytest.fixture
def parser(
    mocker: MockerFixture,
    simple_digital_pdf_file: Path,
    thumbnail_webp_file: Path,
) -> type:
    """Run the real plugin pipeline with cheap, valid parser output."""

    class Parser:
        """An existing parser contract backed by valid sample files."""

        name = "Ingestion test parser"
        version = "1.0"
        can_produce_archive = True
        requires_pdf_rendition = False
        configure = Mock()
        parse = Mock()
        get_text = Mock(return_value="durable ingestion content")
        get_date = Mock(return_value=datetime.datetime(2024, 1, 1))
        get_page_count = Mock(return_value=1)
        get_thumbnail = Mock(return_value=thumbnail_webp_file)
        get_archive_path = Mock(return_value=simple_digital_pdf_file)

        def __enter__(self) -> Parser:
            return self

        def __exit__(self, *args) -> None:
            pass

    mocker.patch("documents.plugins.helpers.ProgressManager", FakeProgressManager)
    mocker.patch("documents.ingestion.ProgressManager", FakeProgressManager)
    mocker.patch("documents.consumer.load_classifier", return_value=None)
    mocker.patch(
        "paperless.parsers.providers.get_parser_registry",
    ).return_value.get_parser_for_file.return_value = Parser
    mocker.patch("documents.search.get_backend")
    return Parser


def _retained_bytes(job: DocumentJob, expected: bytes) -> bool:
    """Find the original input regardless of the journal's internal path layout."""
    return any(
        path.read_bytes() == expected
        for path in job_directory(job.pk).rglob("*")
        if path.is_file()
    )


def test_worker_uses_staged_upload_after_web_scratch_disappears(
    incoming: ConsumableDocument,
    parser: type,
    settings: Settings,
    tmp_path: Path,
) -> None:
    """Web and worker scratch directories need not survive or match."""
    original_bytes = incoming.original_file.read_bytes()
    job = ingestion_jobs.stage_consumption(incoming)
    shutil.rmtree(incoming.original_file.parent)
    settings.SCRATCH_DIR = tmp_path / "worker-scratch"
    settings.SCRATCH_DIR.mkdir()

    result = ingestion_jobs.run_consumption(job.pk)

    document = Document.objects.get(pk=result["document_id"])
    assert document.source_path.read_bytes() == original_bytes
    assert document.original_filename == "incoming.pdf"
    assert document.content == "durable ingestion content"
    parser.parse.assert_called_once()
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.SUCCEEDED


def test_duplicate_delivery_does_not_repeat_document_or_workflow(
    incoming: ConsumableDocument,
    parser: type,
    mocker: MockerFixture,
) -> None:
    """A completed receipt returns the first result without rerunning consumption."""
    trigger = WorkflowTrigger.objects.create(
        type=WorkflowTrigger.WorkflowTriggerType.DOCUMENT_ADDED,
    )
    action = WorkflowAction.objects.create(assign_title="workflow title")
    workflow = Workflow.objects.create(name="Once after ingestion")
    workflow.triggers.add(trigger)
    workflow.actions.add(action)
    pre_script = mocker.patch(
        "documents.consumer.ConsumerPlugin.run_pre_consume_script"
    )
    job = ingestion_jobs.stage_consumption(incoming)

    result = ingestion_jobs.run_consumption(job.pk)
    repeated = ingestion_jobs.run_consumption(str(job.pk))

    assert repeated == result
    assert Document.objects.count() == 1
    assert Document.objects.get(pk=result["document_id"]).title == "workflow title"
    assert WorkflowRun.global_objects.filter(workflow=workflow).count() == 1
    parser.parse.assert_called_once()
    pre_script.assert_called_once()


def test_changed_execution_configuration_retains_input_without_publication(
    incoming: ConsumableDocument,
    parser: type,
    settings: Settings,
) -> None:
    """A changed provider endpoint cannot be reported as the recorded execution."""
    job = ingestion_jobs.stage_consumption(incoming)
    original_endpoint = settings.TIKA_ENDPOINT
    parser.parse.side_effect = lambda *args, **kwargs: setattr(
        settings, "TIKA_ENDPOINT", "http://changed.invalid"
    )

    with pytest.raises(RejectedResult, match="configuration changed"):
        ingestion_jobs.run_consumption(job.pk)

    job.refresh_from_db()
    assert (
        job.configuration["settings"]["execution"]["TIKA_ENDPOINT"] == original_endpoint
    )
    assert not Document.objects.exists()
    assert (job_directory(job.pk) / "input").is_file()


def test_interrupted_running_job_requires_attention_without_replay(
    incoming: ConsumableDocument,
    parser: type,
) -> None:
    """Unknown external-effect outcomes cannot be retried as fresh ingestion."""
    original_bytes = incoming.original_file.read_bytes()
    job = ingestion_jobs.stage_consumption(incoming)
    parser.parse.side_effect = WorkerStopped

    with pytest.raises(WorkerStopped):
        ingestion_jobs.run_consumption(job.pk)
    with pytest.raises(ingestion_jobs.NeedsAttention):
        ingestion_jobs.run_consumption(job.pk)

    parser.parse.assert_called_once()
    assert not Document.objects.exists()
    assert _retained_bytes(job, original_bytes)
    job.refresh_from_db()
    assert job.status == "attention"


def test_crash_after_commit_does_not_repeat_consumption(
    incoming: ConsumableDocument,
    parser: type,
    mocker: MockerFixture,
) -> None:
    """Once the document commits, retry only resumes recorded completion work."""
    job = ingestion_jobs.stage_consumption(incoming)
    post_script = mocker.patch(
        "documents.ingestion_jobs.run_post_consume",
        side_effect=WorkerStopped,
    )
    with pytest.raises(WorkerStopped):
        ingestion_jobs.run_consumption(job.pk)
    mocker.stop(post_script)

    job.refresh_from_db()
    assert job.status == DocumentJob.Status.COMMITTED
    document = Document.objects.get()
    assert document.content == "durable ingestion content"
    result = ingestion_jobs.run_consumption(job.pk)

    assert result["document_id"] == document.pk
    assert Document.objects.count() == 1
    parser.parse.assert_called_once()


@pytest.mark.parametrize("state", ["pending", "started"])
def test_deleted_document_does_not_hide_uncertain_script_completion(
    incoming: ConsumableDocument,
    parser: type,
    mocker: MockerFixture,
    state: str,
) -> None:
    """Deletion cancels pending scripts but cannot resolve an interrupted effect."""
    job = ingestion_jobs.stage_consumption(incoming)
    stopped = mocker.patch(
        "documents.ingestion_jobs.run_post_consume", side_effect=WorkerStopped
    )
    with pytest.raises(WorkerStopped):
        ingestion_jobs.run_consumption(job.pk)
    mocker.stop(stopped)
    document = Document.objects.get()
    document.delete()
    job.refresh_from_db()
    job.result["post_consume"] = state
    job.save(update_fields=["result"])

    if state == "started":
        with pytest.raises(ingestion_jobs.NeedsAttention, match="may already have run"):
            ingestion_jobs.run_consumption(job.pk)
        job.refresh_from_db()
        assert job.status == DocumentJob.Status.ATTENTION
        assert (job_directory(job.pk) / "input").exists()
    else:
        ingestion_jobs.run_consumption(job.pk)
        job.refresh_from_db()
        assert job.result["post_consume"] == "cancelled"
        assert job.status == DocumentJob.Status.SUCCEEDED


@pytest.mark.parametrize("replace_shadow", [False, True])
def test_shadow_cleanup_preserves_replacements(
    incoming: ConsumableDocument,
    parser: type,
    *,
    replace_shadow: bool,
) -> None:
    """Retain legacy AppleDouble cleanup without deleting a later intake's file."""
    shadow = incoming.original_file.with_name(f"._{incoming.original_file.name}")
    shadow.write_bytes(b"original metadata")
    job = ingestion_jobs.stage_consumption(incoming)
    if replace_shadow:
        shadow.write_bytes(b"replacement metadata")

    ingestion_jobs.run_consumption(job.pk)

    assert shadow.exists() == replace_shadow
    if replace_shadow:
        assert shadow.read_bytes() == b"replacement metadata"


def test_replaced_source_is_not_deleted_on_success(
    incoming: ConsumableDocument,
    parser: type,
) -> None:
    """A new file at the old intake path belongs to a future job."""
    job = ingestion_jobs.stage_consumption(incoming)
    incoming.original_file.write_bytes(b"a newer upload at the same path")

    result = ingestion_jobs.run_consumption(job.pk)

    assert Document.objects.filter(pk=result["document_id"]).exists()
    assert incoming.original_file.read_bytes() == b"a newer upload at the same path"


def test_revoked_add_permission_prevents_application(
    incoming: ConsumableDocument,
    parser: type,
) -> None:
    """An authenticated upload does not retain authority after permission revocation."""
    user = UserFactory()
    grant_global(user, "add_document")
    original_bytes = incoming.original_file.read_bytes()
    job = ingestion_jobs.stage_consumption(
        incoming,
        DocumentMetadataOverrides(actor_id=user.pk, owner_id=user.pk),
    )
    parser.parse.side_effect = lambda *args, **kwargs: user.user_permissions.clear()

    with pytest.raises((ConsumerError, RejectedResult, ingestion_jobs.NeedsAttention)):
        ingestion_jobs.run_consumption(job.pk)

    assert not Document.objects.exists()
    assert _retained_bytes(job, original_bytes)
    job.refresh_from_db()
    assert job.status != DocumentJob.Status.SUCCEEDED


def test_publication_crash_keeps_input_and_does_not_create_a_second_document(
    incoming: ConsumableDocument,
    parser: type,
    mocker: MockerFixture,
) -> None:
    """Files published before a rollback remain recoverable without replaying hooks."""
    job = ingestion_jobs.stage_consumption(incoming)
    original_bytes = incoming.original_file.read_bytes()
    publish_file = ingestion_storage.publish_file

    def stop_after_publication(job_id: str, source: Path, target: Path) -> None:
        """Crash after the first durable file publication, before its DB commit."""
        publish_file(job_id, source, target)
        assert target.is_file()
        raise WorkerStopped

    interrupted = mocker.patch(
        "documents.ingestion_storage.publish_file",
        side_effect=stop_after_publication,
    )
    with pytest.raises(WorkerStopped):
        ingestion_jobs.run_consumption(job.pk)

    assert not Document.objects.exists()
    assert _retained_bytes(job, original_bytes)
    mocker.stop(interrupted)
    result = ingestion_jobs.run_consumption(job.pk)
    parser.parse.assert_called_once()
    assert Document.objects.count() == 1
    assert (
        Document.objects.get(pk=result["document_id"]).source_path.read_bytes()
        == original_bytes
    )


def test_version_upload_rejects_changed_root_before_creating_version(
    incoming: ConsumableDocument,
    sample_doc: Document,
    parser: type,
) -> None:
    """A version parsed for an old root revision cannot be applied after an edit."""
    incoming.root_document_id = sample_doc.pk
    job = ingestion_jobs.stage_consumption(incoming)
    parser.parse.side_effect = lambda *args, **kwargs: Document.objects.filter(
        pk=sample_doc.pk,
    ).update(content="newer root edit")

    with pytest.raises((ConsumerError, RejectedResult, ingestion_jobs.NeedsAttention)):
        ingestion_jobs.run_consumption(job.pk)

    sample_doc.refresh_from_db()
    assert sample_doc.content == "newer root edit"
    assert not Document.objects.filter(root_document=sample_doc).exists()
    assert Document.objects.count() == 1


def test_celery_admission_sends_only_identifier_and_retains_canvas(
    incoming: ConsumableDocument,
    mocker: MockerFixture,
) -> None:
    """All legacy call forms stage their input before the transport sees a message."""
    send = mocker.patch("celery.Task.apply_async")
    callback = signature("paperless_mail.tasks.test_completion", args=("mail-id",))
    chord = signature("paperless_mail.tasks.test_chord_completion")

    tasks.consume_file.apply_async(
        args=(incoming, DocumentMetadataOverrides(title="queued title")),
        link=[callback],
        chord=chord,
        group_id="group-id",
        group_index=2,
        root_id="root-task-id",
        parent_id="parent-task-id",
        headers={"trigger_source": "mail_fetch"},
    )

    job = DocumentJob.objects.get(kind="consume")
    send.assert_called_once()
    dispatched = send.call_args.kwargs
    assert dispatched["args"] == ()
    assert dispatched["kwargs"] == {"job_id": str(job.pk)}
    assert dispatched["task_id"] == str(job.pk)
    assert dispatched["link"][0].task == callback.task
    assert dispatched["chord"].task == chord.task
    assert dispatched["link"][0].options["task_id"]
    assert dispatched["chord"].options["task_id"]
    assert job.dispatch_options["link"][0]["task"] == callback.task
    assert job.dispatch_options["chord"]["task"] == chord.task
    assert job.dispatch_options["group_index"] == 2
    assert job.dispatch_options["root_id"] == "root-task-id"
    assert job.dispatch_options["parent_id"] == "parent-task-id"
    assert _retained_bytes(job, incoming.original_file.read_bytes())


def test_uncertain_callback_dispatch_cannot_repeat_native_callbacks(
    incoming: ConsumableDocument,
    parser: type,
) -> None:
    """A crash after callback intent is recorded requires explicit reconciliation."""
    job = ingestion_jobs.stage_consumption(
        incoming,
        dispatch_options={
            "link": [signature("paperless_mail.tasks.test_completion")],
        },
    )
    result = ingestion_jobs.run_consumption(job.pk)
    assert Document.objects.filter(pk=result["document_id"]).exists()
    job.refresh_from_db()
    assert job.result["delivery"] == "started"
    assert not job.cleaned

    with pytest.raises(ingestion_jobs.NeedsAttention, match="callbacks"):
        ingestion_jobs.run_consumption(job.pk)

    parser.parse.assert_called_once()
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.ATTENTION
    assert (job_directory(job.pk) / "input").exists()


def test_callback_acknowledgment_cleans_input_and_suppresses_redelivery(
    incoming: ConsumableDocument,
    parser: type,
) -> None:
    """Dispatch acknowledgment retains input until the callback durably completes."""
    callback = signature("paperless_mail.tasks.test_completion")
    callback_id = callback.freeze().id
    job = ingestion_jobs.stage_consumption(
        incoming,
        dispatch_options={
            "link": [callback],
        },
    )
    result = ingestion_jobs.run_consumption(job.pk)

    tasks.consume_file.after_return(
        "SUCCESS",
        result,
        str(job.pk),
        (),
        {"job_id": str(job.pk)},
        None,
    )

    job.refresh_from_db()
    assert job.result["delivery"] == "done"
    assert not job.cleaned
    assert job_directory(job.pk).exists()
    DocumentJob.objects.create(
        pk=callback_id,
        kind="callback",
        status=DocumentJob.Status.SUCCEEDED,
        timeout=60,
        cleaned=True,
    )
    with pytest.raises(Ignore):
        ingestion_jobs.run_consumption(job.pk)
    job.refresh_from_db()
    assert job.cleaned
    assert not job_directory(job.pk).exists()
    parser.parse.assert_called_once()


def test_native_failure_errback_is_not_dispatched_on_redelivery(
    incoming: ConsumableDocument,
    parser: type,
    mocker: MockerFixture,
) -> None:
    """Celery observes the first failure, then Ignore suppresses repeated errbacks."""
    errback = signature("paperless_mail.tasks.test_failure").set(immutable=True)
    job = ingestion_jobs.stage_consumption(
        incoming,
        dispatch_options={"link_error": [errback]},
    )
    parser.parse.side_effect = RuntimeError("parser failed")
    dispatch_errbacks = mocker.patch.object(
        tasks.consume_file.backend,
        "_call_task_errbacks",
    )

    first = tasks.consume_file.apply(
        kwargs={"job_id": str(job.pk)},
        task_id=str(job.pk),
        link_error=[errback],
        throw=False,
    )
    second = tasks.consume_file.apply(
        kwargs={"job_id": str(job.pk)},
        task_id=str(job.pk),
        link_error=[errback],
        throw=False,
    )

    assert first.state == "FAILURE"
    assert second.state == "IGNORED"
    dispatch_errbacks.assert_called_once()
    parser.parse.assert_called_once()
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.ATTENTION
    assert job.result["delivery"] == "failure-done"
