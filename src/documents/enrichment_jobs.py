"""Optional metadata proposals applied by the core after durable computation."""

from uuid import UUID
from uuid import uuid4

from celery import shared_task
from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from filelock import FileLock

from documents.classification import validate_result
from documents.classification_operations import MODELS
from documents.classification_operations import candidates_for
from documents.classification_operations import configured_provider
from documents.classification_operations import provider_configuration
from documents.classification_operations import snapshot_document
from documents.job_state import PendingOperation
from documents.job_state import RejectedResult
from documents.job_state import check_permission as _check_permission
from documents.job_storage import job_directory
from documents.models import Document
from documents.models import DocumentJob
from documents.models import MatchingModel
from documents.operation_journal import invoke
from documents.operation_journal import json_value
from documents.operation_journal import operation_failure_status


def candidate_snapshot(document: Document, actor_id=None) -> dict:
    """Capture all permitted classification values in one immutable request."""
    actor = User.objects.get(pk=actor_id) if actor_id is not None else None
    return {
        field: [
            {"id": item.pk, "name": item.name}
            for item in candidates_for(document, field, actor)
            if item.matching_algorithm == MatchingModel.MATCH_AUTO
        ]
        for field in MODELS
    }


def submit_enrichment(
    document_id: int,
    *,
    actor_id=None,
    job_id=None,
    expected_provider=None,
    expected_configuration=None,
) -> DocumentJob:
    """Admit optional metadata without hiding the already imported document."""
    identifier = UUID(str(job_id)) if job_id else uuid4()
    with FileLock(job_directory(identifier).with_suffix(".lock"), timeout=0):
        return _stage_enrichment(
            document_id, identifier, actor_id, expected_provider, expected_configuration
        )


def _stage_enrichment(
    document_id, identifier, actor_id, expected_provider, expected_configuration
) -> DocumentJob:
    """Create a stable admission while holding the operation lock."""
    existing = DocumentJob.objects.filter(pk=identifier).first()
    if existing:
        if (
            existing.kind != "enrich"
            or existing.input.get("document_id") != document_id
            or existing.input.get("actor_id") != actor_id
        ):
            raise RejectedResult("Submission identifier belongs to another request")
        return existing
    document = Document.objects.get(pk=document_id)
    _check_permission(document, actor_id)
    identity = (
        expected_provider
        if expected_provider is not None
        else configured_provider().identity
    )
    if identity is None or (
        expected_provider is None and not settings.CLASSIFIER_ENABLED
    ):
        raise RejectedResult("No active metadata provider")
    request = {
        "version": 1,
        "revision": snapshot_document(document),
        "content": document.get_effective_content() or "",
        "metadata": {
            "document_id": document.pk,
            "title": document.title,
            "filename": document.original_filename,
            "owner_id": document.owner_id,
        },
        "candidates": candidate_snapshot(document, actor_id),
    }
    job = DocumentJob.objects.create(
        id=identifier,
        kind="enrich",
        timeout=settings.CELERY_TASK_TIME_LIMIT,
        input={"document_id": document.pk, "actor_id": actor_id, "request": request},
        configuration=expected_configuration
        if expected_configuration is not None
        else json_value(provider_configuration()),
        provider={"identity": list(identity)},
    )
    from documents.deferred_operations import resume

    resume(job)
    return job


def run_enrichment(job_id: str) -> None:
    """Resume inference and atomically apply proposals if the snapshot is current."""
    with FileLock(job_directory(job_id).with_suffix(".lock"), timeout=0):
        job = DocumentJob.objects.get(pk=job_id)
        if job.status in {
            "waiting",
            "cancelled",
            "succeeded",
            "rejected",
            "failed",
            "attention",
        }:
            return
        if job.status == DocumentJob.Status.COMMITTED:
            finish_enrichment(job)
            return
        try:
            provider = configured_provider()

            def configuration_matches() -> bool:
                """Keep recorded legacy routing when a provider gains a declaration."""
                current = json_value(provider_configuration())
                if len(job.configuration) == 3 and "metadata" in job.result.get(
                    "invocations", {}
                ):
                    current = current[:3]
                return current == job.configuration

            if (
                not configuration_matches()
                or json_value(provider.identity) != job.provider["identity"]
            ):
                raise RejectedResult("Metadata provider changed before execution")
            request = job.input["request"]

            def validate(result) -> None:
                """Reject provider changes, revoked access and concurrent edits."""
                document = Document.objects.get(pk=job.input["document_id"])
                _check_permission(document, job.input["actor_id"])
                validate_result(request, result, tuple(job.provider["identity"]))
                current_identity = (
                    provider.current_identity
                    if hasattr(provider, "current_identity")
                    else configured_provider().identity
                )
                if (
                    snapshot_document(document) != request["revision"]
                    or candidate_snapshot(document, job.input["actor_id"])
                    != request["candidates"]
                    or not configuration_matches()
                    or list(provider.identity) != job.provider["identity"]
                    or json_value(current_identity) != job.provider["identity"]
                ):
                    raise RejectedResult(
                        "Metadata input, permissions or provider changed"
                    )

            result = invoke(
                job,
                "metadata",
                job.provider["identity"][0],
                request,
                lambda identifier: provider.classify(json_value(request)),
                replay_safe=True,
                validate=validate,
                operation="metadata.classify",
                provider=provider,
            )
            with transaction.atomic():
                Document.objects.filter(pk=job.input["document_id"]).update(
                    modified=F("modified")
                )
                document = Document.objects.select_for_update().get(
                    pk=job.input["document_id"]
                )
                validate(result)
                for field, attribute in {
                    "correspondents": "correspondent_id",
                    "document_types": "document_type_id",
                    "storage_paths": "storage_path_id",
                }.items():
                    values = result["assignments"][field]
                    if values and getattr(document, attribute) is None:
                        setattr(document, attribute, values[0])
                document.save()
                document.tags.add(*result["assignments"]["tags"])
                job.refresh_from_db()
                job.status = DocumentJob.Status.COMMITTED
                job.updated = timezone.now()
                job.save(update_fields=["status", "updated"])
        except PendingOperation:
            return
        except Exception as error:
            job.refresh_from_db()
            job.status = (
                DocumentJob.Status.REJECTED
                if isinstance(error, RejectedResult)
                else operation_failure_status(job)
            )
            job.error = str(error)
            job.save(update_fields=["status", "error"])
            raise
        finish_enrichment(job)


def finish_enrichment(job: DocumentJob) -> None:
    """Refresh derived state after the atomic assignment without rerunning inference."""
    from documents.caching import clear_document_caches
    from documents.file_handling import reconcile_document_files
    from documents.search import get_backend

    document = Document.objects.get(pk=job.input["document_id"])
    reconcile_document_files(document)
    get_backend().add_or_update(document)
    clear_document_caches(document.pk)
    job.status = DocumentJob.Status.SUCCEEDED
    job.cleaned = True
    job.updated = timezone.now()
    job.save(update_fields=["status", "updated", "cleaned"])


@shared_task
def execute_enrichment(job_id: str) -> None:
    """Deliver a durable metadata identifier to the core coordinator."""
    run_enrichment(job_id)
