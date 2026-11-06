"""Event mode reuses one child without scanning during quiet periods."""

import json
from unittest.mock import Mock

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from filelock import Timeout
from redis.exceptions import ConnectionError as RedisConnectionError

from documents.models import DocumentJob
from paperless_testing.factories import UserFactory

pytestmark = pytest.mark.django_db


@pytest.fixture
def events(mocker, monkeypatch):
    """Supply a finite notification source and a controlled monotonic clock."""
    queue = mocker.patch(
        "documents.management.commands.document_processor.Queue"
    ).return_value
    clock = mocker.patch(
        "documents.management.commands.document_processor.time.monotonic",
        return_value=100,
    )
    mocker.patch("documents.management.commands.document_processor.time.sleep")
    monkeypatch.setenv("PAPERLESS_PROCESSOR_IDLE_TIMEOUT", "60")
    queue.pop.return_value = None
    return queue, clock


def pending_job():
    """Create a selectable pending search operation."""
    return DocumentJob.objects.create(
        kind="search",
        timeout=30,
        status="waiting",
        result={
            "invocations": {
                "work": {
                    "status": "pending",
                    "operation": "search.write",
                    "plugin": "search:1",
                    "version": 2,
                }
            }
        },
    )


def hint(job):
    """Identify work without treating the hint as execution authority."""
    return {"job_id": str(job.pk), "key": "work", "capability": "search"}


def consume(user, **kwargs):
    """Run the supervised mode with explicit search placement."""
    call_command(
        "document_processor",
        user=user.username,
        configured=True,
        capability=["search"],
        events=True,
        **kwargs,
    )


def test_multiple_operations_reuse_child_and_block_between_work(events, mocker):
    queue, _ = events
    first, second = pending_job(), pending_job()
    queue.pop.side_effect = [hint(second), None]
    execute = mocker.patch(
        "documents.management.commands.document_processor.process", return_value=True
    )
    consume(UserFactory(is_superuser=True), notification=json.dumps(hint(first)))
    assert [call.args[0] for call in execute.call_args_list] == [first.pk, second.pk]
    assert [call.args[0] for call in queue.pop.call_args_list] == [60, 60]
    queue.close.assert_called_once()


def test_stale_notification_does_not_extend_quiet_deadline(events, mocker):
    queue, clock = events
    job = pending_job()
    job.status = "cancelled"
    job.save()
    clock.side_effect = [100, 130, 130]
    execute = mocker.patch("documents.management.commands.document_processor.process")
    consume(UserFactory(is_superuser=True), notification=json.dumps(hint(job)))
    execute.assert_not_called()
    queue.pop.assert_called_once_with(30)


def test_zero_timeout_drains_without_blocking(events, monkeypatch, mocker):
    queue, _ = events
    monkeypatch.setenv("PAPERLESS_PROCESSOR_IDLE_TIMEOUT", "0")
    queue.pop.side_effect = [hint(pending_job()), None]
    execute = mocker.patch(
        "documents.management.commands.document_processor.process", return_value=True
    )
    consume(UserFactory(is_superuser=True))
    assert execute.call_count == 1
    assert all(call.args == (0,) for call in queue.pop.call_args_list)


def test_busy_notification_survives_zero_timeout_exit(events, monkeypatch, mocker):
    queue, _ = events
    monkeypatch.setenv("PAPERLESS_PROCESSOR_IDLE_TIMEOUT", "0")
    notification = hint(pending_job())
    mocker.patch(
        "documents.management.commands.document_processor.process",
        side_effect=Timeout("busy"),
    )
    consume(UserFactory(is_superuser=True), notification=json.dumps(notification))
    queue.push.assert_called_once_with(notification)
    queue.pop.assert_not_called()


def test_busy_work_retries_without_discarding_other_work(events, mocker):
    queue, _ = events
    first, second = pending_job(), pending_job()
    queue.pop.side_effect = [hint(second), hint(first), None]
    execute = mocker.patch(
        "documents.management.commands.document_processor.process",
        side_effect=[Timeout("busy"), True, True],
    )
    consume(UserFactory(is_superuser=True), notification=json.dumps(hint(first)))
    queue.push.assert_called_once_with(hint(first))
    assert [call.args[0] for call in execute.call_args_list] == [
        first.pk,
        second.pk,
        first.pk,
    ]


def test_reconciliation_runs_only_when_requested(events, mocker):
    queue, _ = events
    recover = mocker.patch("documents.jobs.recover_document_jobs")
    consume(UserFactory(is_superuser=True), reconcile=True)
    recover.assert_called_once()
    queue.pop.assert_called_once_with(60)


def test_redis_disconnect_requests_supervisor_reconnect(events):
    queue, _ = events
    queue.pop.side_effect = RedisConnectionError("offline")
    with pytest.raises(CommandError) as caught:
        consume(UserFactory(is_superuser=True))
    assert caught.value.returncode == 3
    queue.close.assert_called_once()


def test_missing_account_has_configuration_exit_status(events):
    with pytest.raises(CommandError) as caught:
        consume(Mock(username="missing"))
    assert caught.value.returncode == 2


def test_event_mode_queries_only_notified_job(events, mocker):
    notified = pending_job()
    pending_job()
    execute = mocker.patch(
        "documents.management.commands.document_processor.process", return_value=True
    )
    consume(UserFactory(is_superuser=True), notification=json.dumps(hint(notified)))
    execute.assert_called_once_with(notified.pk, "work", mocker.ANY)


def test_ungranted_account_never_consumes_capability_work(events):
    """A misconfigured account leaves shared notifications for eligible peers."""
    queue, _ = events
    with pytest.raises(CommandError) as caught:
        consume(UserFactory())
    assert caught.value.returncode == 2
    queue.pop.assert_not_called()


def test_revocation_returns_work_and_disables_consumer(events, mocker):
    """Revocation after a blocking read cannot steal another processor's hint."""
    from django.core.exceptions import PermissionDenied

    queue, _ = events
    notification = hint(pending_job())
    queue.pop.return_value = notification
    mocker.patch(
        "documents.management.commands.document_processor.process",
        side_effect=PermissionDenied,
    )
    with pytest.raises(CommandError) as caught:
        consume(UserFactory(is_superuser=True))
    assert caught.value.returncode == 2
    queue.push.assert_called_once_with(notification)


def test_child_bounds_socket_wait_even_when_redis_response_is_lost(events, mocker):
    """Redis's server timeout alone does not bound a broken client connection."""
    factory = mocker.patch("documents.management.commands.document_processor.Queue")
    factory.return_value.pop.return_value = None
    user = UserFactory(is_superuser=True)
    consume(user)
    factory.assert_called_once_with(["search"], user=user.username, socket_timeout=65)


def test_failed_execution_time_does_not_consume_quiet_period(events, mocker):
    """A slow failed attempt still leaves the full quiet period for safe retry."""
    queue, clock = events
    job = pending_job()

    def fail(*args):
        job.result["invocations"]["work"]["attempts"] = 1
        job.save(update_fields=["result"])
        clock.return_value = 200
        raise RuntimeError("endpoint failed after a long request")

    mocker.patch(
        "documents.management.commands.document_processor.process", side_effect=fail
    )
    consume(UserFactory(is_superuser=True), notification=json.dumps(hint(job)))
    queue.pop.assert_called_once_with(60)
