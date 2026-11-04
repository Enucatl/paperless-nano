"""Durable remote execution of the same invocations used by local providers."""

import datetime
import secrets
from contextlib import contextmanager

from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.utils import timezone
from filelock import FileLock
from filelock import Timeout

from documents.job_state import PendingOperation  # noqa: F401
from documents.job_state import execution_policy  # noqa: F401
from documents.job_state import requester_id
from documents.job_state import wait  # noqa: F401
from documents.models import DocumentJob


class OperationConflict(ValueError):
    """An expired, cancelled or changed operation cannot accept this request."""


def authorize(entry: dict, user) -> None:
    """Require an explicit operation grant even for trusted actorless jobs."""
    if not user.is_active or not (
        user.is_superuser or user.pk in entry.get("policy", {}).get("users", [])
    ):
        raise PermissionDenied("This processor is not authorized for the operation")


@contextmanager
def locked_operation(job_id, key: str, user):
    """Serialize remote mutation with the core coordinator on shared storage."""
    from documents.job_storage import job_directory

    with (
        FileLock(job_directory(job_id).with_suffix(".lock"), timeout=0),
        transaction.atomic(),
    ):
        job = DocumentJob.objects.select_for_update().get(pk=job_id)
        entry = job.result.get("invocations", {}).get(key)
        if entry is None or "policy" not in entry:
            raise OperationConflict("No remote invocation with this key")
        authorize(entry, user)
        yield job, entry


def claim(job_id, key: str, user) -> dict:
    """Start a bounded attempt; time spent queued never consumes attempts."""
    with locked_operation(job_id, key, user) as (job, entry):
        if job.status == DocumentJob.Status.CANCELLED:
            raise OperationConflict("Job was cancelled")
        if entry["status"] == "leased":
            if timezone.now() < datetime.datetime.fromisoformat(entry["lease_until"]):
                raise OperationConflict("Operation already has an active claim")
            if not entry["replay_safe"]:
                entry["status"] = "unknown"
                job.status = DocumentJob.Status.ATTENTION
                job.error = "Expired effectful claim requires reconciliation"
                job.save(update_fields=["result", "status", "error"])
                return {"status": "unknown", "id": entry["id"]}
            entry["status"] = "pending"
        if entry["status"] != "pending":
            raise OperationConflict("Operation is not pending")
        attempts = entry.get("attempts", 0)
        if attempts >= 3:
            entry["status"] = "failed"
            job.status = DocumentJob.Status.FAILED
            job.error = "Operation exhausted three execution attempts"
            job.save(update_fields=["result", "status", "error"])
            return {"status": "failed", "id": entry["id"]}
        entry.update(
            status="leased",
            token=secrets.token_urlsafe(32),
            processor=user.pk,
            attempts=attempts + 1,
            lease_until=(
                timezone.now() + datetime.timedelta(seconds=job.timeout or 1800)
            ).isoformat(),
        )
        job.save(update_fields=["result"])
        return dict(entry)


def check_claim(job: DocumentJob, entry: dict, user, token: str) -> None:
    """Reject late results and claims invalidated by cancellation or reassignment."""
    if (
        job.status == DocumentJob.Status.CANCELLED
        or entry["status"] != "leased"
        or entry.get("processor") != user.pk
        or not secrets.compare_digest(entry.get("token", ""), token)
        or timezone.now() >= datetime.datetime.fromisoformat(entry["lease_until"])
    ):
        raise OperationConflict("Claim expired or is no longer current")


def resume(job: DocumentJob) -> None:
    """Record readiness before best-effort dispatch; recovery closes send gaps."""
    from documents.jobs import _dispatch

    job.status = job.result.pop("resume_status", DocumentJob.Status.QUEUED)
    job.updated = timezone.now()
    job.save(update_fields=["result", "status", "updated"])

    def dispatch() -> None:
        """A broker outage leaves a resumable database journal."""
        import logging

        try:
            _dispatch(job)
        except Exception:
            logging.getLogger("paperless.jobs").exception(
                "Deferred result dispatch failed"
            )

    transaction.on_commit(dispatch)


