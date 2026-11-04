"""Crash boundaries retain facts, artifacts and opaque effect ownership."""

import json
from unittest.mock import Mock

import pytest
from celery.exceptions import Ignore
from django.db import transaction
from filelock import FileLock

from documents import ingestion
from documents import ingestion_jobs
from documents import ingestion_state
from documents.jobs import job_directory
from documents.models import Document
from documents.models import DocumentJob
from documents.plugins.base import AlwaysRunPluginMixin
from documents.plugins.base import NoCleanupPluginMixin
from documents.plugins.base import NoSetupPluginMixin
from documents.signals import document_consumption_finished
from documents.signals import document_consumption_started
from documents.tests.test_ingestion_jobs import WorkerStopped
from documents.tests.test_ingestion_jobs import incoming as incoming_fixture
from documents.tests.test_ingestion_jobs import parser as parser_fixture

incoming = incoming_fixture
parser = parser_fixture

pytestmark = pytest.mark.django_db(transaction=True)


def test_prepared_restart_does_not_repeat_parser(incoming, parser, mocker):
    """Prepared means all outputs survive a stopped worker, before document creation."""
    job = ingestion_jobs.stage_consumption(incoming)
    stopped = mocker.patch.object(ingestion, "publish", side_effect=WorkerStopped)
    with pytest.raises(WorkerStopped):
        ingestion_jobs.run_consumption(job.pk)
    job.refresh_from_db()
    assert job.status == "prepared"
    assert job.result["phase"] == "prepared"
    assert (
        job.result["parser_request"]["invocation_id"]
        == job.result["invocations"]["prepare:6"]["id"]
    )
    assert not Document.objects.exists()
    assert all(
        value["status"] == "succeeded" for value in job.result["invocations"].values()
    )
    mocker.stop(stopped)
    result = ingestion_jobs.run_consumption(job.pk)
    parser.parse.assert_called_once()
    assert (
        Document.objects.get(pk=result["document_id"]).content
        == "durable ingestion content"
    )


def test_setup_can_stop_ingestion_without_an_unknown_outcome(incoming, mocker):
    """A supported setup-time stop retains its result and runs tracked cleanup."""
    from documents.plugins.base import StopConsumeTaskError

    ran = Mock()
    cleaned = Mock()

    class Plugin(AlwaysRunPluginMixin, NoCleanupPluginMixin):
        def setup(self):
            raise StopConsumeTaskError("setup chose not to consume")

        def run(self):
            ran()

        def cleanup(self):
            cleaned()

    mocker.patch("documents.tasks.consumption_plugins", return_value=[Plugin])
    job = ingestion_jobs.stage_consumption(incoming)
    assert ingestion_jobs.run_consumption(job.pk) == {
        "reason": "setup chose not to consume"
    }
    job.refresh_from_db()
    assert job.status == "succeeded"
    assert job.cleaned
    assert incoming.original_file.exists()
    assert not Document.objects.exists()
    ran.assert_not_called()
    cleaned.assert_called_once()


def test_overlong_preparation_retains_outputs_without_publishing(
    incoming, parser, mocker
):
    """An expired attempt cannot publish; a fresh attempt reuses validated outcomes."""
    from datetime import timedelta

    from django.utils import timezone

    job = ingestion_jobs.stage_consumption(incoming)
    after_deadline = timezone.now() + timedelta(seconds=job.timeout + 1)
    clock = None

    def exceed_timeout(*args, **kwargs):
        nonlocal clock
        clock = mocker.patch(
            "documents.ingestion.timezone.now", return_value=after_deadline
        )

    parser.parse.side_effect = exceed_timeout
    with pytest.raises(TimeoutError, match="recorded timeout"):
        ingestion_jobs.run_consumption(job.pk)
    job.refresh_from_db()
    assert job.status == "running"
    assert job.result["phase"] == "preparing"
    assert "prepared" in job.result
    assert not Document.objects.exists()
    assert incoming.original_file.exists()
    mocker.stop(clock)
    result = ingestion_jobs.run_consumption(job.pk)
    assert Document.objects.get(pk=result["document_id"]).source_path.exists()
    parser.parse.assert_called_once()


