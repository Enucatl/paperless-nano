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

from documents import enrichment_operations as rich_metadata
from documents.classification import validate_result
from documents.classification_operations import MODELS
from documents.classification_operations import candidates_for
from documents.classification_operations import snapshot_document
from documents.enrichment_operations import configured_provider
from documents.enrichment_operations import provider_configuration
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
    retagger=None,
) -> DocumentJob:
    """Admit optional metadata without hiding the already imported document."""
    identifier = UUID(str(job_id)) if job_id else uuid4()
    with FileLock(
        job_directory(identifier).with_suffix(".lock"), timeout=0, is_singleton=True
    ):
        return _stage_enrichment(
            document_id,
            identifier,
            actor_id,
            expected_provider,
            expected_configuration,
            retagger,
        )


def _stage_enrichment(
    document_id,
    identifier,
    actor_id,
    expected_provider,
    expected_configuration,
    retagger,
) -> DocumentJob:
    """Create a stable admission while holding the operation lock."""
    existing = DocumentJob.objects.filter(pk=identifier).first()
    if existing:
        if (
            existing.kind != "enrich"
            or existing.input.get("document_id") != document_id
            or existing.input.get("actor_id") != actor_id
            or existing.input.get("retagger") != retagger
        ):
            raise RejectedResult("Submission identifier belongs to another request")
        return existing
    document = Document.objects.get(pk=document_id)
    _check_permission(document, actor_id)
    identity = (
        expected_provider
        if expected_provider is not None
        else configured_provider(classification_only=retagger is not None).identity
    )
    if identity is None or (
        expected_provider is None
        and not (settings.ENRICHMENT_PROVIDER and retagger is None)
        and not settings.CLASSIFIER_ENABLED
    ):
        raise RejectedResult("No active metadata provider")
    request = {
        "version": 1,
        "revision": snapshot_document(document),
        "content": (
            document.suggestion_content
            if retagger
            else document.get_effective_content()
        )
        or "",
        "metadata": {
            "document_id": document.pk,
            "title": document.title,
            "filename": document.original_filename,
            "owner_id": document.owner_id,
        },
        "candidates": candidate_snapshot(document, actor_id),
    }
    configuration = (
        expected_configuration
        if expected_configuration is not None
        else json_value(
            provider_configuration(classification_only=retagger is not None)
        )
    )
    rich = isinstance(identity, dict)
    if rich:
        request = rich_metadata.make_request(
            document, actor_id, identity, configuration
        )
    job = DocumentJob.objects.create(
        id=identifier,
        kind="enrich",
        timeout=settings.CELERY_TASK_TIME_LIMIT,
        input={
            "document_id": document.pk,
            "actor_id": actor_id,
            "request": request,
            **(
                {"retagger": retagger, "rules": rule_snapshot(document)}
                if retagger
                else {}
            ),
        },
        configuration=configuration,
        provider={
            "identity": json_value(identity),
            **({"contract": "enrichment"} if rich else {}),
        },
    )
    from documents.deferred_operations import resume

    resume(job)
    return job


