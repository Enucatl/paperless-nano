"""Durable ingestion phases and opaque invocation outcomes under the job lock."""

from collections.abc import Callable
from typing import Any
from uuid import uuid5

from django.utils import timezone

from documents.models import DocumentJob

PHASES = ("accepted", "preparing", "prepared", "committed", "completing", "complete")
STATUSES = ("queued", "running", "prepared", "committed", "committed", "succeeded")


def transition(job: DocumentJob, phase: str) -> None:
    """Advance one legal phase without discarding the phase on operational failure."""
    job.refresh_from_db()
    current = job.result.get("phase", "accepted")
    if phase != current and PHASES.index(phase) != PHASES.index(current) + 1:
        raise ValueError(f"Illegal ingestion transition: {current} -> {phase}")
    if (
        phase == "prepared"
        and "prepared" not in job.result
        and "payload" not in job.result
    ):
        raise ValueError(
            "Prepared requires retained outputs or a terminal stop outcome"
        )
    if phase in {"committed", "completing", "complete"} and "payload" not in job.result:
        raise ValueError("Committed ingestion requires its document or stop outcome")
    if phase == "complete" and any(
        entry["status"] != "succeeded"
        for entry in job.result.get("invocations", {}).values()
    ):
        raise ValueError("Complete requires successful invocation outcomes")
    job.result["phase"] = phase
    job.status = STATUSES[PHASES.index(phase)]
    job.updated = timezone.now()
    job.error = ""
    job.save(update_fields=["result", "status", "updated", "error"])


def invoke(
    job: DocumentJob,
    key: str,
    identity: str,
    inputs: dict,
    execute: Callable[[str], Any],
    *,
    replay_safe: bool = False,
    reconcile: Callable[[str], Any] | None = None,
    validate: Callable[[Any], None] | None = None,
) -> Any:
    """Record an opaque call; an exception never proves absence of external effects.

    The coordinator must hold the job's process lock. Reconciliation returns a
    validated outcome or raises; it never grants permission for unsafe replay.
    """
    from documents.ingestion_jobs import NeedsAttention
    from documents.ingestion_jobs import _json

    job.refresh_from_db()
    invocations = job.result.setdefault("invocations", {})
    entry = invocations.setdefault(
        key,
        {
            "id": str(uuid5(job.pk, key)),
            "plugin": identity,
            "input": _json(inputs),
            "status": "pending",
            "result": None,
            "error": "",
            "replay_safe": replay_safe,
        },
    )
    if entry["plugin"] != identity or entry["input"] != _json(inputs):
        raise NeedsAttention(f"Invocation {key} implementation or inputs changed")
    if entry["status"] == "succeeded":
        return entry["result"]
    replay_safe = replay_safe and entry["replay_safe"]
    if entry["status"] in {"running", "unknown", "failed"} and not replay_safe:
        entry["status"] = "unknown"
        job.save(update_fields=["result"])
        if reconcile is None:
            raise NeedsAttention(
                f"Invocation {key} ({entry['id']}) has an unknown outcome"
            )
        execute = reconcile
    entry["status"] = "running"
    job.save(update_fields=["result"])
    try:
        result = _json(execute(entry["id"]))
        if validate is not None:
            validate(result)
    except Exception as error:
        job.refresh_from_db()
        entry = job.result["invocations"][key]
        entry["status"] = "failed" if replay_safe else "unknown"
        entry["error"] = str(error)
        job.save(update_fields=["result"])
        raise
    job.refresh_from_db()
    entry = job.result["invocations"][key]
    entry.update(status="succeeded", result=result, error="")
    job.save(update_fields=["result"])
    return result
