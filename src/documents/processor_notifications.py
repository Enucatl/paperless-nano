"""Publish work only after its transaction and outer job lock have finished."""

import logging
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar

from django.db import transaction

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
    """Dispatch eligible server work after committing and releasing job locks."""
    after_unlock(lambda: dispatch_pending(job_id))


def dispatch_pending(job_id) -> None:
    """Leave workstation-only work untouched; recover broker gaps from the journal."""
    from filelock import FileLock
    from filelock import Timeout

    from documents.deferred_operations import resume
    from documents.job_storage import job_directory
    from documents.models import DocumentJob
    from documents.server_processor import selected_user

    try:
        with (
            defer_notifications(),
            FileLock(job_directory(job_id).with_suffix(".lock"), timeout=0),
            transaction.atomic(),
        ):
            job = (
                DocumentJob.objects.select_for_update()
                .filter(pk=job_id, status=DocumentJob.Status.WAITING)
                .first()
            )
            if job is None:
                return
            if any(
                entry.get("status") == "pending"
                and entry.get("version") == 2
                and "policy" in entry
                and selected_user(entry) is not None
                for entry in job.result.get("invocations", {}).values()
            ):
                resume(job)
    except Timeout:
        # The current owner or recovery will publish any remaining server work.
        return
    except Exception:
        logger.exception("Job %s is durable but its Celery dispatch failed", job_id)


def wake_processor(username: str) -> None:
    """Reconcile pending placement after granting a server account capabilities."""
    from django.conf import settings

    if username != settings.PROCESSOR_USER:
        return

    def publish() -> None:
        from documents.models import DocumentJob

        for identifier in list(
            DocumentJob.objects.filter(status=DocumentJob.Status.WAITING).values_list(
                "pk", flat=True
            )
        ):
            dispatch_pending(identifier)

    after_unlock(publish)
