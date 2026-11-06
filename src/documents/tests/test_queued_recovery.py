"""Queued results retain permission and uncertain-effect boundaries."""

from types import SimpleNamespace

import pytest
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied

from documents.models import DocumentJob
from documents.search_operations import run_search
from documents.search_operations import submit_search
from documents.server_processor import process
from paperless_testing.factories import UserFactory

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("paperless_dirs"),
]


@pytest.fixture
def queued_search(mocker):
    """Admit search work without contacting a broker or real search engine."""
    provider = SimpleNamespace(identity="search:1", configuration={}, replay_safe=True)
    mocker.patch(
        "documents.search.get_backend", return_value=SimpleNamespace(provider=provider)
    )
    mocker.patch("documents.search_operations.run_search.apply_async")
    mocker.patch("documents.jobs._dispatch")
    execute = mocker.patch("documents.search_operations._execute")
    processor = UserFactory()
    processor.user_permissions.add(Permission.objects.get(codename="process_search"))
    return provider, execute, processor


def test_grant_revoked_after_submission_rejects_application(queued_search):
    """A staged receipt does not retain a revoked capability grant."""
    provider, execute, processor = queued_search
    execute.return_value = None
    job = submit_search(provider, "wipe", {})
    run_search(str(job.pk))
    process(job.pk, "maintenance", processor)
    processor.user_permissions.clear()
    with pytest.raises(PermissionDenied):
        run_search(str(job.pk))
    job.refresh_from_db()
    assert job.result["invocations"]["maintenance"]["status"] == "conflicted"


@pytest.mark.parametrize("safe", [True, False])
def test_server_exception_preserves_effect_uncertainty(
    queued_search, safe, processor_notifications
):
    """Only replay-safe failures return to the pending queue."""
    provider, execute, processor = queued_search
    provider.replay_safe = safe
    execute.side_effect = RuntimeError("processor failed")
    job = submit_search(provider, "wipe", {})
    run_search(str(job.pk))
    processor_notifications.push.reset_mock()
    with pytest.raises(RuntimeError, match="processor failed"):
        process(job.pk, "maintenance", processor)
    job.refresh_from_db()
    entry = job.result["invocations"]["maintenance"]
    assert entry["status"] == ("pending" if safe else "unknown")
    assert entry["attempts"] == 1
    assert job.status == (
        DocumentJob.Status.WAITING if safe else DocumentJob.Status.ATTENTION
    )
    assert processor_notifications.push.call_count == int(safe)
