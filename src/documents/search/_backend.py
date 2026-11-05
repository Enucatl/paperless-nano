"""Application search service: current facts, authorization and write scheduling."""

from __future__ import annotations

import threading
from dataclasses import replace
from typing import TYPE_CHECKING
from typing import Any
from typing import Self

from django.conf import settings
from django.core.exceptions import PermissionDenied
from django.utils.module_loading import import_string
from django.utils.timezone import get_current_timezone

from documents.exceptions import SearchWriteBusy
from documents.search._persistence import _DocumentViewerStream
from documents.search._persistence import documents_for_ids
from documents.search._persistence import index_records
from documents.search._records import document_to_record
from documents.search._types import QueryConfiguration
from documents.search.contract import SearchHit
from documents.search.contract import SearchMode
from documents.search.contract import SearchPage
from documents.search.contract import SearchProvider
from documents.search.contract import SearchRequest
from documents.search_jobs import defer_index_write
from documents.utils import identity

if TYPE_CHECKING:
    from collections.abc import Sequence
    from contextlib import AbstractContextManager
    from datetime import datetime

    from django.contrib.auth.models import AbstractUser
    from django.db.models import QuerySet

    from documents.models import Document
    from documents.search.contract import SearchWriter


def query_configuration() -> QueryConfiguration:
    """Resolve current request settings without retaining timezone or permissions."""
    return QueryConfiguration(
        settings.SEARCH_LANGUAGE,
        settings.ADVANCED_FUZZY_SEARCH_THRESHOLD,
        get_current_timezone(),
    )


def authorized_scope(
    user: AbstractUser | None = None, scope: QuerySet[Document] | None = None
) -> QuerySet[Document]:
    """Keep live root visibility as a database query providers can compose."""
    from documents.models import Document
    from documents.permissions import restrict_queryset_to_visible

    scope = Document.objects.all() if scope is None else scope
    return (
        restrict_queryset_to_visible(
            scope.filter(root_document__isnull=True), user, "view_document"
        )
        if user is not None
        else scope.filter(root_document__isnull=True)
    )


class WriteBatch:
    """Read current core facts after the provider has entered its atomic batch."""

    def __init__(self, context: AbstractContextManager[SearchWriter]) -> None:
        self._context = context

    def __enter__(self) -> Self:
        self._batch = self._context.__enter__()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self._context.__exit__(exc_type, exc_val, exc_tb)

    def add_or_update(self, document: Document) -> None:
        """Read the root's current facts instead of indexing a stale caller instance."""
        records = index_records(documents_for_ids([document.pk]))
        for record in records:
            self._batch.add_or_update(record)

    def add_or_update_ids(self, ids: Sequence[int]) -> None:
        """Stream current roots and grants within the provider's batch."""
        for record in index_records(documents_for_ids(ids)):
            self._batch.add_or_update(record)

    def remove(self, doc_id: int) -> None:
        """Delete an explicit ID even after its authoritative row has disappeared."""
        self._batch.remove(doc_id)


