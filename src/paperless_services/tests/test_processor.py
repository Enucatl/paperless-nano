"""Idle supervisors wait for notifications and forward active-child shutdown."""

import json
import signal
from unittest.mock import Mock

import pytest
from redis.exceptions import ConnectionError

from paperless_services.processor import Processor


@pytest.fixture
def supervisor(monkeypatch):
    """Expose lifecycle events without running processes or contacting Redis."""
    monkeypatch.setenv("PAPERLESS_PROCESSOR_USER", "processor")
    monkeypatch.setenv("PAPERLESS_PROCESSOR_CAPABILITIES", "parser search")
    handlers = {}

    def register(number, handler):
        previous = handlers.get(number)
        handlers[number] = handler
        return previous

    monkeypatch.setattr("signal.signal", register)
    queue = Mock()
    monkeypatch.setattr(
        "paperless_services.processor.Queue", lambda *args, **kwargs: queue
    )
    start = Mock()
    monkeypatch.setattr("subprocess.Popen", start)
    kill = Mock()
    monkeypatch.setattr("os.killpg", kill)
    wait = Mock()
    monkeypatch.setattr("threading.Event.wait", wait)
    return queue, start, kill, wait, handlers


def test_idle_wait_launches_no_extra_child(supervisor):
    """Only startup reconciliation runs before an indefinite Redis wait."""
    queue, start, kill, wait, handlers = supervisor
    start.return_value.wait.return_value = 0
    queue.pop.side_effect = lambda: handlers[signal.SIGTERM](signal.SIGTERM, None)
    Processor().run()
    start.assert_called_once()
    assert "--reconcile" in start.call_args.args[0]
    assert "--events" in start.call_args.args[0]
    queue.pop.assert_called_once_with()
    wait.assert_not_called()
    kill.assert_not_called()
    assert queue.close.call_count == 2


def test_notification_starts_child_and_forwards_shutdown(supervisor):
    """Work after idle reaches the child; SIGTERM reaches its entire group."""
    queue, start, kill, wait, handlers = supervisor
    notification = {"job_id": "job", "key": "parse", "capability": "parser"}
    queue.pop.return_value = notification
    children = [Mock(pid=123), Mock(pid=456)]
    for child in children:
        child.poll.return_value = None
    children[0].wait.return_value = 0
    children[1].wait.side_effect = lambda: (
        handlers[signal.SIGTERM](signal.SIGTERM, None) or 0
    )
    start.side_effect = children
    Processor().run()
    assert start.call_count == 2
    arguments = start.call_args.args[0]
    assert "--reconcile" not in arguments
    assert json.loads(arguments[arguments.index("--notification") + 1]) == notification
    assert start.call_args.kwargs == {"start_new_session": True}
    kill.assert_called_once_with(456, signal.SIGTERM)
    queue.pop.assert_called_once()
    wait.assert_not_called()


@pytest.mark.parametrize("returncode, launches", [(2, 1), (1, 2)])
def test_persistent_failure_does_not_restart_forever(supervisor, returncode, launches):
    """Configuration errors wait; a crash receives at most one recovery child."""
    queue, start, _kill, wait, handlers = supervisor
    start.return_value.wait.return_value = returncode
    queue.pop.side_effect = lambda: handlers[signal.SIGTERM](signal.SIGTERM, None)
    Processor().run()
    assert start.call_count == launches
    queue.pop.assert_called_once_with()
    wait.assert_not_called()


def test_redis_outage_does_not_start_django(supervisor):
    """Connection backoff happens in the lightweight parent before recovery."""
    queue, start, _kill, wait, handlers = supervisor
    queue.connect.side_effect = [ConnectionError("offline"), None]
    start.return_value.wait.return_value = 0
    queue.pop.side_effect = lambda: handlers[signal.SIGTERM](signal.SIGTERM, None)
    Processor().run()
    assert queue.connect.call_count == 2
    wait.assert_called_once_with(1)
    start.assert_called_once()
    assert "--reconcile" in start.call_args.args[0]


def test_reconnect_reconciles_pending_work(supervisor):
    """A failed idle read triggers one reconciliation after Redis returns."""
    queue, start, _kill, _wait, handlers = supervisor
    start.return_value.wait.return_value = 0
    calls = 0

    def receive():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ConnectionError("offline")
        handlers[signal.SIGTERM](signal.SIGTERM, None)

    queue.pop.side_effect = receive
    Processor().run()
    assert start.call_count == 2
    assert all("--reconcile" in call.args[0] for call in start.call_args_list)
    assert queue.connect.call_count == 2


@pytest.mark.parametrize("capabilities", ["", "parser unknown"])
def test_invalid_configuration_starts_no_child(monkeypatch, capabilities):
    """A host cannot silently select unrequested processing capabilities."""
    monkeypatch.setenv("PAPERLESS_PROCESSOR_USER", "processor")
    monkeypatch.setenv("PAPERLESS_PROCESSOR_CAPABILITIES", capabilities)
    start = Mock()
    monkeypatch.setattr("subprocess.Popen", start)
    with pytest.raises(ValueError):
        Processor().run()
    start.assert_not_called()