def test_prepared_artifact_tampering_is_not_published(incoming, parser, mocker):
    """Retained preparation is validated again before permanent publication."""
    job = ingestion_jobs.stage_consumption(incoming)
    stopped = mocker.patch.object(ingestion, "publish", side_effect=WorkerStopped)
    with pytest.raises(WorkerStopped):
        ingestion_jobs.run_consumption(job.pk)
    mocker.stop(stopped)
    (job_directory(job.pk) / "thumbnail.webp").write_bytes(b"tampered")
    with pytest.raises(ValueError):
        ingestion_jobs.run_consumption(job.pk)
    assert not Document.objects.exists()
    parser.parse.assert_called_once()
    assert (job_directory(job.pk) / "input").exists()


def test_finished_signal_observes_committed_files(incoming, parser):
    """Extensions see a committed document with published files and a stable invocation."""
    observed = []

    def receiver(sender, document, invocation_id, **kwargs):
        observed.append(
            (
                document.pk,
                document.source_path.is_file(),
                transaction.get_connection().in_atomic_block,
                invocation_id,
            )
        )

    document_consumption_finished.connect(receiver)
    try:
        job = ingestion_jobs.stage_consumption(incoming)
        result = ingestion_jobs.run_consumption(job.pk)
        ingestion_jobs.run_consumption(job.pk)
    finally:
        document_consumption_finished.disconnect(receiver)
    assert len(observed) == 1
    assert observed[0][:3] == (result["document_id"], True, False)
    job.refresh_from_db()
    assert observed[0][3] == job.result["invocations"]["complete:signal"]["id"]


def test_started_signal_receives_its_durable_invocation_id(incoming, parser):
    """A started extension can identify the tracked invocation's external effects."""
    observed = []

    def receiver(sender, invocation_id, **kwargs):
        observed.append(invocation_id)

    document_consumption_started.connect(receiver)
    try:
        job = ingestion_jobs.stage_consumption(incoming)
        ingestion_jobs.run_consumption(job.pk)
    finally:
        document_consumption_started.disconnect(receiver)
    job.refresh_from_db()
    assert observed == [job.result["invocations"]["prepare:6:started_signal"]["id"]]


@pytest.mark.parametrize("failure", [RuntimeError, WorkerStopped])
def test_opaque_preparation_outcome_never_replays(incoming, mocker, failure):
    """Both a reported error and a crash can follow an irreversible external effect."""
    effect = Mock(side_effect=failure("external effect happened"))

    class Plugin(AlwaysRunPluginMixin, NoSetupPluginMixin, NoCleanupPluginMixin):
        def run(self):
            effect(self.invocation_id)

    mocker.patch("documents.tasks.consumption_plugins", return_value=[Plugin])
    job = ingestion_jobs.stage_consumption(incoming)
    with pytest.raises(failure):
        ingestion_jobs.run_consumption(job.pk)
    with pytest.raises(ingestion_jobs.NeedsAttention):
        ingestion_jobs.run_consumption(job.pk)
    effect.assert_called_once()
    job.refresh_from_db()
    entry = job.result["invocations"]["prepare:0"]
    assert entry["status"] == "unknown"
    assert effect.call_args.args == (entry["id"],)
    assert job.result["phase"] == "preparing"


def test_safe_invocation_retries_after_result_persistence_loss(incoming, mocker):
    """A declared replay-safe operation may recover when result recording was lost."""
    calls = []

    class Plugin(AlwaysRunPluginMixin, NoSetupPluginMixin, NoCleanupPluginMixin):
        replay_safe = True

        def run(self):
            calls.append(self.invocation_id)
            if len(calls) == 1:
                raise WorkerStopped
            from documents.plugins.base import StopConsumeTaskError

            raise StopConsumeTaskError("safe stop")

    mocker.patch("documents.tasks.consumption_plugins", return_value=[Plugin])
    job = ingestion_jobs.stage_consumption(incoming)
    with pytest.raises(WorkerStopped):
        ingestion_jobs.run_consumption(job.pk)
    # The invocation checkpoint itself is tested; this plugin intentionally supplies no document.
    mocker.patch.object(ingestion, "publish", side_effect=WorkerStopped)
    with pytest.raises(WorkerStopped):
        ingestion_jobs.run_consumption(job.pk)
    assert len(calls) == 2 and calls[0] == calls[1]
    job.refresh_from_db()
    assert job.status == "prepared"


