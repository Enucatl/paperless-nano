"""Built-in search provider; all native-engine and filesystem policy lives here."""

from __future__ import annotations

import logging
import random
import time
from contextlib import AbstractContextManager
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime
from typing import TYPE_CHECKING
from typing import Final

from django.conf import settings
from django.core.exceptions import FieldError
from django.utils.timezone import make_aware

from documents.exceptions import SearchIndexLockError
from documents.search._engine import TantivyEngine
from documents.search._errors import SearchQueryError
from documents.search._schema import needs_rebuild
from documents.search._schema import wipe_index
from documents.search.contract import SearchHit
from documents.search.contract import SearchPage
from documents.search.contract import SearchRequest

if TYPE_CHECKING:
    from collections.abc import Iterable
    from collections.abc import Iterator
    from collections.abc import Sequence
    from pathlib import Path

    from django.db.models import QuerySet

    from documents.models import Document
    from documents.search.contract import DocumentFacts
    from documents.search.contract import SearchWriter

logger = logging.getLogger("paperless.search")

_LOCK_TIMEOUT_SECONDS: Final[float] = 10.0
_LOCK_RETRY_ATTEMPTS: Final[int] = 4
_LOCK_BACKOFF_BASE: Final[float] = 1.0
_LOCK_BACKOFF_CAP: Final[float] = 10.0


def enter_batch_with_retry(batch: AbstractContextManager) -> None:
    """Acquire the engine batch with the existing bounded, jittered backoff."""
    for attempt in range(_LOCK_RETRY_ATTEMPTS):
        try:
            batch.__enter__()
            return
        except SearchIndexLockError:
            if attempt == _LOCK_RETRY_ATTEMPTS - 1:
                raise
            sleep_s = random.uniform(
                0,
                min(_LOCK_BACKOFF_CAP, _LOCK_BACKOFF_BASE * (2**attempt)),
            )
            logger.debug(
                "Index lock contention; retrying in %.2fs (attempt %d/%d)",
                sleep_s,
                attempt + 1,
                _LOCK_RETRY_ATTEMPTS,
            )
            time.sleep(sleep_s)


class TantivyProvider:
    """Preserve existing Tantivy syntax, ranking, sorting and storage behavior."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = settings.INDEX_DIR if path is None else path
        self.engine = TantivyEngine(self.path, language=settings.SEARCH_LANGUAGE)

    def open(self) -> None:
        """Open the selected index."""
        self.engine.open()

    def close(self) -> None:
        """Release the index reader."""
        self.engine.close()

    @contextmanager
    def batch_update(
        self, lock_timeout: float = _LOCK_TIMEOUT_SECONDS
    ) -> Iterator[SearchWriter]:
        """Apply bounded lock retry only for this filesystem implementation."""
        batch = self.engine.batch_update(lock_timeout=lock_timeout)
        enter_batch_with_retry(batch)
        try:
            yield batch
        except BaseException as error:
            batch.__exit__(type(error), error, error.__traceback__)
            raise
        else:
            batch.__exit__(None, None, None)

    def matching_ids(self, request: SearchRequest) -> list[int]:
        """Intersect native matches with current permissions before pagination."""
        engine_order = (
            request.ordering in self.engine.SORTABLE_FIELDS
            or request.ordering in {None, "score"}
            or request.similar_to is not None
        )
        if request.similar_to is not None:
            all_ids = self.engine.more_like_this_ids(request.similar_to, None)
        else:
            all_ids = self.engine.search_ids(
                request.query,
                None,
                config=request.configuration,
                sort_field=request.ordering
                if engine_order and request.ordering != "score"
                else None,
                sort_reverse=request.descending,
                search_mode=request.mode,
                filter_query=request.filter_query,
            )
        if engine_order:
            scope = (
                request.scope.filter(pk__in=all_ids)
                if len(all_ids) <= 5000
                else request.scope
            )
            visible = set(scope.values_list("pk", flat=True))
            ids = [pk for pk in all_ids if pk in visible]
        else:
            scope = request.scope
            if not scope.query.order_by:
                try:
                    scope = scope.order_by(
                        ("-" if request.descending else "") + request.ordering
                    )
                except FieldError as error:
                    raise SearchQueryError("Unsupported search ordering") from error
            ids = list(scope.filter(pk__in=all_ids).values_list("pk", flat=True))
        if request.ordering == "score" and not request.descending:
            ids.reverse()
        stop = request.offset + request.limit if request.limit is not None else None
        return ids[request.offset : stop]

    def search(self, request: SearchRequest) -> SearchPage:
        """Keep native ID intersection here, outside the application contract."""
        ids = self.matching_ids(replace(request, offset=0, limit=None))
        stop = request.offset + request.limit if request.limit is not None else None
        page_ids = ids[request.offset : stop]
        if request.similar_to is None:
            hits = self.highlight(request, page_ids, request.offset + 1)
        else:
            hits = [
                SearchHit(id=pk, score=0.0, rank=rank, highlights={})
                for rank, pk in enumerate(page_ids, request.offset + 1)
            ]
        return SearchPage(len(ids), hits, request.offset, lambda: ids)

    def highlight(
        self, request: SearchRequest, ids: Sequence[int], rank_start: int
    ) -> list[SearchHit]:
        """Highlight a page already checked against current authorized scope."""
        allowed = set(request.scope.filter(pk__in=ids).values_list("pk", flat=True))
        return self.engine.highlight_hits(
            request.query,
            [pk for pk in ids if pk in allowed],
            config=request.configuration,
            search_mode=request.mode,
            rank_start=rank_start,
        )

    def autocomplete(
        self, term: str, limit: int, scope: QuerySet[Document]
    ) -> list[str]:
        """Apply live authorization even when indexed permission grants are stale."""
        return self.engine.autocomplete(
            term, limit, allowed_ids=list(scope.values_list("pk", flat=True))
        )

    def rebuild(
        self, records: Iterable[DocumentFacts], *, writer_heap_bytes: int = 512_000_000
    ) -> None:
        """Rebuild native derived data from the core fact stream."""
        self.engine.rebuild(records, writer_heap_bytes=writer_heap_bytes)

    def needs_rebuild(self) -> bool:
        """Inspect native schema and language sentinels before opening."""
        return needs_rebuild(self.path)

    def wipe(self) -> None:
        """Remove only the selected derived index."""
        self.close()
        wipe_index(self.path)

    def optimize(self) -> None:
        """Tantivy merges segments automatically."""

    def last_modified(self) -> datetime | None:
        """Check the index and report its latest file modification time."""
        self.open()
        mtimes = [
            path.stat().st_mtime for path in self.path.iterdir() if path.is_file()
        ]
        return make_aware(datetime.fromtimestamp(max(mtimes))) if mtimes else None
