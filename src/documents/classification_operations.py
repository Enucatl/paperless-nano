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
from documents.classification_providers import SklearnProvider
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


def provider_configuration() -> tuple:
    """Identify selection and effective inference settings outside the contract."""
    return (
        settings.CLASSIFICATION_PROVIDER,
        settings.CLASSIFIER_ENABLED,
        settings.CLASSIFIER_LANGUAGE,
        settings.CLASSIFIER_MATCH_THRESHOLD,
    )


def active_provider_identity() -> tuple[str, str] | None:
    """Read an activation token without loading the built-in model into memory."""
    if not settings.CLASSIFIER_ENABLED:
        return None
    if settings.CLASSIFICATION_PROVIDER:
        provider = import_string(settings.CLASSIFICATION_PROVIDER)()
        return provider.identity
    from documents.classifier import active_model_identity

    model = active_model_identity()
    return (
        ("sklearn:1", fingerprint([model, provider_configuration()])) if model else None
    )


def load_classification_provider():
    """Select inference independently from training and synchronous execution."""
    if not settings.CLASSIFIER_ENABLED:
        return None
    if settings.CLASSIFICATION_PROVIDER:
        return import_string(settings.CLASSIFICATION_PROVIDER)()
    from documents.classifier import load_classifier

    return load_classifier()


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
    identity = getattr(classifier, "identity", None)
    if not isinstance(identity, tuple):
        model = getattr(classifier, "model_identity", None)
        model = model if isinstance(model, str) else None
        identity = ("sklearn:1", fingerprint([model, configuration]))
        provider = SklearnProvider(classifier, identity)
    else:
        provider = classifier
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
    # A loaded model stays immutable, but an activation invalidates its proposals.
    if isinstance(provider, SklearnProvider):
        if (
            isinstance(classifier.model_identity, str)
            and active_provider_identity() != identity
        ):
            raise InvalidClassificationResult("Classification model was replaced")
    elif active_provider_identity() != identity:
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