def test_cleanup_failure_retries_without_recreating_document(incoming, parser, mocker):
    """Completed effects survive a failed final cleanup and remain non-replayable."""
    job = ingestion_jobs.stage_consumption(incoming)
    cleanup = mocker.patch(
        "documents.ingestion_storage.cleanup_publication",
        side_effect=OSError("cleanup failed"),
    )
    with pytest.raises(OSError):
        ingestion_jobs.run_consumption(job.pk)
    job.refresh_from_db()
    assert job.status == "succeeded"
    assert job.result["cleanup"] == "running"
    assert not job.cleaned
    mocker.stop(cleanup)
    ingestion_jobs.run_consumption(job.pk)
    job.refresh_from_db()
    assert job.cleaned
    assert Document.objects.count() == 1
    parser.parse.assert_called_once()


def test_concurrent_delivery_cannot_claim_a_locked_job(incoming, parser):
    """All supported databases serialize duplicate delivery at the shared job lock."""
    job = ingestion_jobs.stage_consumption(incoming)
    with FileLock(job_directory(job.pk).with_suffix(".lock")):
        with pytest.raises(Ignore):
            ingestion_jobs.run_consumption(job.pk)
    parser.parse.assert_not_called()
    assert not Document.objects.exists()


def test_storage_does_not_remove_replaced_target(incoming, parser, mocker):
    """A checksum match alone never establishes ownership of an unrelated file."""
    job = ingestion_jobs.stage_consumption(incoming)
    original_publish = ingestion_jobs.publish_file

    def stop(identifier, source, target):
        original_publish(identifier, source, target)
        raise WorkerStopped

    stopped = mocker.patch.object(ingestion_jobs, "publish_file", side_effect=stop)
    with pytest.raises(WorkerStopped):
        ingestion_jobs.run_consumption(job.pk)
    mocker.stop(stopped)
    entry = json.loads((job_directory(job.pk) / "publication.json").read_text())[0]
    from pathlib import Path

    target = Path(entry["target"])
    content = target.read_bytes()
    target.unlink()
    target.write_bytes(content)
    with pytest.raises(ingestion_jobs.NeedsAttention):
        ingestion_jobs.run_consumption(job.pk)
    assert target.read_bytes() == content
    assert not Document.objects.exists()


def test_illegal_lifecycle_transition_rejected(incoming):
    """An accepted input cannot claim completion before preparation and commit."""
    job = ingestion_jobs.stage_consumption(incoming)
    with pytest.raises(ValueError, match="Illegal ingestion transition"):
        ingestion_state.transition(job, "complete")


def test_reconciliation_uses_stable_invocation_without_repeating_effect(
    incoming, mocker
):
    """An opaque plugin may recover its own retained outcome explicitly."""
    calls = []

    class Plugin(AlwaysRunPluginMixin, NoSetupPluginMixin, NoCleanupPluginMixin):
        def run(self):
            calls.append(self.invocation_id)
            raise WorkerStopped

        def reconcile(self, invocation_id):
            job = DocumentJob.objects.get(pk=self.input_doc.job_id)
            entry = job.result["invocations"]["prepare:0"]
            assert invocation_id == entry["id"]
            return {**entry["input"], "stopped": {"reason": "externally reconciled"}}

    mocker.patch("documents.tasks.consumption_plugins", return_value=[Plugin])
    job = ingestion_jobs.stage_consumption(incoming)
    with pytest.raises(WorkerStopped):
        ingestion_jobs.run_consumption(job.pk)
    result = ingestion_jobs.run_consumption(job.pk)
    assert result == {"reason": "externally reconciled"}
    assert len(calls) == 1
    assert incoming.original_file.exists()


