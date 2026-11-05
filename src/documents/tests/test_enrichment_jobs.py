"""Metadata application preserves edits and completes its derived state once."""

import pytest

from documents.enrichment_jobs import run_enrichment
from documents.enrichment_jobs import submit_enrichment
from documents.models import DocumentJob
from paperless_testing.factories import CorrespondentFactory
from paperless_testing.factories import StoragePathFactory

pytestmark = pytest.mark.django_db(transaction=True)


class Metadata:
    """A local implementation returning one proposed storage path."""

    identity = ("metadata:1", "model:1")
    configuration = {}
    calls = 0

    def classify(self, request):
        """Return only values explicitly admitted in the candidate snapshot."""
        type(self).calls += 1
        return {
            "version": 1,
            "revision": request["revision"],
            "provider": self.identity[0],
            "model": self.identity[1],
            "assignments": {
                field: [values[0]["id"]] if values else []
                for field, values in request["candidates"].items()
            },
        }


class ActivatedMetadata(Metadata):
    """An immutable loaded model whose active replacement changes during compute."""

    current_identity = Metadata.identity

    def classify(self, request):
        """Simulate activation after inference loaded the old immutable model."""
        result = super().classify(request)
        type(self).current_identity = ("metadata:1", "model:2")
        return result


class RetryMetadata(Metadata):
    """A replay-safe computation that fails before returning its first proposal."""

    failed = False

    def classify(self, request):
        """Simulate a temporary processor failure without changing the document."""
        if not type(self).failed:
            type(self).failed = True
            raise RuntimeError("Processor unavailable")
        return super().classify(request)


@pytest.fixture
def metadata(settings, mocker):
    """Select an immediate implementation with no external services."""
    settings.CLASSIFIER_ENABLED = True
    settings.CLASSIFICATION_PROVIDER = "documents.tests.test_enrichment_jobs.Metadata"
    Metadata.calls = 0
    mocker.patch("documents.jobs._dispatch")
    mocker.patch("documents.search.get_backend")


def test_metadata_reconciles_files_after_committed_retry(sample_doc, metadata, mocker):
    """A failed file move resumes completion without recomputing or reapplying."""
    storage = StoragePathFactory(path="enriched/{title}", matching_algorithm=6)
    original = sample_doc.source_path
    original_bytes = original.read_bytes()
    job = submit_enrichment(sample_doc.pk)
    from documents.file_handling import reconcile_document_files

    reconcile = mocker.patch(
        "documents.file_handling.reconcile_document_files",
        side_effect=OSError("disk temporarily unavailable"),
    )
    with pytest.raises(OSError, match="temporarily"):
        run_enrichment(str(job.pk))
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.COMMITTED
    assert Metadata.calls == 1
    reconcile.side_effect = reconcile_document_files
    run_enrichment(str(job.pk))
    sample_doc.refresh_from_db()
    job.refresh_from_db()
    assert sample_doc.storage_path_id == storage.pk
    assert sample_doc.source_path != original
    assert sample_doc.source_path.read_bytes() == original_bytes
    assert job.status == DocumentJob.Status.SUCCEEDED
    assert job.cleaned
    assert Metadata.calls == 1


def test_active_model_change_rejects_loaded_model_result(
    sample_doc, metadata, settings
):
    """A model activation during inference invalidates the older proposal."""
    settings.CLASSIFICATION_PROVIDER = (
        "documents.tests.test_enrichment_jobs.ActivatedMetadata"
    )
    job = submit_enrichment(sample_doc.pk)
    from documents.jobs import RejectedResult

    with pytest.raises(RejectedResult, match="provider changed"):
        run_enrichment(str(job.pk))
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.REJECTED


def test_metadata_preserves_existing_assignment(sample_doc, metadata):
    """Automatic optional enrichment fills gaps without replacing rule assignments."""
    assigned = CorrespondentFactory()
    CorrespondentFactory(matching_algorithm=6)
    sample_doc.correspondent = assigned
    sample_doc.save()
    job = submit_enrichment(sample_doc.pk)
    run_enrichment(str(job.pk))
    sample_doc.refresh_from_db()
    assert sample_doc.correspondent_id == assigned.pk