def submit_result(job_id, key: str, user, token: str, result) -> dict:
    """Stage an immutable result; only the core validator can apply it."""
    from documents.operation_journal import json_value

    result = json_value(result)
    with locked_operation(job_id, key, user) as (job, entry):
        if (
            entry["status"] in {"staged", "succeeded"}
            and entry.get("processor") == user.pk
            and secrets.compare_digest(entry.get("token", ""), token)
            and entry["result"] == result
        ):
            return {"status": entry["status"], "id": entry["id"]}
        check_claim(job, entry, user, token)
        entry.update(status="staged", result=result)
        resume(job)
        return {"status": "staged", "id": entry["id"]}


def cancel(job_id, user) -> None:
    """Cancel pending work without treating uncertain effects as undone."""
    from documents.job_storage import job_directory

    with (
        FileLock(job_directory(job_id).with_suffix(".lock"), timeout=0),
        transaction.atomic(),
    ):
        job = DocumentJob.objects.select_for_update().get(pk=job_id)
        if not user.is_active or not (
            user.is_superuser or requester_id(job) == user.pk
        ):
            raise PermissionDenied("Only the requester can cancel this job")
        if job.status in {
            DocumentJob.Status.SUCCEEDED,
            DocumentJob.Status.COMMITTED,
        }:
            raise OperationConflict("Published work cannot be cancelled")
        for entry in job.result.get("invocations", {}).values():
            if entry["status"] == "leased" and not entry["replay_safe"]:
                entry["status"] = "unknown"
            elif entry["status"] != "succeeded":
                entry["status"] = "cancelled"
        job.status = DocumentJob.Status.CANCELLED
        job.save(update_fields=["result", "status"])


def reconcile(job_id, key: str, user, result, evidence: str) -> dict:
    """Record an administrator's verified external outcome without replaying it."""
    from documents.operation_journal import json_value

    if not user.is_active or not user.is_superuser:
        raise PermissionDenied("Only administrators can reconcile external effects")
    if not isinstance(evidence, str) or not evidence.strip():
        raise OperationConflict(
            "Reconciliation requires evidence of the external outcome"
        )
    with locked_operation(job_id, key, user) as (job, entry):
        if entry["status"] != "unknown":
            raise OperationConflict("Only unknown effects can be reconciled")
        entry.update(
            result=json_value(result),
            reconciliation={"actor_id": user.pk, "evidence": evidence},
            status="staged",
        )
        if job.status == DocumentJob.Status.CANCELLED:
            entry["status"] = "reconciled_cancelled"
            job.save(update_fields=["result"])
        else:
            resume(job)
        return {"id": entry["id"], "status": entry["status"]}


def recover_waiting() -> None:
    """Distinguish untouched offline work from expired real execution attempts."""
    from documents.job_storage import job_directory

    for identifier in (
        DocumentJob.objects.filter(status=DocumentJob.Status.WAITING)
        .values_list("pk", flat=True)
        .iterator()
    ):
        try:
            with (
                FileLock(job_directory(identifier).with_suffix(".lock"), timeout=0),
                transaction.atomic(),
            ):
                job = DocumentJob.objects.select_for_update().get(pk=identifier)
                if job.status != DocumentJob.Status.WAITING:
                    continue
                for entry in job.result.get("invocations", {}).values():
                    if entry[
                        "status"
                    ] != "leased" or timezone.now() < datetime.datetime.fromisoformat(
                        entry["lease_until"]
                    ):
                        continue
                    entry["status"] = "pending" if entry["replay_safe"] else "unknown"
                    if not entry["replay_safe"]:
                        job.status = DocumentJob.Status.ATTENTION
                        job.error = "Expired effectful claim requires reconciliation"
                job.save(update_fields=["result", "status", "error"])
        except Timeout:
            continue