def test_result_recording_crash_keeps_opaque_effect_unknown(incoming, mocker):
    """Returning successfully is insufficient until the outcome commits."""
    effect = Mock()

    class Plugin(AlwaysRunPluginMixin, NoSetupPluginMixin, NoCleanupPluginMixin):
        def run(self):
            effect()

    mocker.patch("documents.tasks.consumption_plugins", return_value=[Plugin])
    save = DocumentJob.save

    def stop_before_result(instance, *args, **kwargs):
        if (
            instance.result.get("invocations", {}).get("prepare:0", {}).get("status")
            == "succeeded"
        ):
            raise WorkerStopped
        return save(instance, *args, **kwargs)

    stopped = mocker.patch.object(
        DocumentJob, "save", autospec=True, side_effect=stop_before_result
    )
    job = ingestion_jobs.stage_consumption(incoming)
    with pytest.raises(WorkerStopped):
        ingestion_jobs.run_consumption(job.pk)
    mocker.stop(stopped)
    with pytest.raises(ingestion_jobs.NeedsAttention):
        ingestion_jobs.run_consumption(job.pk)
    effect.assert_called_once()


def test_in_place_plugin_mutation_does_not_destroy_previous_checkpoint(
    incoming, parser, mocker
):
    """Each invocation gets a working copy, retaining earlier input identities."""
    from documents.consumer import ConsumerPlugin

    calls = []

    class Plugin(AlwaysRunPluginMixin, NoSetupPluginMixin, NoCleanupPluginMixin):
        def run(self):
            calls.append(self.invocation_id)
            # A harmless PDF trailing comment changes the source without invalidating it.
            with self.input_doc.original_file.open("ab") as output:
                output.write(b"\n% changed by plugin\n")

    mocker.patch(
        "documents.tasks.consumption_plugins", return_value=[Plugin, ConsumerPlugin]
    )
    parser.replay_safe = True
    stopped = mocker.patch.object(ConsumerPlugin, "prepare", side_effect=WorkerStopped)
    job = ingestion_jobs.stage_consumption(incoming)
    with pytest.raises(WorkerStopped):
        ingestion_jobs.run_consumption(job.pk)
    mocker.stop(stopped)
    result = ingestion_jobs.run_consumption(job.pk)
    assert len(calls) == 1
    assert (
        Document.objects.get(pk=result["document_id"])
        .source_path.read_bytes()
        .endswith(b"% changed by plugin\n")
    )


def test_missing_dispatch_remains_discoverable(incoming, mocker):
    """A broker outage does not lose an accepted input or its scheduled recovery."""
    from datetime import timedelta

    from django.utils import timezone

    from documents.jobs import recover_document_jobs

    send = mocker.patch("celery.Task.apply_async", side_effect=OSError("broker down"))
    from documents.tasks import consume_file

    with pytest.raises(OSError):
        consume_file.apply_async(args=(incoming,))
    job = DocumentJob.objects.get(kind="consume")
    assert job.result["phase"] == "accepted"
    assert (job_directory(job.pk) / "input").is_file()
    DocumentJob.objects.filter(pk=job.pk).update(
        updated=timezone.now() - timedelta(minutes=10)
    )
    mocker.stop(send)
    dispatch = mocker.patch("documents.ingestion_jobs.dispatch_consumption")
    recover_document_jobs()
    dispatch.assert_called_once()
    assert dispatch.call_args.args[0].pk == job.pk


def test_version_workflow_updates_root_and_reconciles_files(
    incoming, parser, sample_doc, settings
):
    """Version completion matches new effective content and owns root filename effects."""
    from documents.models import MatchingModel
    from documents.models import Workflow
    from documents.models import WorkflowAction
    from documents.models import WorkflowTrigger

    settings.FILENAME_FORMAT = "{title}"
    incoming.root_document_id = sample_doc.pk
    trigger = WorkflowTrigger.objects.create(
        type=WorkflowTrigger.WorkflowTriggerType.DOCUMENT_UPDATED,
        matching_algorithm=MatchingModel.MATCH_ANY,
        match="durable ingestion",
    )
    action = WorkflowAction.objects.create(assign_title="after-version")
    workflow = Workflow.objects.create(name="version completion")
    workflow.triggers.add(trigger)
    workflow.actions.add(action)
    before = sample_doc.modified
    job = ingestion_jobs.stage_consumption(incoming)
    result = ingestion_jobs.run_consumption(job.pk)
    sample_doc.refresh_from_db()
    version = Document.objects.get(pk=result["document_id"])
    assert version.root_document_id == sample_doc.pk
    assert sample_doc.modified > before
    assert sample_doc.title == "after-version"
    assert version.source_path.exists()
    assert "after-version" in version.filename


