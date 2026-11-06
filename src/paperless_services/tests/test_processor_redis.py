"""Exercise blocking wake delivery against a disposable real Redis instance."""

import os
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from paperless_services.processor_queue import Queue


@pytest.fixture
def broker(monkeypatch):
    """Isolate integration keys and delete only this test's namespace."""
    url = os.environ.get("PAPERLESS_TEST_REDIS")
    if not url:
        pytest.skip("Set PAPERLESS_TEST_REDIS to a disposable Redis test server")
    monkeypatch.setenv("PAPERLESS_REDIS", url)
    monkeypatch.setenv("PAPERLESS_REDIS_PREFIX", f"test-{uuid4()}:")
    queue = Queue(["parser", "search"], user="test", socket_timeout=5)
    try:
        yield queue
    finally:
        keys = list(queue.client.scan_iter(match=queue.prefix + "*"))
        if keys:
            queue.client.delete(*keys)
        queue.close()


def test_real_blocking_wake_and_timeout(broker):
    """Work wakes a blocked connection and an empty queue actually times out."""
    notification = {"capability": "parser", "job_id": str(uuid4()), "key": "parse"}
    with ThreadPoolExecutor(max_workers=1) as pool:
        waiting = pool.submit(broker.pop, 2)
        broker.push(notification)
        assert waiting.result(timeout=4) == notification
    assert broker.pop(0.05) is None
    assert broker.pop(0) is None


def test_real_account_control_does_not_steal_capability_work(broker):
    """A disabled processor wakes only for its own account and preserves work."""
    notification = {"capability": "parser", "job_id": str(uuid4()), "key": "parse"}
    control = Queue([], user="test", socket_timeout=5)
    try:
        broker.push(notification)
        assert control.pop(0) is None
        broker.push({"user": "test", "reconcile": True})
        assert control.pop(0) == {"user": "test", "reconcile": True}
        assert broker.pop(0) == notification
    finally:
        control.close()


def test_real_provisioning_broadcast_reaches_shared_account_supervisors(broker):
    """One provisioning event reaches every sibling without stealing work."""
    controls = [Queue([], user="test", socket_timeout=5) for _ in range(2)]
    work = {"capability": "parser", "job_id": str(uuid4()), "key": "parse"}
    provisioned = {"user": "test", "reconcile": True}
    try:
        broker.push(work)
        with ThreadPoolExecutor(max_workers=2) as pool:
            waiting = [pool.submit(control.pop, 2) for control in controls]
            broker.push(provisioned)
            assert [future.result(timeout=4) for future in waiting] == [provisioned] * 2
        for control in controls:
            control.close()
            assert control.pop(0) is None
        assert broker.pop(0) == work
        broker.push(provisioned)
        broker.push(provisioned)
        assert broker.client.xlen(broker.prefix + "account:test") == 1
        assert [control.pop(0) for control in controls] == [provisioned] * 2
        assert [control.pop(0) for control in controls] == [None, None]
    finally:
        for control in controls:
            control.close()


def test_real_busy_capability_does_not_starve_other_work(broker):
    """Requeued parser work yields to search while preserving each list's FIFO."""
    parser = {"capability": "parser", "job_id": str(uuid4()), "key": "parse"}
    search = {"capability": "search", "job_id": str(uuid4()), "key": "search"}
    broker.push(parser)
    broker.push(search)
    assert broker.pop(0) == parser
    broker.push(parser)
    assert broker.pop(0) == search
    assert broker.pop(0) == parser
