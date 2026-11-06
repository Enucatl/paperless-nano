"""Publish work only after its transaction and outer job lock have finished."""

import logging
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar

from django.db import transaction

from paperless_services.processor_queue import CAPABILITIES
from paperless_services.processor_queue import Queue
from paperless_services.processor_queue import capability_for

logger = logging.getLogger("paperless.processor")
_callbacks = ContextVar("processor_notifications", default=None)


@contextmanager
def defer_notifications():
    """Collect nested handoffs until the outermost job runner releases its lock."""
    if _callbacks.get() is not None:
        yield
        return
    callbacks = []
    token = _callbacks.set(callbacks)
    try:
        yield
    finally:
        _callbacks.reset(token)
        for callback in callbacks:
            transaction.on_commit(callback, robust=True)


def after_unlock(callback: Callable[[], None]) -> None:
    """Schedule a committed handoff outside any enclosing job lock."""

    def ready() -> None:
        callbacks = _callbacks.get()
        if callbacks is None:
            callback()
        else:
            callbacks.append(callback)

    transaction.on_commit(ready, robust=True)


def notify_pending(job_id) -> None:
    """Publish current pending operations; notifications never authorize work."""

    def publish() -> None:
        from documents.models import DocumentJob

        job = DocumentJob.objects.filter(
            pk=job_id, status=DocumentJob.Status.WAITING
        ).first()
        if job is None:
            return
        queue = Queue(CAPABILITIES, socket_timeout=5)
        try:
            for key, entry in job.result.get("invocations", {}).items():
                if (
                    entry.get("status") == "pending"
                    and entry.get("version") == 2
                    and "policy" in entry
                ):
                    queue.push(
                        {
                            "job_id": str(job.pk),
                            "key": key,
                            "capability": capability_for(entry["operation"]),
                        }
                    )
        except Exception:
            logger.exception("Job %s is durable but its processor wake failed", job_id)
        finally:
            queue.close()

    after_unlock(publish)


def wake_processor(username: str) -> None:
    """Wake newly provisioned accounts to reconcile previously waiting work."""

    def publish() -> None:
        queue = Queue(CAPABILITIES, socket_timeout=5)
        try:
            queue.push({"user": username, "reconcile": True})
        finally:
            queue.close()

    after_unlock(publish)