@pytest.mark.parametrize("email_failure", [False, True])
def test_added_workflow_email_uses_renamed_file_and_records_failure(
    incoming, parser, settings, mocker, email_failure
):
    """Post-commit workflow files stay current and ambiguous email is not success."""
    from documents.models import Workflow
    from documents.models import WorkflowAction
    from documents.models import WorkflowActionEmail
    from documents.models import WorkflowTrigger

    settings.FILENAME_FORMAT = "{title}"
    settings.EMAIL_ENABLED = True
    trigger = WorkflowTrigger.objects.create(
        type=WorkflowTrigger.WorkflowTriggerType.DOCUMENT_ADDED
    )
    for order, action in enumerate(
        [
            WorkflowAction.objects.create(assign_title="renamed-before-email"),
            WorkflowAction.objects.create(
                type=WorkflowAction.WorkflowActionType.EMAIL,
                email=WorkflowActionEmail.objects.create(
                    to="test@example.com", subject="Document", include_document=True
                ),
            ),
        ]
    ):
        workflow = Workflow.objects.create(name=f"completion {order}", order=order)
        workflow.triggers.add(trigger)
        workflow.actions.add(action)
    attachments = []

    def send(**kwargs):
        attachment = kwargs["attachments"][0]
        attachments.append(attachment.path)
        assert attachment.path.name == "renamed-before-email.pdf"
        assert attachment.path.read_bytes()
        if email_failure:
            raise OSError("email outcome unknown")
        return 1

    email = mocker.patch("documents.workflows.actions.send_email", side_effect=send)
    job = ingestion_jobs.stage_consumption(incoming)
    if email_failure:
        with pytest.raises(OSError, match="email outcome unknown"):
            ingestion_jobs.run_consumption(job.pk)
        with pytest.raises(ingestion_jobs.NeedsAttention):
            ingestion_jobs.run_consumption(job.pk)
    else:
        ingestion_jobs.run_consumption(job.pk)
    job.refresh_from_db()
    assert job.status == ("attention" if email_failure else "succeeded")
    assert job.result["invocations"]["complete:workflows"]["status"] == (
        "unknown" if email_failure else "succeeded"
    )
    assert Document.objects.get().source_path == attachments[0]
    email.assert_called_once()


def test_workflow_failure_keeps_committed_document_without_replay(
    incoming, parser, mocker
):
    """A failed opaque follow-up does not undo or recreate document creation."""
    effect = mocker.patch(
        "documents.workflows.runner.run_workflows",
        side_effect=RuntimeError("unknown external effect"),
    )
    job = ingestion_jobs.stage_consumption(incoming)
    with pytest.raises(RuntimeError):
        ingestion_jobs.run_consumption(job.pk)
    job.refresh_from_db()
    assert job.status == "attention"
    assert job.result["phase"] == "completing"
    assert Document.objects.count() == 1
    assert Document.objects.get().source_path.exists()
    with pytest.raises(ingestion_jobs.NeedsAttention):
        ingestion_jobs.run_consumption(job.pk)
    effect.assert_called_once()
    parser.parse.assert_called_once()


def test_admission_interruption_retains_source_and_marks_attention(incoming, mocker):
    """Incomplete input retention cannot be treated as accepted executable work."""
    from datetime import timedelta

    from django.utils import timezone

    from documents.jobs import recover_document_jobs

    mocker.patch.object(ingestion_jobs, "_copy", side_effect=WorkerStopped)
    with pytest.raises(WorkerStopped):
        ingestion_jobs.stage_consumption(incoming)
    job = DocumentJob.objects.get(kind="consume")
    assert job.status == "staging"
    assert incoming.original_file.exists()
    DocumentJob.objects.filter(pk=job.pk).update(
        updated=timezone.now() - timedelta(minutes=10)
    )
    recover_document_jobs()
    job.refresh_from_db()
    assert job.status == "attention"
