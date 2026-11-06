"""Wake disposable processing children without retaining Django or model state."""

import json
import logging
import os
import signal
import subprocess
import sys
import threading

from redis.exceptions import RedisError

from paperless.environment import load_configuration
from paperless_services.processor_queue import CAPABILITIES
from paperless_services.processor_queue import Queue
from paperless_services.processor_queue import idle_timeout

logger = logging.getLogger("paperless.processor")


class _Stopped(BaseException):
    """Interrupt an idle blocking read without retrying its system call."""


class Processor:
    """Run explicitly enabled capabilities in disposable foreground children."""

    def run(self) -> None:
        """Sleep on Redis, reusing Django only while a child has active work."""
        load_configuration()
        user = os.environ.get("PAPERLESS_PROCESSOR_USER", "")
        capabilities = os.environ.get("PAPERLESS_PROCESSOR_CAPABILITIES", "").split()
        if not user or not capabilities:
            raise ValueError(
                "Set PAPERLESS_PROCESSOR_USER and PAPERLESS_PROCESSOR_CAPABILITIES to enable server processing"
            )
        if set(capabilities) - set(CAPABILITIES):
            raise ValueError("Unknown PAPERLESS_PROCESSOR_CAPABILITIES value")
        idle_timeout()
        command = [
            sys.executable,
            "manage.py",
            "document_processor",
            "--skip-checks",
            "--user",
            user,
            "--configured",
            "--events",
        ]
        for capability in capabilities:
            command.extend(["--capability", capability])
        stopped = threading.Event()
        child = None
        spawning = False
        normal_queue = Queue(capabilities, user=user)
        control_queue = Queue([], user=user)
        queue = normal_queue
        notification = None
        blocked = False

        def stop(signum: int, frame) -> None:
            """Interrupt idle waits or signal the active child process group."""
            stopped.set()
            if child is None:
                if spawning:
                    return
                raise _Stopped
            if child.poll() is None:
                try:
                    os.killpg(child.pid, signum)
                except ProcessLookupError:
                    pass

        previous = {
            number: signal.signal(number, stop)
            for number in (signal.SIGTERM, signal.SIGINT)
        }
        reconnect = True
        reconcile = True
        crash_recovery = False
        backoff = 1
        try:
            while not stopped.is_set():
                try:
                    if reconnect:
                        queue.connect()
                        reconnect = False
                        reconcile = not blocked
                        backoff = 1
                    if not reconcile:
                        notification = queue.pop()
                        if notification is None:
                            continue
                        if blocked:
                            queue.close()
                            queue = normal_queue
                            blocked = False
                            reconcile = True
                            notification = None
                    arguments = list(command)
                    if reconcile:
                        arguments.append("--reconcile")
                    if notification is not None:
                        arguments.extend(["--notification", json.dumps(notification)])
                    # Do not orphan a child if shutdown arrives inside Popen.
                    spawning = True
                    delivered = notification
                    try:
                        child = subprocess.Popen(arguments, start_new_session=True)
                        notification = None
                    finally:
                        spawning = False
                    if stopped.is_set():
                        stop(signal.SIGTERM, None)
                    returncode = child.wait()
                    child = None
                    reconcile = False
                    if returncode and not stopped.is_set():
                        logger.error(
                            "Processor child exited %s; check account, configuration and Tasks",
                            returncode,
                        )
                        if returncode == 2:
                            queue.close()
                            queue = control_queue
                            blocked = True
                            if delivered is not None and "job_id" in delivered:
                                publisher = Queue(capabilities, socket_timeout=5)
                                try:
                                    publisher.push(delivered)
                                finally:
                                    publisher.close()
                        elif returncode == 3:
                            queue.close()
                            reconnect = True
                            stopped.wait(backoff)
                            backoff = min(backoff * 2, 60)
                        else:
                            # Persistent failures must not recreate idle startup loops.
                            reconcile = not crash_recovery
                            crash_recovery = reconcile
                    else:
                        crash_recovery = False
                except RedisError:
                    if stopped.is_set():
                        break
                    logger.exception("Processor Redis connection failed; reconnecting")
                    queue.close()
                    reconnect = True
                    stopped.wait(backoff)
                    backoff = min(backoff * 2, 60)
        except _Stopped:
            pass
        finally:
            if notification is not None:
                publisher = Queue(capabilities, socket_timeout=5)
                try:
                    publisher.push(notification)
                except RedisError:
                    logger.exception(
                        "Could not retain interrupted processor wake; reconcile pending jobs"
                    )
                finally:
                    publisher.close()
            normal_queue.close()
            control_queue.close()
            for number, handler in previous.items():
                signal.signal(number, handler)