def run_enrichment(job_id: str) -> None:
    """Resume inference and atomically apply proposals if the snapshot is current."""
    with FileLock(
        job_directory(job_id).with_suffix(".lock"), timeout=0, is_singleton=True
    ):
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
        rich = job.provider.get("contract") == "enrichment"
        classification_only = "retagger" in job.input
        try:
            provider = configured_provider(classification_only=classification_only)

            def configuration_matches() -> bool:
                """Reject changes to the retained effective configuration."""
                current = json_value(
                    provider_configuration(classification_only=classification_only)
                )
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
                if rich:
                    from documents.deferred_operations import authorize

                    current_job = DocumentJob.objects.get(pk=job.pk)
                    entry = current_job.result.get("invocations", {}).get(
                        "metadata", {}
                    )
                    if "processor" in entry and "reconciliation" not in entry:
                        processor = User.objects.filter(pk=entry["processor"]).first()
                        if processor is None:
                            raise RejectedResult("Processor account no longer exists")
                        authorize(entry, processor)
                if rich:
                    rich_metadata.validate(
                        document,
                        job.input["actor_id"],
                        request,
                        result,
                        job.provider["identity"],
                    )
                else:
                    validate_result(request, result, tuple(job.provider["identity"]))
                current_identity = (
                    provider.current_identity
                    if hasattr(provider, "current_identity")
                    else configured_provider(
                        classification_only=classification_only
                    ).identity
                )
                if (
                    (
                        "retagger" in job.input
                        and rule_snapshot(document) != job.input["rules"]
                    )
                    or (
                        not rich
                        and (
                            snapshot_document(document) != request["revision"]
                            or candidate_snapshot(document, job.input["actor_id"])
                            != request["candidates"]
                        )
                    )
                    or not configuration_matches()
                    or json_value(provider.identity) != job.provider["identity"]
                    or json_value(current_identity) != job.provider["identity"]
                ):
                    raise RejectedResult(
                        "Metadata input, permissions or provider changed"
                    )

            result = invoke(
                job,
                "metadata",
                job.provider["identity"]["provider"]
                if rich
                else job.provider["identity"][0],
                request,
                lambda identifier: (provider.enrich if rich else provider.classify)(
                    json_value(request)
                ),
                replay_safe=getattr(provider, "replay_safe", False) if rich else True,
                validate=validate,
                operation="metadata.enrich" if rich else "metadata.classify",
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
                if rich:
                    changed = rich_metadata.apply(
                        document, job.input["actor_id"], request, result, job.pk
                    )
                elif "retagger" in job.input:
                    apply_retagger(
                        document,
                        job.input["retagger"],
                        result,
                        tuple(job.provider["identity"]),
                    )
                else:
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
                if rich:
                    job.result["metadata_changed"] = changed
                job.status = DocumentJob.Status.COMMITTED
                job.updated = timezone.now()
                job.save(update_fields=["status", "updated", "result"])
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
    changed = job.result.get("metadata_changed", True)
    if changed and job.provider.get("contract") == "enrichment":
        from documents.updates import document_update

        def followup(identifier: str) -> None:
            """Run existing workflows and notifications once after metadata commit."""
            with document_update(
                document,
                sender=run_enrichment,
                logging_group=UUID(identifier),
                index=False,
            ):
                pass

        try:
            invoke(
                job,
                "metadata:followup",
                "paperless.document_update:1",
                {"document_id": document.pk},
                followup,
                allow_deferred=False,
            )
        except Exception as error:
            job.refresh_from_db()
            job.status = DocumentJob.Status.ATTENTION
            job.error = "Metadata committed; follow-up effects require review: " + str(
                error
            )
            job.save(update_fields=["status", "error"])
            raise
        document.refresh_from_db()
    if changed:
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


def rule_snapshot(document: Document) -> dict:
    """Retain rule matching inputs as well as the classifier candidate allowlist."""
    from documents.classification_operations import fingerprint

    return {
        field: fingerprint(list(model.objects.order_by("pk").values()))
        for field, model in MODELS.items()
    }


def apply_retagger(
    document: Document, options: dict, result: dict, identity: tuple
) -> None:
    """Apply validated proposals through the existing rule and replacement policy."""
    from documents.signals.handlers import set_correspondent
    from documents.signals.handlers import set_document_type
    from documents.signals.handlers import set_storage_path
    from documents.signals.handlers import set_tags

    class RetainedClassification:
        """Supply accepted assignments to the core setters without new inference."""

        def __init__(self):
            self.identity = identity

        def classify(self, request):
            return {
                **result,
                "revision": request["revision"],
                "assignments": {
                    field: result["assignments"][field]
                    for field in request["candidates"]
                },
            }

    classifier = RetainedClassification()
    for field, setter in {
        "correspondent": set_correspondent,
        "document_type": set_document_type,
        "tags": set_tags,
        "storage_path": set_storage_path,
    }.items():
        if options[field]:
            kwargs = {"replace": options["overwrite"], "classifier": classifier}
            if field != "tags":
                kwargs["use_first"] = options["use_first"]
            setter(None, document, **kwargs)