def test_shutdown_during_spawn_does_not_orphan_child(supervisor):
    """A signal between process creation and assignment is forwarded afterward."""
    queue, start, kill, _wait, handlers = supervisor
    child = Mock(pid=789)
    child.poll.return_value = None
    child.wait.return_value = 0

    def spawn(*args, **kwargs):
        handlers[signal.SIGTERM](signal.SIGTERM, None)
        return child

    start.side_effect = spawn
    Processor().run()
    kill.assert_called_once_with(789, signal.SIGTERM)
    child.wait.assert_called_once()
    queue.pop.assert_not_called()


def test_child_redis_failure_waits_for_reconnection(supervisor):
    """A disconnected child cannot trigger more Django starts during an outage."""
    queue, start, _kill, wait, handlers = supervisor
    child = Mock()
    child.wait.side_effect = [3, 0]
    start.return_value = child
    queue.connect.side_effect = [None, ConnectionError("offline"), None]
    queue.pop.side_effect = lambda: handlers[signal.SIGTERM](signal.SIGTERM, None)
    Processor().run()
    assert start.call_count == 2
    assert queue.connect.call_count == 3
    assert [call.args[0] for call in wait.call_args_list] == [1, 2]
    assert all("--reconcile" in call.args[0] for call in start.call_args_list)


def test_failed_spawn_retains_received_notification(supervisor):
    """A wake consumed by the parent survives failure to start its child."""
    queue, start, _kill, _wait, _handlers = supervisor
    notification = {"job_id": "job", "key": "parse", "capability": "parser"}
    queue.pop.return_value = notification
    child = Mock()
    child.wait.return_value = 0
    start.side_effect = [child, OSError("cannot spawn")]
    with pytest.raises(OSError, match="cannot spawn"):
        Processor().run()
    queue.push.assert_called_once_with(notification)


def test_denied_account_waits_for_provisioning_without_stealing_work(
    supervisor, monkeypatch
):
    """Denied processors leave capability wakes for eligible competitors."""
    queue, start, _kill, _wait, handlers = supervisor
    controls = Mock()
    publisher = Mock()
    notification = {"job_id": "job", "key": "parse", "capability": "parser"}
    received = 0

    def receive_work():
        nonlocal received
        received += 1
        if received == 1:
            return notification
        handlers[signal.SIGTERM](signal.SIGTERM, None)

    queue.pop.side_effect = receive_work
    controls.pop.return_value = {"user": "processor", "reconcile": True}
    factory = Mock(
        side_effect=lambda caps, **kw: (
            publisher if "socket_timeout" in kw else queue if caps else controls
        )
    )
    monkeypatch.setattr("paperless_services.processor.Queue", factory)
    start.return_value.wait.side_effect = [0, 2, 0]
    Processor().run()
    publisher.push.assert_called_once_with(notification)
    controls.pop.assert_called_once_with()
    assert queue.pop.call_count == 2
    assert start.call_count == 3
    assert "--reconcile" in start.call_args.args[0]
    assert any(
        call.args == ([],) and call.kwargs == {"user": "processor"}
        for call in factory.call_args_list
    )


def test_disabled_account_reconnect_does_not_start_django(supervisor, monkeypatch):
    """Redis recovery does not make a denied account consume capability work."""
    queue, start, _kill, _wait, handlers = supervisor
    controls = Mock()
    received = 0

    def receive_control():
        nonlocal received
        received += 1
        if received == 1:
            raise ConnectionError("offline")
        handlers[signal.SIGTERM](signal.SIGTERM, None)

    controls.pop.side_effect = receive_control
    monkeypatch.setattr(
        "paperless_services.processor.Queue",
        lambda caps, **kwargs: queue if caps else controls,
    )
    start.return_value.wait.return_value = 2
    Processor().run()
    start.assert_called_once()
    queue.pop.assert_not_called()
    controls.connect.assert_called_once()


def test_repeated_configuration_failure_reuses_control_cursor(supervisor, monkeypatch):
    """An old provisioning event cannot restart a persistently denied account."""
    queue, start, _kill, _wait, handlers = supervisor
    controls = Mock()
    received = 0

    def receive_control():
        nonlocal received
        received += 1
        if received == 1:
            return {"user": "processor", "reconcile": True}
        handlers[signal.SIGTERM](signal.SIGTERM, None)

    controls.pop.side_effect = receive_control
    factory = Mock(side_effect=lambda caps, **kwargs: queue if caps else controls)
    monkeypatch.setattr("paperless_services.processor.Queue", factory)
    start.return_value.wait.return_value = 2
    Processor().run()
    assert start.call_count == 2
    assert controls.pop.call_count == 2
    assert sum(call.args == ([],) for call in factory.call_args_list) == 1
    queue.pop.assert_not_called()