def test_metadata_retries_temporary_computation(sample_doc, metadata, settings):
    """A temporary inference failure remains recoverable within its attempt budget."""
    settings.CLASSIFICATION_PROVIDER = (
        "documents.tests.test_enrichment_jobs.RetryMetadata"
    )
    RetryMetadata.failed = False
    job = submit_enrichment(sample_doc.pk)
    with pytest.raises(RuntimeError, match="unavailable"):
        run_enrichment(str(job.pk))
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.WAITING
    run_enrichment(str(job.pk))
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.SUCCEEDED
    assert job.result["invocations"]["metadata"]["attempts"] == 2


@pytest.fixture(autouse=True)
def local_processors(monkeypatch, request):
    """Drive separately claimed inference before checking authoritative changes."""
    import sys
    from documents import enrichment_jobs
    from paperless_testing.factories import UserFactory
    from paperless_testing.queued import drive_processors

    if "retagger" in request.node.name:
        return
    processor = UserFactory(is_superuser=True)
    runner = drive_processors(enrichment_jobs.run_enrichment, processor)
    monkeypatch.setattr(enrichment_jobs, "run_enrichment", runner)
    monkeypatch.setattr(sys.modules[__name__], "run_enrichment", runner)
    monkeypatch.setattr(ActivatedMetadata, "current_identity", Metadata.identity)


@pytest.mark.parametrize("overwrite", [False, True])
def test_queued_retagger_preserves_selected_targets(sample_doc, metadata, overwrite):
    """Queued retagging applies requested replacements only after validation."""
    from documents.server_processor import process
    from paperless_testing.factories import UserFactory

    assigned = CorrespondentFactory()
    proposed = CorrespondentFactory(matching_algorithm=6)
    sample_doc.correspondent = assigned
    sample_doc.save()
    job = submit_enrichment(
        sample_doc.pk,
        retagger={
            "correspondent": True,
            "document_type": False,
            "tags": False,
            "storage_path": False,
            "overwrite": overwrite,
            "use_first": False,
        },
    )
    run_enrichment(str(job.pk))
    sample_doc.refresh_from_db()
    assert sample_doc.correspondent_id == assigned.pk
    process(job.pk, "metadata", UserFactory(is_superuser=True))
    run_enrichment(str(job.pk))
    sample_doc.refresh_from_db()
    assert sample_doc.correspondent_id == (proposed.pk if overwrite else assigned.pk)
    assert Metadata.calls == 1


def test_queued_retagger_rejects_changed_rules(sample_doc, metadata):
    """A rule edit while work is queued invalidates the retained application."""
    from documents.server_processor import process
    from documents.job_state import RejectedResult
    from paperless_testing.factories import UserFactory

    rule = CorrespondentFactory(match="old", matching_algorithm=1)
    job = submit_enrichment(
        sample_doc.pk,
        retagger={
            "correspondent": True,
            "document_type": False,
            "tags": False,
            "storage_path": False,
            "overwrite": True,
            "use_first": False,
        },
    )
    run_enrichment(str(job.pk))
    process(job.pk, "metadata", UserFactory(is_superuser=True))
    rule.match = "new"
    rule.save()
    with pytest.raises(RejectedResult):
        run_enrichment(str(job.pk))


def test_retagger_command_queues_selected_targets(sample_doc, metadata, mocker):
    """The CLI admits durable work without computing or assigning metadata."""
    from django.core.management import call_command

    classify = mocker.spy(Metadata, "classify")
    call_command(
        "document_retagger",
        correspondent=True,
        overwrite=True,
        id_range=[sample_doc.pk, sample_doc.pk],
    )
    job = DocumentJob.objects.get(kind="enrich")
    assert job.input["retagger"] == {
        "correspondent": True,
        "document_type": False,
        "tags": False,
        "storage_path": False,
        "overwrite": True,
        "use_first": False,
    }
    classify.assert_not_called()
