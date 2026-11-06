"""Celery runners execute placed operations and retain offline workstation work."""

from datetime import timedelta

import pytest
from django.db import connection
from django.utils import timezone
from filelock import FileLock

from documents.deferred_operations import recover_waiting
from documents.job_state import PendingOperation
from documents.job_storage import job_directory
from documents.models import DocumentJob
from documents.operation_journal import invoke
from documents.processor_notifications import defer_notifications
from documents.processor_notifications import dispatch_pending
from paperless_testing.factories import UserFactory

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("paperless_dirs"),
]


@pytest.fixture
def server(settings):
    """Place parser work on a currently authorized server account."""
    user = UserFactory(is_superuser=True)
    settings.PROCESSOR_USER = user.username
    settings.PROCESSOR_CAPABILITIES = ["parser"]
    return user


def operation(job, key, execute, validate=None, *, replay_safe=True):
    """Call the finite parser contract under the same lock as document runners."""
    with (
        defer_notifications(),
        FileLock(job_directory(job.pk).with_suffix(".lock"), timeout=0),
    ):
        return invoke(
            job,
            key,
            "parser:1",
            {},
            execute,
            operation="parser.process",
            validate=validate,
            replay_safe=replay_safe,
        )


def test_server_advances_and_validates_without_handoffs(
    server, processor_notifications
):
    """Several operations reuse one task without a transaction during execution."""
    job = DocumentJob.objects.create(kind="reprocess", timeout=30)
    executed, validated = [], []

    def execute(identifier):
        assert not connection.in_atomic_block
        executed.append(identifier)
        return {"text": "accepted"}

    for key in ["first", "second", "second"]:
        assert operation(job, key, execute, validated.append) == {"text": "accepted"}
    assert len(executed) == len(validated) == 2
    job.refresh_from_db()
    for entry in job.result["invocations"].values():
        assert entry["status"] == "succeeded"
        assert entry["attempts"] == 1
        assert entry["processor_location"] == "server"
    processor_notifications.assert_not_called()


def test_inline_result_is_rejected_if_processor_grant_revoked(server):
    """Inline acceptance rechecks authorization after external computation."""
    from django.core.exceptions import PermissionDenied

    job = DocumentJob.objects.create(kind="reprocess", timeout=30)

    def execute(identifier):
        server.is_active = False
        server.save(update_fields=["is_active"])
        return "output"

    with pytest.raises(PermissionDenied):
        operation(job, "parse", execute)
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.REJECTED
    assert job.result["invocations"]["parse"]["status"] == "conflicted"


def test_offline_week_does_not_claim_or_dispatch(
    settings, processor_notifications, mocker
):
    """Recovery distinguishes server readiness from unclaimed workstation work."""
    processor_notifications.side_effect = dispatch_pending
    dispatch = mocker.patch("documents.jobs._dispatch")
    settings.PROCESSOR_USER = ""
    settings.PROCESSOR_CAPABILITIES = []
    job = DocumentJob.objects.create(kind="reprocess", timeout=30)
    executed = []
    with pytest.raises(PendingOperation):
        operation(job, "parse", executed.append)
    job.updated = timezone.now() - timedelta(days=8)
    job.save(update_fields=["updated"])
    for _ in range(3):
        recover_waiting()
    job.refresh_from_db()
    entry = job.result["invocations"]["parse"]
    assert job.status == DocumentJob.Status.WAITING
    assert entry["status"] == "pending"
    assert entry.get("attempts", 0) == 0
    assert "lease_until" not in entry
    assert executed == []
    dispatch.assert_not_called()


@pytest.mark.parametrize("safe,expected", [(True, "pending"), (False, "unknown")])
def test_worker_crash_keeps_claim_replay_boundary(server, safe, expected):
    """Only expired safe execution can return to the pending journal."""
    job = DocumentJob.objects.create(kind="reprocess", timeout=30)

    def crash(identifier):
        raise SystemExit("worker died")

    with pytest.raises(SystemExit):
        operation(job, "parse", crash, replay_safe=safe)
    job.refresh_from_db()
    entry = job.result["invocations"]["parse"]
    assert entry["status"] == "leased"
    entry["lease_until"] = (timezone.now() - timedelta(seconds=1)).isoformat()
    job.save(update_fields=["result"])
    recover_waiting()
    job.refresh_from_db()
    assert job.result["invocations"]["parse"]["status"] == expected
    assert job.result["invocations"]["parse"]["attempts"] == 1


def test_cancellation_between_operations_stops_traversal(server):
    """A cancelled journal never starts the next finite operation."""
    job = DocumentJob.objects.create(kind="reprocess", timeout=30)
    executed = []
    operation(job, "first", executed.append)
    DocumentJob.objects.filter(pk=job.pk).update(status=DocumentJob.Status.CANCELLED)
    with pytest.raises(PendingOperation):
        operation(job, "second", executed.append)
    job.refresh_from_db()
    assert len(executed) == 1
    assert "second" not in job.result["invocations"]
    assert job.status == DocumentJob.Status.CANCELLED


def test_waiting_search_delivery_does_not_initialize_provider(mocker):
    """A duplicate delivery while offline exits before loading the provider."""
    from documents.search_operations import run_search

    job = DocumentJob.objects.create(
        kind="search", timeout=30, status=DocumentJob.Status.WAITING
    )
    backend = mocker.patch("documents.search.get_backend")
    run_search(str(job.pk))
    backend.assert_not_called()
