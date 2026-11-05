"""Finite sweeps release imports and receive supervisor termination."""

import signal
from unittest.mock import Mock

import pytest

from paperless_services.processor import Processor


def test_processor_runs_another_sweep_and_forwards_shutdown(monkeypatch):
    """New work is picked up after idle, and SIGTERM reaches active children."""
    monkeypatch.setenv("PAPERLESS_PROCESSOR_USER", "processor")
    monkeypatch.setenv("PAPERLESS_PROCESSOR_CAPABILITIES", "parser search")
    handlers = {}
    monkeypatch.setattr(
        "signal.signal", lambda number, handler: handlers.setdefault(number, handler)
    )
    kill = Mock()
    monkeypatch.setattr("os.killpg", kill)
    children = [Mock(pid=123), Mock(pid=456)]
    for child in children:
        child.poll.return_value = None
    children[0].wait.return_value = 0
    children[1].wait.side_effect = lambda: (
        handlers[signal.SIGTERM](signal.SIGTERM, None) or 0
    )
    start = Mock(side_effect=children)
    monkeypatch.setattr("subprocess.Popen", start)
    monkeypatch.setattr("threading.Event.wait", lambda self, timeout: self.is_set())
    Processor().run()
    assert start.call_count == 2
    assert start.call_args.args[0][-4:] == [
        "--capability",
        "parser",
        "--capability",
        "search",
    ]
    assert start.call_args.kwargs == {"start_new_session": True}
    kill.assert_called_once_with(456, signal.SIGTERM)


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