class SearchService:
    """Stable application operations independent of provider storage and query engine."""

    def __init__(self, provider: SearchProvider) -> None:
        self.provider = provider

    def open(self) -> None:
        """Open selected provider resources."""
        self.provider.open()

    def close(self) -> None:
        """Release selected provider resources."""
        self.provider.close()

    def batch_update(self) -> WriteBatch:
        """Apply a batch atomically; never hide partial or failed batch writes."""
        from documents.search_operations import DeferredSearchBatch

        return WriteBatch(DeferredSearchBatch(self.provider))

    def add_or_update(self, document: Document) -> None:
        """Defer only failures the provider explicitly declares safe to retry."""
        try:
            with self.batch_update() as batch:
                batch.add_or_update(document)
        except SearchWriteBusy:
            defer_index_write(document.pk)

    def remove(self, doc_id: int) -> None:
        """Schedule explicit removal only on a declared retryable write failure."""
        try:
            with self.batch_update() as batch:
                batch.remove(doc_id)
        except SearchWriteBusy:
            defer_index_write(doc_id, remove=True)

    def request(
        self,
        query: str = "",
        user: AbstractUser | None = None,
        *,
        scope: QuerySet[Document] | None = None,
        **kwargs: Any,
    ) -> SearchRequest:
        """Build a request whose authorized scope remains composable SQL."""
        return SearchRequest(
            authorized_scope(user, scope),
            query_configuration(),
            query=query,
            similarity_scope=authorized_scope(user)
            if kwargs.get("similar_to")
            else None,
            **kwargs,
        )

    def search(self, request: SearchRequest) -> SearchPage:
        """Validate provider page visibility against the supplied core scope."""
        if request.similar_to is not None:
            seed_scope = (
                request.similarity_scope
                if request.similarity_scope is not None
                else request.scope
            )
            if not seed_scope.filter(pk=request.similar_to).exists():
                raise PermissionDenied("Insufficient permissions for similarity seed")
        result = self.provider.search(request)
        ids = [hit["id"] for hit in result.hits]
        allowed = set(request.scope.filter(pk__in=ids).values_list("pk", flat=True))
        if any(pk not in allowed for pk in ids):
            raise ValueError(
                "Search provider returned a document outside authorized scope"
            )

        def matching_ids():
            """Keep lazy SQL selections scoped and check concrete provider ID lists."""
            from django.db.models import QuerySet

            matches = result.matching_ids()
            if isinstance(matches, QuerySet):
                return matches.filter(pk__in=request.scope.values("pk"))
            matches = list(matches)
            allowed = set(
                request.scope.filter(pk__in=matches).values_list("pk", flat=True)
            )
            if any(pk not in allowed for pk in matches):
                raise ValueError(
                    "Search provider returned selection IDs outside authorized scope"
                )
            return matches

        return replace(result, matching_ids=matching_ids)

    def search_ids(
        self,
        query: str,
        user: AbstractUser | None = None,
        *,
        sort_field: str | None = None,
        sort_reverse: bool = False,
        search_mode: SearchMode = SearchMode.QUERY,
        filter_query: str | None = None,
        limit: int | None = None,
    ) -> list[int]:
        """Expose authorized IDs for existing selection and workflow callers."""
        request = self.request(
            query,
            user,
            ordering=sort_field,
            descending=sort_reverse,
            mode=search_mode,
            filter_query=filter_query,
            limit=limit,
        )
        ids = list(self.provider.matching_ids(request))
        allowed = set(request.scope.filter(pk__in=ids).values_list("pk", flat=True))
        if any(pk not in allowed for pk in ids):
            raise ValueError(
                "Search provider returned a document outside authorized scope"
            )
        return ids

    def highlight_hits(
        self,
        query: str,
        doc_ids: list[int],
        *,
        user: AbstractUser | None = None,
        search_mode: SearchMode = SearchMode.QUERY,
        rank_start: int = 1,
    ) -> list[SearchHit]:
        """Authorize requested IDs before asking the provider for highlight text."""
        request = self.request(query, user, mode=search_mode)
        allowed = set(request.scope.filter(pk__in=doc_ids).values_list("pk", flat=True))
        ids = [pk for pk in doc_ids if pk in allowed]
        hits = self.provider.highlight(request, ids, rank_start)
        if any(hit["id"] not in allowed for hit in hits):
            raise ValueError(
                "Search provider returned a highlight outside authorized scope"
            )
        return hits

    def autocomplete(
        self, term: str, limit: int, user: AbstractUser | None = None
    ) -> list[str]:
        """Supply current visibility for completion terms as well as documents."""
        return self.provider.autocomplete(term, limit, authorized_scope(user))

    def more_like_this_ids(
        self, doc_id: int, user: AbstractUser | None = None, *, limit: int | None = None
    ) -> list[int]:
        """Authorize the root seed before asking for similar authorized documents."""
        from documents.versioning import root_document_ids

        root = next(iter(root_document_ids([doc_id])), doc_id)
        request = self.request(user=user, similar_to=root, limit=limit)
        if not request.scope.filter(pk=root).exists():
            return []
        ids = list(self.provider.matching_ids(request))
        allowed = set(request.scope.filter(pk__in=ids).values_list("pk", flat=True))
        if any(pk not in allowed for pk in ids):
            raise ValueError(
                "Search provider returned a document outside authorized scope"
            )
        return ids

    def rebuild(
        self,
        documents: QuerySet[Document],
        iter_wrapper=identity,
        writer_heap_bytes: int = 512_000_000,
    ) -> None:
        """Stream core facts with progress while storage remains provider-owned."""
        records = (
            document_to_record(
                document,
                viewer_ids=grant.viewer_ids,
                viewer_group_ids=grant.viewer_group_ids,
            )
            for document, grant in iter_wrapper(
                _DocumentViewerStream(documents, chunk_size=1000)
            )
        )
        from documents.search_operations import submit_search

        # shortcut: rebuild snapshots use memory; stage JSONL when indexes exceed worker RAM.
        submit_search(
            self.provider,
            "rebuild",
            {
                "records": list(records),
                "writer_heap_bytes": writer_heap_bytes,
            },
        )

    def last_modified(self) -> datetime | None:
        """Return provider health/write time without assuming an index directory."""
        return self.provider.last_modified()

    def optimize(self) -> None:
        """Invoke the selected provider's maintenance operation."""
        from documents.search_operations import submit_search

        submit_search(self.provider, "optimize", {})


class TantivyBackend(SearchService):
    """Internal convenience for native engine regression tests, not a plugin API."""

    def __init__(self, path):
        from documents.search.tantivy import TantivyProvider

        super().__init__(TantivyProvider(path))
        self.engine = self.provider.engine

    def batch_update(self, lock_timeout: float = 10.0) -> WriteBatch:
        """Allow native tests to exercise bounded lock acquisition explicitly."""
        return WriteBatch(self.provider.batch_update(lock_timeout=lock_timeout))


_backend: SearchService | None = None
_backend_key = None
_backend_lock = threading.RLock()


def get_backend() -> SearchService:
    """Cache one selected service per process; never import an unselected provider."""
    global _backend, _backend_key
    from documents.search_operations import provider_snapshot

    with _backend_lock:
        if _backend is not None:
            if _backend_key == provider_snapshot(_backend.provider):
                return _backend
            reset_backend()
        provider = import_string(settings.SEARCH_PROVIDER)()
        key = provider_snapshot(provider)
        service = SearchService(provider)
        try:
            service.open()
        except BaseException:
            service.close()
            raise
        _backend, _backend_key = service, key
        return service


def reset_backend() -> None:
    """Release cached resources, including during provider configuration changes."""
    global _backend, _backend_key
    with _backend_lock:
        if _backend is not None:
            _backend.close()
        _backend = None
        _backend_key = None


def needs_rebuild() -> bool:
    """Inspect selected storage without opening and changing rebuild sentinels."""
    provider = import_string(settings.SEARCH_PROVIDER)()
    try:
        return provider.needs_rebuild()
    finally:
        provider.close()


def wipe_index() -> None:
    """Remove selected derived storage without touching authoritative documents."""
    reset_backend()
    provider = import_string(settings.SEARCH_PROVIDER)()
    try:
        from documents.search_operations import submit_search

        submit_search(provider, "wipe", {})
    finally:
        provider.close()
