"""Core selection, authorization and persistence for rich metadata proposals."""

import json
from contextlib import nullcontext
from datetime import date

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.utils import timezone
from django.utils.module_loading import import_string

from documents.classification_operations import candidates_for
from documents.classification_operations import fingerprint
from documents.classification_operations import snapshot_document
from documents.enrichment import InvalidEnrichmentResult
from documents.enrichment import validate_result
from documents.job_state import RejectedResult
from documents.models import Correspondent
from documents.models import CustomField
from documents.models import CustomFieldInstance
from documents.models import Document
from documents.models import Tag
from documents.operation_journal import json_value
from documents.updates import document_metadata_update
from documents.updates import save_custom_field_value
from documents.updates import save_document_metadata
from paperless.version import __full_version_str__


def configured_provider(*, classification_only: bool = False):
    """Select rich enrichment explicitly, retaining legacy classification fallback."""
    if classification_only or not settings.ENRICHMENT_PROVIDER:
        from documents.classification_operations import configured_provider as classify

        return classify()
    provider = import_string(settings.ENRICHMENT_PROVIDER)()
    identity = provider.identity
    if (
        not isinstance(identity, dict)
        or set(identity) != {"provider", "model", "prompt"}
        or any(not isinstance(value, str) or not value for value in identity.values())
    ):
        raise RejectedResult("Invalid rich metadata provider identity")
    return provider


def provider_configuration(*, classification_only: bool = False):
    """Retain the selected implementation and secret-free effective settings."""
    if classification_only or not settings.ENRICHMENT_PROVIDER:
        from documents.classification_operations import (
            provider_configuration as classify,
        )

        return classify()
    provider = configured_provider()
    configuration = provider.configuration
    if not isinstance(configuration, dict):
        raise RejectedResult("Rich metadata configuration must be an object")
    remote = getattr(provider, "uses_remote_service", True)
    replay_safe = getattr(provider, "replay_safe", False)
    if type(remote) is not bool or type(replay_safe) is not bool:
        raise RejectedResult("Rich metadata execution declarations must be boolean")
    if remote and not settings.ENRICHMENT_ALLOW_REMOTE:
        raise RejectedResult("Remote rich metadata inference is not permitted")
    try:
        return json.loads(
            json.dumps(
                {
                    "provider": settings.ENRICHMENT_PROVIDER,
                    "configuration": configuration,
                    "allow_remote": settings.ENRICHMENT_ALLOW_REMOTE,
                    "uses_remote_service": remote,
                    "replay_safe": replay_safe,
                },
                allow_nan=False,
            )
        )
    except (TypeError, ValueError) as error:
        raise RejectedResult(
            "Rich metadata configuration must be finite JSON"
        ) from error


def managed_fields() -> dict:
    """Snapshot existing fields by exact name, reserving OCR provenance for core."""
    fields = CustomField.objects.all()
    provenance = settings.OCR_PROVENANCE_FIELD
    if provenance not in (None, ""):
        fields = fields.exclude(pk=provenance)
    return {
        field.name: {"id": field.pk, "data_type": field.data_type} for field in fields
    }


def snapshot(document: Document) -> str:
    """Include custom-field edits which the classification-only revision omits."""
    return fingerprint(
        {
            "document": snapshot_document(document),
            "custom_fields": list(document.custom_fields.order_by("pk").values()),
        }
    )


def candidates(document: Document, actor_id: int | None) -> dict:
    """Expose permitted correspondents and tags regardless of matching algorithm."""
    actor = User.objects.get(pk=actor_id) if actor_id is not None else None
    return {
        field: [
            {"id": item.pk, "name": item.name}
            for item in candidates_for(document, field, actor)
        ]
        for field in ("correspondents", "tags")
    }


def make_request(
    document: Document, actor_id: int | None, identity: dict, configuration: dict
) -> dict:
    """Capture one immutable request including existing managed and unrelated values."""
    return json_value(
        {
            "version": 1,
            "revision": snapshot(document),
            "content": document.get_effective_content() or "",
            "metadata": {
                "document_id": document.pk,
                "root_document_id": document.root_document_id,
                "title": document.title,
                "document_date": document.created,
                "filename": document.original_filename,
                "owner_id": document.owner_id,
                "paperless_version": __full_version_str__,
                "correspondent_id": document.correspondent_id,
                "tags": list(document.tags.values_list("pk", flat=True)),
                "custom_fields": {
                    str(item.field_id): item.value
                    for item in document.custom_fields.all()
                },
            },
            "candidates": candidates(document, actor_id),
            "managed_fields": managed_fields(),
            "configuration": configuration,
            "identity": identity,
            "date": timezone.now().date().isoformat(),
            "document_date_max": date.today().isoformat(),
        }
    )


