"""Provider declarations route finite work without changing interactive contracts."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.contrib.auth.models import Permission

from documents.deferred_operations import claim
from documents.deferred_operations import submit_result
from documents.enrichment_jobs import run_enrichment
from documents.enrichment_jobs import submit_enrichment
from documents.job_state import RejectedResult
from documents.models import DocumentJob
from documents.operation_journal import NeedsAttention
from documents.search_operations import run_search
from documents.search_operations import submit_search
from paperless_testing.factories import UserFactory

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("paperless_dirs"),
]


class ExternalMetadata:
    """Keep inference usable while admitting durable calls to an external host."""

    identity = ("external-metadata:1", "model:1")
    configuration = {}

    def classify(self, request):
        """Return proposals through the same implementation on either host."""
        return {
            "version": 1,
            "revision": request["revision"],
            "provider": self.identity[0],
            "model": self.identity[1],
            "assignments": {field: [] for field in request["candidates"]},
        }


@pytest.mark.parametrize("operation", ["write", "rebuild", "optimize", "wipe"])
def test_external_search_declaration_routes_maintenance(settings, mocker, operation):
    """Every finite maintenance boundary waits without an operation-name map."""
    provider = SimpleNamespace(identity="search:1", configuration={})
    processor = UserFactory()
    mocker.patch(
        "documents.search.get_backend", return_value=SimpleNamespace(provider=provider)
    )
    mocker.patch("documents.search_operations.run_search.apply_async")
    mocker.patch("documents.jobs._dispatch")
    execute = mocker.patch("documents.search_operations._execute")
    inputs = {"records": [], "remove": [], "writer_heap_bytes": 1}
    job = submit_search(provider, operation, inputs)
    run_search(str(job.pk))
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.WAITING
    processor.user_permissions.add(Permission.objects.get(codename="process_search"))
    lease = claim(job.pk, "maintenance", processor)
    submit_result(job.pk, "maintenance", processor, lease["token"], None)
    run_search(str(job.pk))
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.SUCCEEDED
    execute.assert_not_called()


def test_external_metadata_uses_existing_result_contract(sample_doc, settings, mocker):
    """A capability grant can complete inference without local computation."""
    settings.CLASSIFIER_ENABLED = True
    settings.CLASSIFICATION_PROVIDER = (
        "documents.tests.test_provider_execution_boundaries.ExternalMetadata"
    )
    processor = UserFactory()
    mocker.patch("documents.jobs._dispatch")
    mocker.patch("documents.search.get_backend")
    compute = mocker.patch.object(ExternalMetadata, "classify", Mock())
    job = submit_enrichment(sample_doc.pk)
    run_enrichment(str(job.pk))
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.WAITING
    compute.assert_not_called()

    processor.user_permissions.add(Permission.objects.get(codename="process_metadata"))
    lease = claim(job.pk, "metadata", processor)
    request = lease["input"]
    result = {
        "version": 1,
        "revision": request["revision"],
        "provider": ExternalMetadata.identity[0],
        "model": ExternalMetadata.identity[1],
        "assignments": {field: [] for field in request["candidates"]},
    }
    submit_result(job.pk, "metadata", processor, lease["token"], result)
    run_enrichment(str(job.pk))
    run_enrichment(str(job.pk))
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.SUCCEEDED
    compute.assert_not_called()


def test_search_rejects_configuration_change_before_first_attempt(settings, mocker):
    """A queued external operation cannot silently become local computation."""
    provider = SimpleNamespace(identity="search:1", configuration={})
    mocker.patch(
        "documents.search.get_backend", return_value=SimpleNamespace(provider=provider)
    )
    mocker.patch("documents.search_operations.run_search.apply_async")
    execute = mocker.patch("documents.search_operations._execute")
    job = submit_search(provider, "optimize", {})
    provider.configuration = {"changed": True}
    with pytest.raises(NeedsAttention, match="provider configuration"):
        run_search(str(job.pk))
    execute.assert_not_called()


def test_metadata_rejects_configuration_change_before_first_attempt(
    sample_doc, settings, mocker
):
    """Metadata admission retains the declaration used by chained jobs too."""
    settings.CLASSIFIER_ENABLED = True
    settings.CLASSIFICATION_PROVIDER = (
        "documents.tests.test_provider_execution_boundaries.ExternalMetadata"
    )
    mocker.patch("documents.jobs._dispatch")
    compute = mocker.patch.object(ExternalMetadata, "classify")
    job = submit_enrichment(sample_doc.pk)
    mocker.patch.object(ExternalMetadata, "configuration", {"changed": True})
    with pytest.raises(RejectedResult, match="provider changed"):
        run_enrichment(str(job.pk))
    compute.assert_not_called()
