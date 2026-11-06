"""Redis wake hints support blocking, namespaces and lightweight imports."""

import json
import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest

from paperless_services.processor_queue import Queue
from paperless_services.processor_queue import capability_for
from paperless_services.processor_queue import idle_timeout


@pytest.fixture
def connection(monkeypatch):
    """Capture Redis operations without opening a socket."""
    client = Mock()
    factory = Mock(return_value=client)
    monkeypatch.setattr("paperless_services.processor_queue.Redis.from_url", factory)
    monkeypatch.delenv("PAPERLESS_REDIS", raising=False)
    monkeypatch.delenv("PAPERLESS_REDIS_PREFIX", raising=False)
    return client, factory


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("redis://broker:6379/2", "redis://broker:6379/2"),
        ("rediss://broker:6379/2", "rediss://broker:6379/2"),
        ("unix:///run/redis.sock?db=2", "unix:///run/redis.sock?db=2"),
        ("redis+socket:///run/redis.sock?virtual_host=2", "unix:/run/redis.sock?db=2"),
    ],
)
def test_connection_formats(connection, monkeypatch, url, expected):
    """Use the same TCP, TLS and socket settings as other Paperless services."""
    _client, factory = connection
    monkeypatch.setenv("PAPERLESS_REDIS", url)
    Queue(["parser"])
    factory.assert_called_once_with(
        expected, decode_responses=True, socket_connect_timeout=5, socket_timeout=None
    )


def test_blocking_pop_and_push_keep_capability_and_namespace(connection, monkeypatch):
    """Only a selected capability receives a retained notification."""
    client, _factory = connection
    monkeypatch.setenv("PAPERLESS_REDIS_PREFIX", "instance:")
    queue = Queue(["parser", "search"])
    notification = {
        "job_id": "e38a1d3a-aa42-46dd-b9ed-a898b05fbc55",
        "key": "parse",
        "capability": "parser",
    }
    queue.push(notification)
    client.rpush.assert_called_once_with(
        "instance:processor:wake:parser", json.dumps(notification)
    )
    client.blpop.return_value = (
        "instance:processor:wake:parser",
        json.dumps(notification),
    )
    assert queue.pop() == notification
    client.blpop.assert_called_once_with(
        ["instance:processor:wake:parser", "instance:processor:wake:search"], timeout=0
    )


def test_zero_timeout_does_not_block(connection):
    """Zero drains existing notifications without Redis's infinite timeout."""
    client, _factory = connection
    queue = Queue(["parser", "search"])
    client.lpop.side_effect = [None, None]
    assert queue.pop(0) is None
    assert client.lpop.call_count == 2
    client.blpop.assert_not_called()


def test_malformed_notification_does_not_stop_queue(connection):
    """Bad hints cannot prevent a valid later wake from reaching the child."""
    client, _factory = connection
    queue = Queue(["parser"])
    valid = {"capability": "parser", "reconcile": True}
    client.blpop.side_effect = [
        ("processor:wake:parser", "not json"),
        (
            "processor:wake:parser",
            json.dumps({"capability": "search", "reconcile": True}),
        ),
        (
            "processor:wake:parser",
            json.dumps({"capability": "parser", "job_id": "not-uuid", "key": "parse"}),
        ),
        ("processor:wake:parser", json.dumps(valid)),
    ]
    assert queue.pop() == valid
    assert client.blpop.call_count == 4


def test_publishing_connection_can_use_bounded_socket_timeout(connection):
    """An unavailable broker cannot indefinitely hold a publication callback."""
    _client, factory = connection
    Queue(["parser"], socket_timeout=5)
    assert factory.call_args.kwargs["socket_timeout"] == 5


def test_reconnect_probe_uses_bounded_connection(connection):
    """The connection probe times out even though the idle consumer blocks."""
    client, factory = connection
    queue = Queue(["parser"])
    queue.connect()
    assert factory.call_args.kwargs["socket_timeout"] == 5
    client.ping.assert_called_once()
    client.close.assert_called_once()


@pytest.mark.parametrize("value, expected", [("0", 0), ("60", 60), ("120", 120)])
def test_idle_timeout(monkeypatch, value, expected):
    """Accept an explicit nonnegative quiet period, including immediate exit."""
    monkeypatch.setenv("PAPERLESS_PROCESSOR_IDLE_TIMEOUT", value)
    assert idle_timeout() == expected


