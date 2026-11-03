"""Django-facing search operations; the engine receives only plain values."""

from __future__ import annotations

import threading
from typing import TYPE_CHECKING

from django.conf import settings
from django.utils.timezone import get_current_timezone

from documents.exceptions import SearchIndexLockError
from documents.search._engine import SearchHit
from documents.search._engine import SearchMode
from documents.search._engine import TantivyEngine
from documents.search._persistence import _DocumentViewerStream
from documents.search._persistence import documents_for_ids
from documents.search._persistence import index_records
from documents.search._persistence import record_for_document
from documents.search._records import document_to_record
from documents.search._types import QueryConfiguration
from documents.search._types import SearchVisibility
from documents.search_jobs import _LOCK_TIMEOUT_SECONDS
from documents.search_jobs import defer_index_write
from documents.search_jobs import enter_batch_with_retry
from documents.utils import identity

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path
    from typing import Self

    from django.contrib.auth.models import AbstractUser
    from django.db.models import QuerySet

    from documents.models import Document
    from documents.search._persistence import ViewerGrant
    from documents.utils import IterWrapper


def query_configuration() -> QueryConfiguration:
    """Resolve effective settings and the current request's timezone."""
    return QueryConfiguration(
        language=settings.SEARCH_LANGUAGE,
        fuzzy_threshold=settings.ADVANCED_FUZZY_SEARCH_THRESHOLD,
        timezone=get_current_timezone(),
    )


def visibility(user: AbstractUser | None) -> SearchVisibility | None:
    """Fetch current group memberships rather than trusting indexed membership."""
    if user is None:
        return None
    return SearchVisibility(user.pk, tuple(user.groups.values_list("pk", flat=True)))


class WriteBatch:
    """Load records only after the engine's writer lock has been acquired."""

    def __init__(self, backend: TantivyBackend, lock_timeout: float) -> None:
        self._batch = backend.engine.batch_update(lock_timeout=lock_timeout)

    def __enter__(self) -> Self:
        enter_batch_with_retry(self._batch)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        return self._batch.__exit__(exc_type, exc_val, exc_tb)

    def add_or_update(self, document: Document) -> None:
        """Map a document and its grants to a plain engine record."""
        self._batch.add_or_update(record_for_document(document))

    def add_or_update_ids(self, ids: Sequence[int]) -> None:
        """Index existing roots with batched grants and effective version content."""
        if not ids:
            return
        for record in index_records(documents_for_ids(ids)):
            self._batch.add_or_update(record)

    def remove(self, doc_id: int) -> None:
        """Remove the requested ID, including IDs whose database row is gone."""
        self._batch.remove(doc_id)


class TantivyBackend:
    """Preserve the application search API around the record-based Tantivy engine."""

    SORTABLE_FIELDS = TantivyEngine.SORTABLE_FIELDS
    SORT_FIELD_MAP = TantivyEngine.SORT_FIELD_MAP

    def __init__(self, path: Path) -> None:
        self.engine = TantivyEngine(path, language=settings.SEARCH_LANGUAGE)

    def open(self) -> None:
        """Open the configured search index."""
        self.engine.open()

    def close(self) -> None:
        """Release this adapter's engine resources."""
        self.engine.close()

    def batch_update(self, lock_timeout: float = 30.0) -> WriteBatch:
        """Acquire a writer with the job adapter's bounded lock retry policy."""
        return WriteBatch(self, lock_timeout)

    def add_or_update(self, document: Document) -> None:
        """Index a document, deferring through the job adapter on lock exhaustion."""
        try:
            with self.batch_update(lock_timeout=_LOCK_TIMEOUT_SECONDS) as batch:
                batch.add_or_update(document)
        except SearchIndexLockError:
            defer_index_write(document.pk)

    def remove(self, doc_id: int) -> None:
        """Remove an ID, deferring through the job adapter on lock exhaustion."""
        try:
            with self.batch_update(lock_timeout=_LOCK_TIMEOUT_SECONDS) as batch:
                batch.remove(doc_id)
        except SearchIndexLockError:
            defer_index_write(doc_id, remove=True)

    def search_ids(
        self,
        query: str,
        user: AbstractUser | None,
        *,
        sort_field: str | None = None,
        sort_reverse: bool = False,
        search_mode: SearchMode = SearchMode.QUERY,
        filter_query: str | None = None,
        limit: int | None = None,
    ) -> list[int]:
        """Search with explicit request configuration and current visibility."""
        return self.engine.search_ids(
            query,
            visibility(user),
            config=query_configuration(),
            sort_field=sort_field,
            sort_reverse=sort_reverse,
            search_mode=search_mode,
            filter_query=filter_query,
            limit=limit,
        )

    def highlight_hits(
        self,
        query: str,
        doc_ids: list[int],
        *,
        search_mode: SearchMode = SearchMode.QUERY,
        rank_start: int = 1,
    ) -> list[SearchHit]:
        """Build highlights using the same effective configuration as search."""
        return self.engine.highlight_hits(
            query,
            doc_ids,
            config=query_configuration(),
            search_mode=search_mode,
            rank_start=rank_start,
        )

    def autocomplete(
        self,
        term: str,
        limit: int,
        user: AbstractUser | None = None,
    ) -> list[str]:
        """Restrict completions to current visibility, preserving superuser access."""
        return self.engine.autocomplete(
            term,
            limit,
            visibility(user) if user and not user.is_superuser else None,
        )

    def more_like_this_ids(
        self,
        doc_id: int,
        user: AbstractUser | None,
        *,
        limit: int | None = None,
    ) -> list[int]:
        """Resolve a version to its root before searching for similar documents."""
        from documents.versioning import root_document_ids

        root_id = next(iter(root_document_ids([doc_id])), doc_id)
        return self.engine.more_like_this_ids(root_id, visibility(user), limit=limit)

    def rebuild(
        self,
        documents: QuerySet[Document],
        iter_wrapper: IterWrapper[tuple[Document, ViewerGrant]] = identity,
        writer_heap_bytes: int = 512_000_000,
    ) -> None:
        """Stream mapped records while retaining per-document progress callbacks."""
        records = (
            document_to_record(
                document,
                viewer_ids=grant.viewer_ids,
                viewer_group_ids=grant.viewer_group_ids,
            )
            for document, grant in iter_wrapper(
                _DocumentViewerStream(documents, chunk_size=1000),
            )
        )
        self.engine.rebuild(records, writer_heap_bytes=writer_heap_bytes)


# Module-level singleton with proper thread safety
_backend: TantivyBackend | None = None
_backend_path: Path | None = None
_backend_lock = threading.RLock()


def get_backend() -> TantivyBackend:
    """Get the shared adapter, replacing it when INDEX_DIR changes."""
    global _backend, _backend_path

    current_path: Path = settings.INDEX_DIR
    if _backend is not None and _backend_path == current_path:
        return _backend
    with _backend_lock:
        if _backend is not None and _backend_path == current_path:
            return _backend
        if _backend is not None:
            _backend.close()
        _backend = TantivyBackend(path=current_path)
        _backend.open()
        _backend_path = current_path
        return _backend


def reset_backend() -> None:
    """Release the shared adapter, including when tests change index directories."""
    global _backend, _backend_path

    with _backend_lock:
        if _backend is not None:
            _backend.close()
        _backend = None
        _backend_path = None
