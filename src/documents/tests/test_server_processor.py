"""Server processors submit the same durable results as remote processors."""

from types import SimpleNamespace

import pytest

from documents.models import DocumentJob
from documents.search_operations import run_search
from documents.search_operations import submit_search
from documents.server_processor import process
from paperless_testing.factories import UserFactory

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("paperless_dirs"),
]


def test_server_stages_before_coordinator_applies(mocker):
    """Computation happens once; authoritative completion follows submission."""
    provider = SimpleNamespace(identity="search:1", configuration={})
    mocker.patch(
        "documents.search.get_backend", return_value=SimpleNamespace(provider=provider)
    )
    mocker.patch("documents.search_operations.run_search.apply_async")
    mocker.patch("documents.jobs._dispatch")
    execute = mocker.patch("documents.search_operations._execute", return_value=None)
    job = submit_search(provider, "wipe", {})
    run_search(str(job.pk))
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.WAITING
    execute.assert_not_called()
    processor = UserFactory(is_superuser=True)
    process(job.pk, "maintenance", processor)
    job.refresh_from_db()
    assert job.result["invocations"]["maintenance"]["status"] == "staged"
    assert job.result["invocations"]["maintenance"]["processor_location"] == "server"
    assert job.status != DocumentJob.Status.SUCCEEDED
    process(job.pk, "maintenance", processor)
    execute.assert_called_once()
    run_search(str(job.pk))
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.SUCCEEDED


def test_cancelled_work_never_executes_on_server(mocker):
    """Cancellation remains authoritative when a server later sees the job."""
    from documents.deferred_operations import cancel

    provider = SimpleNamespace(identity="search:1", configuration={})
    mocker.patch(
        "documents.search.get_backend", return_value=SimpleNamespace(provider=provider)
    )
    mocker.patch("documents.search_operations.run_search.apply_async")
    execute = mocker.patch("documents.search_operations._execute")
    job = submit_search(provider, "wipe", {})
    run_search(str(job.pk))
    processor = UserFactory(is_superuser=True)
    cancel(job.pk, processor)
    process(job.pk, "maintenance", processor)
    execute.assert_not_called()
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.CANCELLED


@pytest.mark.parametrize(
    "status",
    [DocumentJob.Status.WAITING, DocumentJob.Status.RUNNING, DocumentJob.Status.QUEUED],
)
def test_server_can_resume_expired_safe_claim(mocker, status):
    """An offline safe processor leaves bounded work available to the server."""
    from datetime import timedelta

    from django.utils import timezone

    from documents.deferred_operations import claim
    from documents.deferred_operations import recover_waiting

    provider = SimpleNamespace(identity="search:1", configuration={}, replay_safe=True)
    mocker.patch(
        "documents.search.get_backend", return_value=SimpleNamespace(provider=provider)
    )
    mocker.patch("documents.search_operations.run_search.apply_async")
    mocker.patch("documents.jobs._dispatch")
    execute = mocker.patch("documents.search_operations._execute", return_value=None)
    job = submit_search(provider, "optimize", {})
    run_search(str(job.pk))
    processor = UserFactory(is_superuser=True)
    claim(job.pk, "maintenance", processor)
    job.refresh_from_db()
    job.result["invocations"]["maintenance"]["lease_until"] = (
        timezone.now() - timedelta(seconds=1)
    ).isoformat()
    job.status = status
    job.save(update_fields=["result", "status"])
    recover_waiting()
    process(job.pk, "maintenance", processor)
    job.refresh_from_db()
    assert job.result["invocations"]["maintenance"]["status"] == "staged"
    assert job.result["invocations"]["maintenance"]["attempts"] == 2
    execute.assert_called_once()


def test_upgrade_quarantines_unfinished_outcomes():
    """An upgrade preserves evidence without silently claiming legacy work."""
    from importlib import import_module

    from django.apps import apps

    pending = DocumentJob.objects.create(
        kind="search",
        timeout=30,
        status=DocumentJob.Status.WAITING,
        result={"invocations": {"work": {"status": "leased", "token": "retained"}}},
    )
    done = DocumentJob.objects.create(
        kind="search",
        timeout=30,
        status=DocumentJob.Status.SUCCEEDED,
        result={"receipt": "preserved"},
    )
    import_module("documents.migrations.0031_queued_operations").quarantine_legacy(
        apps, None
    )
    pending.refresh_from_db()
    done.refresh_from_db()
    assert pending.status == DocumentJob.Status.ATTENTION
    assert pending.result["invocations"]["work"]["token"] == "retained"
    assert done.status == DocumentJob.Status.SUCCEEDED
    assert done.result == {"receipt": "preserved"}


def test_server_preserves_rejected_configuration_before_claim(mocker):
    """A changed provider remains reviewable instead of returning to the queue."""
    from documents.operation_journal import NeedsAttention

    provider = SimpleNamespace(identity="search:1", configuration={})
    mocker.patch(
        "documents.search.get_backend", return_value=SimpleNamespace(provider=provider)
    )
    mocker.patch("documents.search_operations.run_search.apply_async")
    execute = mocker.patch("documents.search_operations._execute")
    job = submit_search(provider, "wipe", {})
    run_search(str(job.pk))
    provider.configuration = {"changed": True}
    with pytest.raises(NeedsAttention):
        process(job.pk, "maintenance", UserFactory(is_superuser=True))
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.ATTENTION
    assert job.result["invocations"]["maintenance"].get("attempts", 0) == 0
    execute.assert_not_called()


def test_processor_skips_busy_job_and_continues(mocker):
    """Competing server workers do not prevent unrelated queued work."""
    from django.core.management import call_command
    from filelock import Timeout

    processor = UserFactory()
    for _ in range(2):
        DocumentJob.objects.create(
            kind="search",
            timeout=30,
            status=DocumentJob.Status.WAITING,
            result={
                "invocations": {
                    "maintenance": {"operation": "search.wipe", "plugin": "provider"}
                }
            },
        )
    execute = mocker.patch(
        "documents.management.commands.document_processor.process",
        side_effect=[Timeout("busy.lock"), None],
    )
    call_command(
        "document_processor",
        user=processor.username,
        operation=["search.wipe"],
        provider=["provider"],
    )
    assert execute.call_count == 2