@pytest.mark.parametrize("value", ["-1", "1.5", "", "invalid"])
def test_invalid_idle_timeout(monkeypatch, value):
    """Reject invalid lifecycle settings before any child starts."""
    monkeypatch.setenv("PAPERLESS_PROCESSOR_IDLE_TIMEOUT", value)
    with pytest.raises(ValueError, match="nonnegative integer"):
        idle_timeout()


def test_default_idle_timeout(monkeypatch):
    """The default retains processing imports for one quiet minute."""
    monkeypatch.delenv("PAPERLESS_PROCESSOR_IDLE_TIMEOUT", raising=False)
    assert idle_timeout() == 60
    assert capability_for("prepare:barcode") == "ingestion"
    assert capability_for("parser.process") == "parser"


def test_queue_and_supervisor_import_no_django():
    """Waiting for work must not retain the framework or provider runtime."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from paperless_services import processor_queue, processor; assert not any(name == 'django' or name.startswith('django.') for name in sys.modules)",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_account_control_queue_does_not_consume_capability_work(connection):
    """Each denied supervisor retains its own provisioning stream cursor."""
    client, _factory = connection
    queue = Queue([], user="processor")
    notification = {"user": "processor", "reconcile": True}
    queue.push(notification)
    client.xadd.assert_called_once_with(
        "processor:wake:account:processor",
        {"notification": json.dumps(notification)},
        maxlen=1,
        approximate=False,
    )
    client.xread.return_value = [
        (
            "processor:wake:account:processor",
            [("100-0", {"notification": json.dumps(notification)})],
        )
    ]
    assert queue.pop() == notification
    client.xread.assert_called_once_with(
        {"processor:wake:account:processor": "0-0"}, count=1, block=0
    )
    queue.close()
    client.xread.return_value = []
    assert queue.pop(0) is None
    client.xread.assert_called_with(
        {"processor:wake:account:processor": "100-0"}, count=1, block=None
    )
    client.blpop.assert_not_called()
    client.lpop.assert_not_called()


def test_account_control_requires_matching_identity(connection):
    """An account control hint cannot wake a different processor identity."""
    client, _factory = connection
    queue = Queue([], user="processor")
    notification = {"user": "processor", "reconcile": True}
    client.xread.side_effect = [
        [
            (
                "processor:wake:account:processor",
                [
                    (
                        "100-0",
                        {
                            "notification": json.dumps(
                                {"user": "other", "reconcile": True}
                            )
                        },
                    )
                ],
            )
        ],
        [
            (
                "processor:wake:account:processor",
                [("101-0", {"notification": json.dumps(notification)})],
            )
        ],
    ]
    assert queue.pop() == notification
    assert client.xread.call_count == 2


def test_enabled_account_does_not_consume_control_stream(connection):
    """Enabled processors receive only work; provisioning remains broadcast."""
    client, _factory = connection
    queue = Queue(["parser"], user="processor")
    client.lpop.return_value = None
    assert queue.pop(0) is None
    client.lpop.assert_called_once_with("processor:wake:parser")
    client.xread.assert_not_called()


def test_configuration_file_does_not_require_django(tmp_path):
    """The parent sees the same broker configuration without loading Django."""
    path = tmp_path / "paperless.conf"
    path.write_text(
        "PAPERLESS_REDIS=redis://configured:6379/4\n"
        "PAPERLESS_REDIS_PREFIX=file-prefix:\n"
        "PAPERLESS_PROCESSOR_IDLE_TIMEOUT=90\n"
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith("PAPERLESS_")
    }
    env["PAPERLESS_CONFIGURATION_PATH"] = str(path)
    env["PAPERLESS_REDIS_PREFIX"] = "environment-prefix:"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; from paperless.environment import load_configuration; from paperless_services.processor_queue import Queue, idle_timeout; load_configuration(); q = Queue(['parser']); assert q.url == 'redis://configured:6379/4'; assert q.prefix == 'environment-prefix:processor:wake:'; assert idle_timeout() == 90; q.close(); assert not any(name == 'django' or name.startswith('django.') for name in sys.modules)",
        ],
        cwd=Path(__file__).resolve().parents[2],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
