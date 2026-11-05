"""Deferred search writes retain inputs and reject stale completion receipts."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.contrib.auth.models import Permission

from documents.deferred_operations import claim
from documents.deferred_operations import submit_result
from documents.ingestion_state import NeedsAttention
from documents.models import DocumentJob
from documents.search import SearchService
from documents.search_operations import run_search
from paperless_testing.factories import DocumentFactory
from paperless_testing.factories import UserFactory

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("paperless_dirs"),
]


@pytest.fixture
def remote_search(settings, mocker):
    """Select an effectful remote provider without loading a local search engine."""
    processor = UserFactory()
    settings.SEARCH_PROVIDER = "tests.RemoteSearch"
    processor.user_permissions.add(Permission.objects.get(codename="process_search"))
    provider = SimpleNamespace(
        identity="remote-search-v1",
        configuration={"collection": "documents"},
        batch_update=Mock(),
    )
    service = SearchService(provider)
    mocker.patch("documents.search.get_backend", return_value=service)
    mocker.patch("documents.search_operations.run_search.apply_async")
    mocker.patch("documents.jobs._dispatch")
    return service, processor


def test_remote_write_waits_and_applies_receipt_once(remote_search):
    """Duplicate delivery neither executes locally nor consumes a remote claim."""
    service, processor = remote_search
    document = DocumentFactory()
    service.add_or_update(document)
    job = DocumentJob.objects.get(kind="search")
    run_search(str(job.pk))
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.WAITING
    invocation = job.result["invocations"]["maintenance"]
    assert "attempts" not in invocation
    run_search(str(job.pk))
    leased = claim(job.pk, "maintenance", processor)
    assert leased["attempts"] == 1
    assert leased["input"]["records"][0]["id"] == [document.pk]
    submit_result(job.pk, "maintenance", processor, leased["token"], None)
    run_search(str(job.pk))
    run_search(str(job.pk))
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.SUCCEEDED
    assert job.result["invocations"]["maintenance"]["id"] == invocation["id"]
    service.provider.batch_update.assert_not_called()


@pytest.mark.parametrize("change", ["content", "owner", "deleted"])
def test_remote_write_rejects_changed_authoritative_record(remote_search, change):
    """Pending content and visibility cannot replace newer authoritative facts."""
    service, processor = remote_search
    document = DocumentFactory()
    service.add_or_update(document)
    job = DocumentJob.objects.get(kind="search")
    run_search(str(job.pk))
    leased = claim(job.pk, "maintenance", processor)
    if change == "deleted":
        document.delete()
    elif change == "owner":
        document.owner = UserFactory()
        document.save(update_fields=["owner"])
    else:
        document.content = "New authoritative content"
        document.save(update_fields=["content"])
    submit_result(job.pk, "maintenance", processor, leased["token"], None)
    with pytest.raises(NeedsAttention, match="Search input changed"):
        run_search(str(job.pk))
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.ATTENTION
    service.provider.batch_update.assert_not_called()
