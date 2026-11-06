"""Job handoffs observe committed state after the outer execution lock exits."""

from datetime import timedelta

import pytest
from django.db import transaction
from django.utils import timezone
from filelock import FileLock

from documents.deferred_operations import recover_waiting
from documents.job_storage import job_directory
from documents.models import DocumentJob
from documents.processor_notifications import after_unlock
from documents.processor_notifications import defer_notifications
from documents.processor_notifications import notify_pending
from documents.search_operations import run_search
from documents.search_operations import submit_search
from documents.server_processor import process
from paperless_testing.factories import UserFactory

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("paperless_dirs"),
]


def test_handoff_waits_for_outer_lock_and_commit():
    """Nested runners cannot wake the next owner before releasing their lock."""
    called = []
    with transaction.atomic():
        with defer_notifications():
            with defer_notifications():
                after_unlock(lambda: called.append("ready"))
            assert called == []
        assert called == []
    assert called == ["ready"]


def test_rollback_discards_handoff():
    """A rolled-back operation must never publish readiness."""
    called = []
    with defer_notifications():
        with pytest.raises(ValueError), transaction.atomic():
            after_unlock(lambda: called.append("ready"))
            raise ValueError("rollback")
    assert called == []


def test_exception_keeps_committed_handoff():
    """Control-flow exceptions still publish committed waiting work."""
    called = []
    with pytest.raises(ValueError), defer_notifications():
        after_unlock(lambda: called.append("ready"))
        assert called == []
        raise ValueError("waiting")
    assert called == ["ready"]


def test_search_handoffs_release_both_coordinator_and_processor_locks(
    mocker, processor_notifications
):
    """Immediate delivery can acquire the job lock in both directions."""
    from types import SimpleNamespace

    provider = SimpleNamespace(identity="search:1", configuration={}, replay_safe=True)
    mocker.patch(
        "documents.search.get_backend", return_value=SimpleNamespace(provider=provider)
    )
    mocker.patch("documents.search_operations.run_search.apply_async")
    dispatch = mocker.patch("documents.jobs._dispatch")
    mocker.patch("documents.search_operations._execute", return_value=None)
    job = submit_search(provider, "optimize", {})
    lock = FileLock(job_directory(job.pk).with_suffix(".lock"), timeout=0)

    def assert_unlocked(*args):
        with lock:
            pass

    processor_notifications.push.side_effect = assert_unlocked
    run_search(str(job.pk))
    assert processor_notifications.push.call_args.args[0]["job_id"] == str(job.pk)
    dispatch.reset_mock()
    dispatch.side_effect = assert_unlocked
    process(job.pk, "maintenance", UserFactory(is_superuser=True))
    dispatch.assert_called_once()


@pytest.mark.parametrize(
    "state,safe,expected",
    [
        ("pending", True, "pending"),
        ("active", True, "leased"),
        ("expired", True, "pending"),
        ("expired", False, "unknown"),
    ],
)
def test_recovery_only_notifies_unclaimed_safe_work(
    state, safe, expected, processor_notifications
):
    """Waiting is not failure; active leases and uncertain effects are preserved."""
    entry = {
        "status": "pending" if state == "pending" else "leased",
        "version": 2,
        "operation": "parser.process",
        "policy": {"permission": "documents.process_parser"},
        "replay_safe": safe,
        "attempts": 1,
        "lease_until": (
            timezone.now() + timedelta(seconds=60 if state == "active" else -60)
        ).isoformat(),
    }
    job = DocumentJob.objects.create(
        kind="search",
        timeout=30,
        status="waiting",
        result={"invocations": {"work": entry}},
    )
    recover_waiting()
    job.refresh_from_db()
    assert job.result["invocations"]["work"]["status"] == expected
    assert job.result["invocations"]["work"]["attempts"] == 1
    assert processor_notifications.push.call_count == int(expected == "pending")


def test_notification_failure_preserves_pending_job(processor_notifications):
    """A failed Redis send leaves authoritative work available for reconciliation."""
    job = DocumentJob.objects.create(
        kind="search",
        timeout=30,
        status="waiting",
        result={
            "invocations": {
                "work": {
                    "status": "pending",
                    "version": 2,
                    "operation": "search.write",
                    "policy": {},
                }
            }
        },
    )
    processor_notifications.push.side_effect = ConnectionError("offline")
    notify_pending(job.pk)
    job.refresh_from_db()
    assert job.result["invocations"]["work"]["status"] == "pending"


def test_startup_repairs_recent_dispatch_without_interrupting_admission(mocker):
    """Restart need not wait a day to redispatch a freshly staged result."""
    from documents.jobs import recover_document_jobs

    staged = DocumentJob.objects.create(
        kind="search",
        timeout=30,
        status="queued",
        result={"invocations": {"work": {"status": "staged"}}},
    )
    admitting = DocumentJob.objects.create(kind="consume", timeout=30, status="staging")
    running = DocumentJob.objects.create(kind="search", timeout=30, status="running")
    dispatch = mocker.patch("documents.jobs._dispatch")
    recover_document_jobs(include_recent=True)
    assert [call.args[0].pk for call in dispatch.call_args_list] == [staged.pk]
    admitting.refresh_from_db()
    running.refresh_from_db()
    assert admitting.status == "staging"
    assert running.status == "running"


def test_recovery_releases_cursor_before_coordinator_publication(mocker):
    """Startup recovery can dispatch multiple jobs when Celery closes SQL."""
    from django.db import connection

    from documents.jobs import recover_document_jobs

    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL server-side cursor regression")
    jobs = [
        DocumentJob.objects.create(kind="search", timeout=30, status="queued")
        for _ in range(2)
    ]
    dispatch = mocker.patch(
        "documents.jobs._dispatch", side_effect=lambda job: connection.close()
    )
    recover_document_jobs(include_recent=True)
    assert {call.args[0].pk for call in dispatch.call_args_list} == {
        job.pk for job in jobs
    }
