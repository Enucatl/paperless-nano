"""Carry processor wake hints without importing Django or provider libraries."""

import json
import logging
import math
import os
import time
from urllib.parse import parse_qsl
from urllib.parse import urlencode
from urllib.parse import urlsplit
from urllib.parse import urlunsplit
from uuid import UUID

from redis import Redis

CAPABILITIES = ("parser", "metadata", "search", "ingestion")
logger = logging.getLogger("paperless.processor")


def capability_for(operation: str) -> str:
    """Map preparation and provider operations to their execution capability."""
    return (
        "ingestion" if operation.startswith("prepare:") else operation.split(".", 1)[0]
    )


def idle_timeout() -> int:
    """Read the nonnegative child quiet period without loading settings."""
    try:
        value = int(os.environ.get("PAPERLESS_PROCESSOR_IDLE_TIMEOUT", "60"))
        if value >= 0:
            return value
    except ValueError:
        pass
    raise ValueError("PAPERLESS_PROCESSOR_IDLE_TIMEOUT must be a nonnegative integer")


class Queue:
    """Block on capability-specific hints; the database remains authoritative."""

    def __init__(
        self,
        capabilities: list[str] | tuple[str, ...],
        *,
        user: str | None = None,
        socket_timeout: float | None = None,
    ) -> None:
        """Use the deployment's Redis URL and namespace without Django settings."""
        if (not capabilities and not user) or set(capabilities) - set(CAPABILITIES):
            raise ValueError("Select known processor capabilities")
        self.capabilities = tuple(dict.fromkeys(capabilities))
        self.user = user
        self.control_cursor = "0-0"
        self.prefix = os.environ.get("PAPERLESS_REDIS_PREFIX", "") + "processor:wake:"
        url = os.environ.get("PAPERLESS_REDIS", "redis://localhost:6379")
        if url.startswith("redis+socket:"):
            parts = urlsplit(url)
            query = dict(parse_qsl(parts.query))
            if "virtual_host" in query:
                query["db"] = query.pop("virtual_host")
            url = urlunsplit(("unix", parts.netloc, parts.path, urlencode(query), ""))
        self.url = url
        self.client = Redis.from_url(
            url,
            decode_responses=True,
            socket_connect_timeout=5,
            socket_timeout=socket_timeout,
        )

    def connect(self) -> None:
        """Verify the broker before launching a recovery child."""
        client = Redis.from_url(
            self.url,
            socket_connect_timeout=5,
            socket_timeout=5,
        )
        try:
            client.ping()
        finally:
            client.close()

    def push(self, notification: dict) -> None:
        """Retain a wake until a processor for its capability receives it."""
        capability = notification.get("capability")
        user = notification.get("user")
        if isinstance(user, str) and user and notification.get("reconcile") is True:
            key = self.prefix + "account:" + user
            self.client.xadd(
                key,
                {"notification": json.dumps(notification)},
                maxlen=1,
                approximate=False,
            )
            return
        elif capability in CAPABILITIES:
            key = self.prefix + capability
        else:
            raise ValueError("Unknown processor notification capability")
        self.client.rpush(key, json.dumps(notification))

    def pop(self, timeout: float | None = None) -> dict | None:
        """Wait for a valid hint; zero drains available hints without blocking."""
        keys = [self.prefix + capability for capability in self.capabilities]
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            remaining = 0 if deadline is None else deadline - time.monotonic()
            if timeout != 0 and deadline is not None and remaining <= 0:
                return None
            if not self.capabilities:
                key = self.prefix + "account:" + self.user
                messages = self.client.xread(
                    {key: self.control_cursor},
                    count=1,
                    block=None
                    if timeout == 0
                    else 0
                    if deadline is None
                    else max(1, math.ceil(remaining * 1000)),
                )
                if not messages:
                    return None
                self.control_cursor, fields = messages[0][1][0]
                result = (key, fields.get("notification", ""))
            elif timeout == 0:
                result = None
                for key in keys:
                    value = self.client.lpop(key)
                    if value is not None:
                        result = (key, value)
                        break
            else:
                result = self.client.blpop(keys, timeout=remaining)
            if result is None:
                return None
            key, value = result
            try:
                notification = json.loads(value)
                if not isinstance(notification, dict):
                    raise ValueError("Expected an object")
                if self.user and key == self.prefix + "account:" + self.user:
                    if (
                        notification.get("user") != self.user
                        or notification.get("reconcile") is not True
                    ):
                        raise ValueError("Invalid account wake")
                    return notification
                if notification.get("capability") != key[len(self.prefix) :]:
                    raise ValueError("Capability does not match its queue")
                if notification.get("reconcile") is not True and not (
                    isinstance(notification.get("job_id"), str)
                    and isinstance(notification.get("key"), str)
                ):
                    raise ValueError("Missing job and operation identifiers")
                if notification.get("reconcile") is not True:
                    UUID(notification["job_id"])
            except (ValueError, TypeError):
                logger.warning("Discarding malformed processor notification")
                continue
            capability = notification["capability"]
            self.capabilities = (
                *(selected for selected in self.capabilities if selected != capability),
                capability,
            )
            return notification

    def close(self) -> None:
        """Release all connections, including interrupted blocking reads."""
        self.client.close()
        self.client.connection_pool.disconnect()
