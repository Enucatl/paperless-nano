"""Stable installed-provider contract; no search engine imports."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING
from typing import Protocol
from typing import TypeAlias
from typing import TypedDict

if TYPE_CHECKING:
    from collections.abc import Callable
    from collections.abc import Iterable
    from collections.abc import Sequence
    from contextlib import AbstractContextManager

    from django.db.models import QuerySet

    from documents.models import Document
    from documents.search._types import QueryConfiguration

FactValue: TypeAlias = str | int | datetime | dict[str, str | None]
DocumentFacts: TypeAlias = dict[str, list[FactValue]]


class SearchMode(StrEnum):
    QUERY = "query"
    TEXT = "text"
    TITLE = "title"


class SearchHit(TypedDict):
    """Type definition for search result hits."""

    id: int
    score: float
    rank: int
    highlights: dict[str, str]


@dataclass(frozen=True)
class SearchRequest:
    """A query against a current authorized and caller-filtered root queryset.

    Providers apply scope before ranking/count/pagination, never discard filters,
    and raise SearchQueryError for unsupported expressions. Ordering is a public
    Document field name or score; None means best relevance first.
    """

    scope: QuerySet[Document]
    configuration: QueryConfiguration
    query: str = ""
    mode: SearchMode = SearchMode.QUERY
    filter_query: str | None = None
    ordering: str | None = None
    descending: bool = False
    offset: int = 0
    limit: int | None = None
    similar_to: int | None = None


@dataclass(frozen=True)
class SearchPage:
    """Exact authorized count and page hits, plus lazy IDs for explicit selections.

    matching_ids may return a QuerySet subquery; normal pagination does not
    materialize it. Hit ranks are one-based positions in the whole result.
    """

    count: int
    hits: list[SearchHit]
    offset: int
    matching_ids: Callable[[], Iterable[int]]


class SearchResults:
    """Adapt an already paged result to DRF without loading every matching ID."""

    def __init__(self, page: SearchPage) -> None:
        self.page = page

    def __len__(self) -> int:
        return self.page.count

    def __getitem__(self, key: slice) -> list[SearchHit]:
        start = key.start or 0
        if start != self.page.offset:
            raise IndexError("Search result page does not match requested offset")
        return self.page.hits[: (key.stop or self.page.count) - start]

    def get_all_ids(self) -> list[int]:
        """Materialize IDs only for the legacy HTTP all-results field."""
        return list(self.page.matching_ids())


class SearchWriter(Protocol):
    """A provider-owned atomic batch; an exception must roll back the batch."""

    def add_or_update(self, record: DocumentFacts) -> None:
        """Replace the searchable representation of these current document facts."""
        ...

    def remove(self, document_id: int) -> None:
        """Remove an explicit ID, including a row already deleted from core."""
        ...


class SearchProvider(Protocol):
    """No-argument installed implementation; settings/storage belong to it.

    Core supplies current authorization and facts. Providers must honor scope
    for every read, including autocomplete and similarity. Resources are local
    to a process and released by close(). No lock or external-index requirement.
    """

    def open(self) -> None:
        """Initialize provider resources or fail visibly."""
        ...

    def close(self) -> None:
        """Release provider resources; safe after initialization failure."""
        ...

    def search(self, request: SearchRequest) -> SearchPage:
        """Return an authorized, ordered page with exact count."""
        ...

    def matching_ids(self, request: SearchRequest) -> Iterable[int]:
        """Return authorized ordered IDs, optionally as a database query."""
        ...

    def highlight(
        self, request: SearchRequest, ids: Sequence[int], rank_start: int
    ) -> list[SearchHit]:
        """Return highlights only for IDs inside request.scope."""
        ...

    def autocomplete(
        self, term: str, limit: int, scope: QuerySet[Document]
    ) -> list[str]:
        """Return completions derived exclusively from scope."""
        ...

    def batch_update(self) -> AbstractContextManager[SearchWriter]:
        """Open an atomic write batch; errors propagate without implicit replay."""
        ...

    def rebuild(
        self, records: Iterable[DocumentFacts], *, writer_heap_bytes: int
    ) -> None:
        """Replace derived search data from core facts; memory budget is advisory."""
        ...

    def needs_rebuild(self) -> bool:
        """Report whether provider-specific derived storage requires rebuilding."""
        ...

    def wipe(self) -> None:
        """Remove only this provider's derived data, never authoritative documents."""
        ...

    def optimize(self) -> None:
        """Perform optional provider maintenance."""
        ...

    def last_modified(self) -> datetime | None:
        """Check health and return the last derived write time if available."""
        ...
