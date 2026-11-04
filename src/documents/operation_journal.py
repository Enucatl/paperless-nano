"""Durable finite invocation outcomes, independent of domain and task adapters."""

import json
from collections.abc import Callable
from typing import Any
from uuid import uuid5

from django.core.serializers.json import DjangoJSONEncoder

from documents.job_state import RejectedResult
from documents.models import DocumentJob


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
    operation: str | None = None,
    allow_deferred: bool = True,
    provider=None,
) -> Any:
    """Record an opaque call; an exception never proves absence of external effects.

    The coordinator must hold the job's process lock. Reconciliation returns a
    validated outcome or raises; it never grants permission for unsafe replay.
    """

    from documents.job_state import PendingOperation
    from documents.job_state import execution_policy
    from documents.job_state import requester_id
    from documents.job_state import wait

    job.refresh_from_db()
    invocations = job.result.setdefault("invocations", {})
    new_invocation = key not in invocations
    entry = invocations.setdefault(
        key,
        {
            "id": str(uuid5(job.pk, key)),
            "plugin": identity,
            "input": json_value(inputs),
            "status": "pending",
            "result": None,
            "error": "",
            "replay_safe": replay_safe,
            "operation": operation or key,
            "version": 1,
            "configuration": json_value(job.configuration),
            "authorization": {"actor_id": requester_id(job)},
            "dependencies": [
                item["id"]
                for item in invocations.values()
                if item["status"] == "succeeded"
            ],
        },
    )
    if entry["plugin"] != identity or entry["input"] != json_value(inputs):
        raise NeedsAttention(f"Invocation {key} implementation or inputs changed")
    if entry["status"] == "succeeded":
        return entry["result"]
    if new_invocation and allow_deferred:
        policy = execution_policy(operation or key, identity, provider)
        if policy is not None:
            entry["policy"] = json_value(policy)
    if "policy" in entry:
        if entry["status"] in {"unknown", "failed", "conflicted", "cancelled"}:
            raise NeedsAttention(
                f"Invocation {key} requires reconciliation: {entry['status']}"
            )
        if entry["status"] != "staged":
            wait(job, entry)
        result = entry["result"]
        try:
            if validate is not None:
                validate(result)
        except Exception as error:
            entry.update(status="conflicted", error=str(error))
            job.status = DocumentJob.Status.REJECTED
            job.error = str(error)
            job.save(update_fields=["result", "status", "error"])
            raise
        entry.update(status="succeeded", error="")
        job.save(update_fields=["result"])
        return result
    if entry["status"] == "conflicted":
        raise RejectedResult(entry["error"])
    replay_safe = replay_safe and entry["replay_safe"]
    if replay_safe and entry.get("attempts", 0) >= 3:
        entry["status"] = "failed"
        job.save(update_fields=["result"])
        raise NeedsAttention(f"Invocation {key} exhausted three execution attempts")
    if entry["status"] in {"running", "unknown", "failed"} and not replay_safe:
        entry["status"] = "unknown"
        job.save(update_fields=["result"])
        if reconcile is None:
            raise NeedsAttention(
                f"Invocation {key} ({entry['id']}) has an unknown outcome"
            )
        execute = reconcile
    entry["status"] = "running"
    entry["attempts"] = entry.get("attempts", 0) + 1
    job.save(update_fields=["result"])
    validating = False
    try:
        result = json_value(execute(entry["id"]))
        validating = True
        if validate is not None:
            validate(result)
    except PendingOperation:
        # Nested orchestration is replayable; its finite child owns the claim.
        job.refresh_from_db()
        entry = job.result["invocations"][key]
        entry["status"] = "pending"
        entry["attempts"] -= 1
        job.save(update_fields=["result"])
        raise
    except Exception as error:
        job.refresh_from_db()
        entry = job.result["invocations"][key]
        entry["status"] = (
            "conflicted" if validating else ("failed" if replay_safe else "unknown")
        )
        entry["error"] = str(error)
        job.save(update_fields=["result"])
        raise
    job.refresh_from_db()
    entry = job.result["invocations"][key]
    entry.update(status="succeeded", result=result, error="")
    job.save(update_fields=["result"])
    return result


def operation_failure_status(job: DocumentJob) -> str:
    """Keep safe computation retries distinct from conflicts and uncertain effects."""
    entries = list(job.result.get("invocations", {}).values())
    if any(entry["status"] == "unknown" for entry in entries):
        return DocumentJob.Status.ATTENTION
    if any(entry["status"] == "conflicted" for entry in entries):
        return DocumentJob.Status.REJECTED
    failed = [entry for entry in entries if entry["status"] == "failed"]
    if failed:
        return (
            DocumentJob.Status.FAILED
            if any(entry.get("attempts", 0) >= 3 for entry in failed)
            else DocumentJob.Status.QUEUED
        )
    return DocumentJob.Status.ATTENTION


class NeedsAttention(Exception):
    """An external effect may have happened and must not be repeated automatically."""


def json_value(value: object) -> object:
    """Normalize model inputs to JSON primitives, including dates and decimal values."""
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))