def validate(
    document: Document,
    actor_id: int | None,
    request: dict,
    result: dict,
    identity: dict,
) -> None:
    """Reject malformed proposals, changed fields, revoked relations and stale edits."""
    try:
        validate_result(request, result, identity)
    except InvalidEnrichmentResult as error:
        raise RejectedResult(str(error)) from error
    current = candidates(document, actor_id)
    if (
        snapshot(document) != request["revision"]
        or managed_fields() != request["managed_fields"]
        or any(
            item not in current[field]
            for field, items in request["candidates"].items()
            for item in items
        )
    ):
        raise RejectedResult(
            "Rich metadata input, fields or relation permissions changed"
        )
    actor = User.objects.get(pk=actor_id) if actor_id is not None else None
    for key, model in (("correspondent", Correspondent), ("language", Tag)):
        if (
            "create" in result.get(key, {})
            and actor is not None
            and not actor.has_perm(f"documents.add_{model._meta.model_name}")
        ):
            raise RejectedResult(
                f"The requesting user cannot create {model._meta.verbose_name}"
            )


def resolve_relation(document: Document, actor_id: int | None, model, proposal: dict):
    """Reuse a unique owner-scoped creation, never a hidden same-name entity."""
    if "id" in proposal:
        return model.objects.get(pk=proposal["id"])
    defaults = {"matching_algorithm": 0}
    if model is Tag:
        defaults["color"] = "#166534"
    instance, _ = model.objects.get_or_create(
        name=proposal["create"],
        owner_id=actor_id if actor_id is not None else document.owner_id,
        defaults=defaults,
    )
    field = "tags" if model is Tag else "correspondents"
    if instance.pk not in {
        item["id"] for item in candidates(document, actor_id)[field]
    }:
        raise RejectedResult("A concurrent relation creation is not accessible")
    return instance


def apply(
    document: Document, actor_id: int | None, request: dict, result: dict, job_id
) -> bool:
    """Persist a validated proposal inside the caller's atomic commit without file IO."""
    values = {}
    if "title" in result:
        values["title"] = result["title"]
    if "document_date" in result:
        values["created"] = date.fromisoformat(result["document_date"])
    outcome = {"job_id": str(job_id)}
    if "correspondent" in result:
        values["correspondent"] = resolve_relation(
            document, actor_id, Correspondent, result["correspondent"]
        )
        outcome["correspondent_id"] = values["correspondent"].pk
    if "language" in result:
        language = resolve_relation(document, actor_id, Tag, result["language"])
        managed = {
            item["id"]
            for item in request["candidates"]["tags"]
            if item["name"].startswith("language:")
        }
        retained = list(document.tags.exclude(pk__in=managed))
        # Removing an ancestor would also remove an unrelated descendant.
        ancestors = {parent for tag in retained for parent in tag.get_ancestors()}
        values["tags"] = [*retained, *ancestors, language]
        outcome["language_tag_id"] = language.pk
    fields = []
    proposed = {
        item["field"]: item["value"] for item in result.get("custom_fields", [])
    }
    for role in ("summary", "processing_date", "audit"):
        if role in result and role in request["managed_fields"]:
            value = result[role]
            if role == "audit":
                value = json.dumps(
                    {**value, "application": outcome}, ensure_ascii=False
                )
            proposed[request["managed_fields"][role]["id"]] = value
    for pk, value in proposed.items():
        field = CustomField.objects.get(pk=pk)
        storage = CustomFieldInstance._meta.get_field(
            CustomFieldInstance.get_value_field_name(field.data_type)
        )
        try:
            value = storage.to_python(value)
            storage.run_validators(value)
        except ValidationError as error:
            raise RejectedResult(
                f"Invalid value for managed field {field.name}: {error}"
            ) from error
        fields.append((field, value))
    if values or fields:
        from auditlog.context import set_actor

        actor = User.objects.get(pk=actor_id) if actor_id is not None else None
        with (
            set_actor(actor) if settings.AUDIT_LOG_ENABLED else nullcontext(),
            document_metadata_update(document, reconcile_files=False),
        ):
            save_document_metadata(document, values, user=actor)
            for field, value in fields:
                save_custom_field_value(document, field, value)
    return bool(values or fields)
