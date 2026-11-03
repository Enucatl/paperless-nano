from datetime import UTC
from datetime import datetime

from django.conf import settings
from django.core.cache import cache
from rest_framework.request import Request

from documents.caching import CACHE_5_MINUTES
from documents.caching import CACHE_50_MINUTES
from documents.caching import CLASSIFIER_MODIFIED_KEY
from documents.caching import CLASSIFIER_VERSION_KEY
from documents.caching import get_thumbnail_modified_key
from documents.classification import ClassificationProviderError
from documents.classification import InvalidClassificationResult
from documents.classifier import DocumentClassifier
from documents.models import Document
from documents.versioning import resolve_effective_document_by_pk


def suggestions_etag(request, pk: int) -> str | None:
    """
    Returns an optional string for the ETag, allowing browser caching of
    suggestions if the classifier has not been changed and the suggested dates
    setting is also unchanged

    """
    from documents.classification_operations import suggestion_scope
    from documents.permissions import has_perms_owner_aware

    document = Document.objects.filter(pk=pk).first()
    if document is None or not has_perms_owner_aware(
        request.user, "change_document", document
    ):
        return None
    try:
        return suggestion_scope(document, request.user)
    except (TimeoutError, ClassificationProviderError, InvalidClassificationResult):
        # The endpoint maps provider failures; a conditional cannot answer them.
        return None


def suggestions_last_modified(request, pk: int) -> datetime | None:
    """
    Returns the datetime of classifier last modification.  This is slightly off,
    as there is not way to track the suggested date setting modification, but it seems
    unlikely that changes too often
    """
    # A date cannot represent permission/configuration changes; only ETags can
    # safely answer conditional requests. Keep the informational header otherwise.
    if request.META.get("HTTP_IF_MODIFIED_SINCE"):
        return None
    # No file, no last modified
    if not settings.MODEL_FILE.exists():
        return None
    cache_hits = cache.get_many(
        [CLASSIFIER_VERSION_KEY, CLASSIFIER_MODIFIED_KEY],
    )
    # If the version differs somehow, no last modified
    if (
        CLASSIFIER_VERSION_KEY in cache_hits
        and cache_hits[CLASSIFIER_VERSION_KEY] != DocumentClassifier.FORMAT_VERSION
    ):
        return None
    elif CLASSIFIER_MODIFIED_KEY in cache_hits:
        # Refresh the cache and return the last modified
        cache.touch(CLASSIFIER_MODIFIED_KEY, CACHE_5_MINUTES)
        return cache_hits[CLASSIFIER_MODIFIED_KEY]
    return None


def metadata_etag(request, pk: int) -> str | None:
    """
    Metadata responses include metadata as well as document fields, so include
    the modification time with the checksum so metadata-only changes invalidate cache.
    """
    doc = resolve_effective_document_by_pk(pk, request).document
    if doc is None:
        return None
    return f"{doc.checksum}:{doc.modified.isoformat()}"


def metadata_last_modified(request, pk: int) -> datetime | None:
    """
    Metadata is extracted from the original file, so use its modified.  Strictly speaking, this is
    not the modification of the original file, but of the database object, but might as well
    error on the side of more cautious
    """
    doc = resolve_effective_document_by_pk(pk, request).document
    if doc is None:
        return None
    return doc.modified


def preview_etag(request, pk: int) -> str | None:
    """
    ETag for the document preview, using the original or archive checksum, depending on the request
    """
    doc = resolve_effective_document_by_pk(pk, request).document
    if doc is None:
        return None
    use_original = (
        hasattr(request, "query_params")
        and "original" in request.query_params
        and request.query_params["original"] == "true"
    )
    return doc.checksum if use_original else doc.archive_checksum


def preview_last_modified(request, pk: int) -> datetime | None:
    """
    Uses the documents modified time to set the Last-Modified header.  Not strictly
    speaking correct, but close enough and quick
    """
    doc = resolve_effective_document_by_pk(pk, request).document
    if doc is None:
        return None
    return doc.modified


def thumbnail_etag(request: Request, pk: int) -> str | None:
    """
    Thumbnails are version-dependent, so use the effective document checksum as
    the ETag to invalidate cache when the latest version changes.
    """
    doc = resolve_effective_document_by_pk(pk, request).document
    if doc is None:
        return None
    return doc.checksum


def thumbnail_last_modified(request: Request, pk: int) -> datetime | None:
    """
    Returns the filesystem last modified either from cache or from filesystem.
    Cache should be (slightly?) faster than filesystem
    """
    try:
        doc = resolve_effective_document_by_pk(pk, request).document
        if doc is None:
            return None
        if not doc.thumbnail_path.exists():
            return None
        # Use the effective document id for cache key
        doc_key = get_thumbnail_modified_key(doc.id)

        cache_hit = cache.get(doc_key)
        if cache_hit is not None:
            cache.touch(doc_key, CACHE_50_MINUTES)
            return cache_hit

        # No cache, get the timestamp and cache the datetime
        last_modified = datetime.fromtimestamp(
            doc.thumbnail_path.stat().st_mtime,
            tz=UTC,
        )
        cache.set(doc_key, last_modified, CACHE_50_MINUTES)
        return last_modified
    except (Document.DoesNotExist, OSError):  # pragma: no cover
        return None
