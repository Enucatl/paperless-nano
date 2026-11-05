"""Core snapshots, selection and validation around replaceable inference."""

import json
from datetime import UTC
from datetime import datetime
from functools import wraps
from hashlib import sha256

from django.conf import settings
from django.contrib.auth.models import User
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.utils.module_loading import import_string

from documents.classification import ClassificationRequest
from documents.classification import InvalidClassificationResult
from documents.classification import validate_result
from documents.models import Correspondent
from documents.models import Document
from documents.models import DocumentType
from documents.models import MatchingModel
from documents.models import StoragePath
from documents.models import Tag
from documents.permissions import permitted_object_ids

MODELS = {
    "correspondents": Correspondent,
    "document_types": DocumentType,
    "tags": Tag,
    "storage_paths": StoragePath,
}


def fingerprint(value: object) -> str:
    """Hash a serializable snapshot with stable ordering and timestamp precision."""
    return sha256(
        json.dumps(value, cls=DjangoJSONEncoder, sort_keys=True).encode(),
    ).hexdigest()


def snapshot_document(document: Document) -> str:
    """Capture source values and relations, including effective version content."""
    fields = {}
    for field in Document._meta.concrete_fields:
        if field.generated:
            continue
        # Compare persisted values: DateField also accepts datetime on creation.
        value = field.get_prep_value(getattr(document, field.attname))
        if isinstance(value, datetime) and value.tzinfo is not None:
            value = value.astimezone(UTC)
        fields[field.attname] = value
    return fingerprint(
        {
            "fields": fields,
            "content": document.get_effective_content(),
            "tags": list(document.tags.order_by("pk").values_list("pk", flat=True))
            if document.pk
            else [],
            "versions": list(
                document.versions.order_by("pk").values("pk", "modified", "content"),
            )
            if document.pk
            else [],
            "root": list(
                Document.objects.filter(pk=document.root_document_id).values(),
            )
            if document.root_document_id
            else [],
        },
    )


def assert_document_unchanged(document: Document, revision: str) -> None:
    """Reject deleted documents, newer edits and stale prefetched content."""
    current = (
        Document.objects.filter(pk=document.pk).first() if document.pk else document
    )
    if current is None or snapshot_document(current) != revision:
        raise InvalidClassificationResult("Document changed during classification")


def candidates_for(document: Document, field: str, user=None) -> list:
    """Resolve current permissions in the core using the existing matching policy."""
    model = MODELS[field]
    if user is None:
        user = document.owner
    queryset = model.objects.all()
    if user is not None:
        # Do not reuse a request user's cached permissions after provider execution.
        user = User.objects.filter(pk=user.pk, is_active=True).first()
        if user is None:
            return []
        queryset = queryset.filter(
            pk__in=permitted_object_ids(user, model, f"view_{model._meta.model_name}"),
        )
    return list(queryset)


def configured_provider():
    """Construct the selected implementation through its public contract."""
    provider = import_string(
        settings.CLASSIFICATION_PROVIDER
        or "documents.classification_providers.SklearnProvider"
    )()
    identity = provider.identity
    if identity is not None and (
        not isinstance(identity, tuple)
        or len(identity) != 2
        or any(not isinstance(value, str) or not value for value in identity)
    ):
        raise InvalidClassificationResult("Invalid classification provider identity")
    return provider


def provider_configuration() -> tuple:
    """Snapshot selection and implementation-declared effective configuration."""
    provider = configured_provider()
    configuration = getattr(provider, "configuration", {})
    if not isinstance(configuration, dict):
        raise InvalidClassificationResult(
            "Classification configuration must be an object"
        )
    try:
        configuration = json.loads(json.dumps(configuration, allow_nan=False))
    except (ValueError, TypeError) as error:
        raise InvalidClassificationResult(
            "Classification configuration must be JSON"
        ) from error
    snapshot = (
        settings.CLASSIFICATION_PROVIDER,
        settings.CLASSIFIER_ENABLED,
        configuration,
    )
    return snapshot


def active_provider_identity() -> tuple[str, str] | None:
    """Read the selected implementation's current activation identity."""
    if not settings.CLASSIFIER_ENABLED:
        return None
    return configured_provider().identity


def load_classification_provider():
    """Select inference independently from training and execution transport."""
    if not settings.CLASSIFIER_ENABLED:
        return None
    provider = configured_provider()
    return provider if provider.identity is not None else None


def proposed_assignments(
    document: Document,
    field: str,
    classifier,
    user=None,
) -> tuple[list[MatchingModel], list[int]]:
    """Execute one candidate snapshot and reject stale or forbidden proposals."""
    candidates = candidates_for(document, field, user)
    if classifier is None:
        return candidates, []
    configuration = provider_configuration()
    revision = snapshot_document(document)
    assert_document_unchanged(document, revision)
    provider = classifier
    identity = provider.identity
    if (
        not isinstance(identity, tuple)
        or len(identity) != 2
        or any(not isinstance(value, str) or not value for value in identity)
    ):
        raise InvalidClassificationResult("Invalid classification provider identity")
    request = ClassificationRequest(
        version=1,
        revision=revision,
        content=document.suggestion_content or "",
        metadata={
            "document_id": document.pk,
            "title": document.title,
            "filename": document.original_filename,
            "owner_id": document.owner_id,
        },
        candidates={
            field: [
                {"id": candidate.pk, "name": candidate.name}
                for candidate in candidates
                if candidate.matching_algorithm == MatchingModel.MATCH_AUTO
            ],
        },
    )
    # A separate copy prevents an in-process provider from widening its allowlist.
    result = provider.classify(json.loads(json.dumps(request)))
    validate_result(request, result, identity)
    assert_document_unchanged(document, revision)
    current = candidates_for(document, field, user)
    if (
        configuration != provider_configuration()
        or [
            (o.pk, o.name, o.matching_algorithm, o.match, o.is_insensitive)
            for o in current
        ]
        != [
            (o.pk, o.name, o.matching_algorithm, o.match, o.is_insensitive)
            for o in candidates
        ]
        or provider.identity != identity
    ):
        raise InvalidClassificationResult(
            "Classification candidates or provider changed"
        )
    current_identity = (
        provider.current_identity
        if hasattr(provider, "current_identity")
        else active_provider_identity()
    )
    if current_identity != identity:
        raise InvalidClassificationResult("Classification provider was replaced")
    return candidates, result["assignments"][field]


def classification_assignment(function):
    """Lock the assignment target and roll back failed replacements."""

    @wraps(function)
    def apply(sender, document: Document, *args, **kwargs):
        """Check the supplied source before entering the existing assignment policy."""
        with transaction.atomic():
            if document.pk:
                current = Document.objects.select_for_update().get(pk=document.pk)
                if snapshot_document(current) != snapshot_document(document):
                    raise InvalidClassificationResult(
                        "Document changed before classification"
                    )
            result = function(sender, document, *args, **kwargs)
            if document.pk and not kwargs.get("dry_run", False):
                # File reconciliation updates filenames on its own ORM instance.
                document.refresh_from_db()
            return result

    return apply


def suggestion_scope(document: Document, user) -> str:
    """Bind cached results to source, provider, rules and current visibility."""
    return fingerprint(
        {
            "revision": snapshot_document(document),
            "provider": active_provider_identity(),
            "configuration": provider_configuration(),
            "dates": [
                settings.NUMBER_OF_SUGGESTED_DATES,
                settings.DATE_ORDER,
                settings.FILENAME_DATE_ORDER,
                settings.DATE_PARSER_LANGUAGES,
                sorted(settings.IGNORE_DATES),
            ],
            "user": user.pk if user else None,
            "candidates": {
                field: [
                    (o.pk, o.name, o.matching_algorithm, o.match, o.is_insensitive)
                    for o in candidates_for(document, field, user)
                ]
                for field in MODELS
            },
        }
    )
