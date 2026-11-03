from __future__ import annotations

import logging
import re
from enum import StrEnum
from typing import TYPE_CHECKING
from typing import Self
from typing import TypedDict
from typing import cast

import filelock
import tantivy

from documents.exceptions import SearchIndexLockError
from documents.search._query import normalize_search_text
from documents.search._query import parse_simple_text_highlight_query
from documents.search._query import parse_simple_text_query
from documents.search._query import parse_simple_title_query
from documents.search._query import parse_user_query
from documents.search._schema import _write_sentinels
from documents.search._schema import build_schema
from documents.search._schema import open_or_rebuild_index
from documents.search._schema import rebuild_in_progress
from documents.search._schema import wipe_index
from documents.search._tokenizer import ascii_fold
from documents.search._tokenizer import register_tokenizers

if TYPE_CHECKING:
    from collections.abc import Iterable
    from pathlib import Path

    from documents.search._records import IndexRecord
    from documents.search._types import QueryConfiguration
    from documents.search._types import SearchVisibility

logger = logging.getLogger("paperless.search")


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


class TantivyRelevanceList:
    """
    DRF-compatible list wrapper for Tantivy search results.

    Holds a lightweight ordered list of IDs (for pagination count and
    ``selection_data``) together with a small page of rich ``SearchHit``
    dicts (for serialization).  DRF's ``PageNumberPagination`` calls
    ``__len__`` to compute the total page count and ``__getitem__`` to
    slice the displayed page.

    Args:
        ordered_ids: All matching document IDs in display order.
        page_hits: Rich SearchHit dicts for the requested DRF page only.
        page_offset: Index into *ordered_ids* where *page_hits* starts.
    """

    def __init__(
        self,
        ordered_ids: list[int],
        page_hits: list[SearchHit],
        page_offset: int = 0,
    ) -> None:
        self._ordered_ids = ordered_ids
        self._page_hits = page_hits
        self._page_offset = page_offset

    def __len__(self) -> int:
        return len(self._ordered_ids)

    def __getitem__(self, key: int | slice) -> SearchHit | list[SearchHit]:
        if isinstance(key, int):
            idx = key if key >= 0 else len(self._ordered_ids) + key
            if self._page_offset <= idx < self._page_offset + len(self._page_hits):
                return self._page_hits[idx - self._page_offset]
            return SearchHit(
                id=self._ordered_ids[key],
                score=0.0,
                rank=idx + 1,
                highlights={},
            )
        start = key.start or 0
        stop = key.stop or len(self._ordered_ids)
        # DRF slices to extract the current page.  If the slice aligns
        # with our pre-fetched page_hits, return them directly.
        # We only check start — DRF always slices with stop=start+page_size,
        # which exceeds page_hits length on the last page.
        if start == self._page_offset:
            return self._page_hits[: stop - start]
        # Fallback: return stub dicts (no highlights).
        return [
            SearchHit(id=doc_id, score=0.0, rank=start + i + 1, highlights={})
            for i, doc_id in enumerate(self._ordered_ids[key])
        ]

    def get_all_ids(self) -> list[int]:
        """Return all matching document IDs in display order."""
        return self._ordered_ids


class WriteBatch:
    """
    Context manager for bulk index operations with file locking.

    Provides transactional batch updates to the search index with proper
    concurrency control via file locking. All operations within the batch
    are committed atomically or rolled back on exception.

    Usage:
        with backend.batch_update() as batch:
            batch.add_or_update(record)
            batch.remove(doc_id)
    """

    def __init__(self, backend: TantivyEngine, lock_timeout: float):
        self._backend = backend
        self._lock_timeout = lock_timeout
        self._raw_writer: tantivy.IndexWriter | None = None
        self._lock = None

    @property
    def _writer(self) -> tantivy.IndexWriter:
        assert self._raw_writer is not None, (
            "WriteBatch not entered; use as context manager"
        )
        return self._raw_writer

    def __enter__(self) -> Self:
        lock_path = self._backend._path / ".tantivy.lock"
        self._lock = filelock.FileLock(str(lock_path))
        try:
            self._lock.acquire(timeout=self._lock_timeout)
        except filelock.Timeout as exc:
            raise SearchIndexLockError("Could not acquire search index lock") from exc

        # Open a fresh Index (and thus a fresh Tantivy ManagedDirectory)
        # for the write, rather than reusing the process-local cached
        # index. ManagedDirectory loads its GC bookkeeping (.managed.json)
        # once, at construction, and never re-reads it; paperless runs
        # several long-lived processes (Granian workers, Celery workers)
        # that take turns writing under the file lock above. A cached,
        # long-lived writer index would carry a stale managed-files view
        # and, on commit, overwrite .managed.json with that stale view -
        # permanently losing track of segment files other processes
        # registered in the meantime, so they can never be garbage
        # collected. Reopening fresh here always picks up the current
        # on-disk state. The long-lived self._backend._index is used for
        # reads only and is reloaded (not reopened) after commit below.
        try:
            write_index = tantivy.Index(
                build_schema(),
                path=str(self._backend._path),
            )
            register_tokenizers(write_index, self._backend._language)
            self._raw_writer = write_index.writer()
        except BaseException:
            self._lock.release()
            raise
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        try:
            if exc_type is None:
                self._writer.commit()
                # Wait for background merge threads to finish before releasing
                # the file lock so the next writer doesn't race against an
                # in-progress merge on the same index files.
                self._writer.wait_merging_threads()
                self._backend._index.reload()
        finally:
            # Always release the writer (and Tantivy's internal writer lock),
            # even if commit/merge/reload raised, so the next batch can acquire
            # a writer instead of failing with LockBusy. An uncommitted writer
            # is simply discarded.
            if self._raw_writer is not None:
                del self._raw_writer
                self._raw_writer = None
            if self._lock is not None:
                self._lock.release()

    def add_or_update(self, record: IndexRecord) -> None:
        """Upsert a plain index record under the acquired writer lock."""
        self.remove(record["id"][0])
        self._writer.add_document(
            tantivy.Document.from_dict(record, self._backend._schema),
        )

    def remove(self, doc_id: int) -> None:
        """Remove a document from the batch by its primary key."""
        self._writer.delete_documents_by_query(
            tantivy.Query.term_query(self._backend._schema, "id", doc_id),
        )


def build_permission_filter(
    schema: tantivy.Schema,
    user: SearchVisibility,
) -> tantivy.Query:
    """
    Build a query filter for user document permissions.

    Creates a query that matches only documents visible to the specified user
    according to paperless-ngx permission rules:
    - Public documents (no owner) are visible to all users
    - Private documents are visible to their owner
    - Documents explicitly shared with the user are visible
    - Documents shared with one of the user's current groups are visible

    Args:
        schema: Tantivy schema for field validation
        user: User to check permissions for

    Returns:
        Tantivy query that filters results to visible documents
    """
    owner_any = tantivy.Query.exists_query("owner_id")
    no_owner = tantivy.Query.boolean_query(
        [
            (tantivy.Occur.Must, tantivy.Query.all_query()),
            (tantivy.Occur.MustNot, owner_any),
        ],
    )
    owned = tantivy.Query.term_query(schema, "owner_id", user.user_id)
    shared = tantivy.Query.term_query(schema, "viewer_id", user.user_id)
    group_shared = [
        tantivy.Query.term_query(schema, "viewer_group_id", group_id)
        for group_id in user.group_ids
    ]
    return tantivy.Query.disjunction_max_query(
        [no_owner, owned, shared, *group_shared],
    )


class TantivyEngine:
    """
    Tantivy search backend with explicit lifecycle management.

    Provides full-text search capabilities using the Tantivy search engine.
    Keeps a persistent on-disk index. Handles document indexing, search queries,
    autocompletion, and "more like this" functionality.

    The backend manages its own connection lifecycle and can be reset when
    the underlying index directory changes (e.g., during test isolation).
    """

    # Maps DRF ordering field names to Tantivy index field names.
    SORT_FIELD_MAP: dict[str, str] = {
        "title": "title_sort",
        "correspondent__name": "correspondent_sort",
        "document_type__name": "type_sort",
        "created": "created",
        "added": "added",
        "modified": "modified",
        "archive_serial_number": "asn",
        "page_count": "page_count",
        "num_notes": "num_notes",
    }

    # Fields where Tantivy's sort order matches the ORM's sort order.
    # Text-based fields (title, correspondent__name, document_type__name)
    # are excluded because Tantivy's tokenized fast fields produce different
    # ordering than the ORM's collation-based ordering.
    SORTABLE_FIELDS: frozenset[str] = frozenset(
        {
            "created",
            "added",
            "modified",
            "archive_serial_number",
            "page_count",
            "num_notes",
        },
    )

    def __init__(self, path: Path, *, language: str | None):
        self._path = path
        self._language = language
        self._raw_index: tantivy.Index | None = None
        self._raw_schema: tantivy.Schema | None = None

    @property
    def _index(self) -> tantivy.Index:
        assert self._raw_index is not None, "Index not open; call open() first"
        return self._raw_index

    @property
    def _schema(self) -> tantivy.Schema:
        assert self._raw_schema is not None, "Schema not open; call open() first"
        return self._raw_schema

    def open(self) -> None:
        """
        Open or rebuild the index as needed.

        Checks if rebuilding is needed due to schema version or language
        changes. Registers custom tokenizers after opening.
        Safe to call multiple times - subsequent calls are no-ops.
        """
        if self._raw_index is not None:
            return  # pragma: no cover
        self._raw_index = open_or_rebuild_index(self._path, language=self._language)
        register_tokenizers(self._raw_index, self._language)
        self._raw_schema = self._raw_index.schema

    def close(self) -> None:
        """
        Close the index and release resources.

        Safe to call multiple times - subsequent calls are no-ops.
        """
        self._raw_index = None
        self._raw_schema = None

    def _ensure_open(self) -> None:
        """Ensure the index is open before operations."""
        if self._raw_index is None:
            self.open()  # pragma: no cover

    def _parse_query(
        self,
        query: str,
        search_mode: SearchMode,
        config: QueryConfiguration,
    ) -> tantivy.Query:
        """Parse a user query string into a Tantivy Query object."""
        query = normalize_search_text(query)
        if search_mode is SearchMode.TEXT:
            return parse_simple_text_query(self._index, query)
        elif search_mode is SearchMode.TITLE:
            return parse_simple_title_query(self._index, query)
        else:
            return parse_user_query(
                self._index,
                query,
                config.timezone,
                language=config.language,
                fuzzy_threshold=config.fuzzy_threshold,
            )

    def _apply_permission_filter(
        self,
        query: tantivy.Query,
        user: SearchVisibility | None,
    ) -> tantivy.Query:
        """Apply the explicit visibility snapshot, or leave an unfiltered query."""
        if user is not None:
            permission_filter = self._build_permission_filter(user)
            return tantivy.Query.boolean_query(
                [
                    (tantivy.Occur.Must, query),
                    (tantivy.Occur.Must, permission_filter),
                ],
            )
        return query

    def _build_permission_filter(self, user: SearchVisibility) -> tantivy.Query:
        """Build a permission filter from explicit current visibility IDs."""
        return build_permission_filter(self._schema, user)

    def highlight_hits(
        self,
        query: str,
        doc_ids: list[int],
        *,
        config: QueryConfiguration,
        search_mode: SearchMode = SearchMode.QUERY,
        rank_start: int = 1,
    ) -> list[SearchHit]:
        """
        Generate SearchHit dicts with highlights for specific document IDs.

        Unlike search(), this does not execute a ranked query — it looks up
        each document by ID and generates snippets against the provided query.
        Use this when you already know which documents to display (from
        search_ids + ORM filtering) and just need highlight data.

        Args:
            query: The search query (used for snippet generation)
            doc_ids: Ordered list of document IDs to generate hits for
            search_mode: Query parsing mode (for building the snippet query)
            rank_start: Starting rank value (1-based absolute position in the
                full result set; pass ``page_offset + 1`` for paginated calls)

        Returns:
            List of SearchHit dicts in the same order as doc_ids
        """
        if not doc_ids:
            return []

        self._ensure_open()
        user_query = self._parse_query(query, search_mode, config)
        # _parse_query normalizes its own copy; the snippet queries below are
        # built from the string directly, so normalize it here too.
        query = normalize_search_text(query)
        highlight_query = user_query
        if search_mode is SearchMode.TEXT:
            try:
                highlight_query = parse_simple_text_highlight_query(
                    self._index,
                    query,
                )
            except ValueError:
                logger.debug(
                    "Skipping simple text highlight query: token string is not "
                    "valid tantivy query syntax: %r",
                    query,
                )

        # For notes_text snippet generation, we need a query that targets the
        # notes_text field directly. user_query may contain JSON-field terms
        # (e.g. notes.note:urgent) that the SnippetGenerator cannot resolve
        # against a text field. Strip field:value prefixes so bare terms like
        # "urgent" are re-parsed against notes_text, producing highlights even
        # when the original query used structured syntax.
        bare_query = re.sub(r"\w[\w.]*:", "", query).strip()
        try:
            notes_text_query = (
                self._index.parse_query(bare_query, ["notes_text"])
                if bare_query
                else user_query
            )
        except Exception:
            notes_text_query = user_query

        searcher = self._index.searcher()

        # Fetch all requested docs in a single search: user_query MUST match
        # and exactly the requested IDs MUST match (OR of term_queries).
        id_filter = tantivy.Query.boolean_query(
            [
                (
                    tantivy.Occur.Should,
                    tantivy.Query.term_query(self._schema, "id", did),
                )
                for did in doc_ids
            ],
        )
        batch_query = tantivy.Query.boolean_query(
            [
                (tantivy.Occur.Must, user_query),
                (tantivy.Occur.Must, id_filter),
            ],
        )
        batch_results = searcher.search(batch_query, limit=len(doc_ids))

        result_addrs = [addr for _score, addr in batch_results.hits]
        result_ids = cast("list[int]", searcher.fast_field_values("id", result_addrs))
        addr_by_id: dict[int, tuple[float, tantivy.DocAddress]] = {
            doc_id: (score, addr)
            for (score, addr), doc_id in zip(batch_results.hits, result_ids)
        }

        snippet_generator = None
        notes_snippet_generator = None
        hits: list[SearchHit] = []

        for rank, doc_id in enumerate(doc_ids, start=rank_start):
            if doc_id not in addr_by_id:
                continue

            score, doc_address = addr_by_id[doc_id]
            actual_doc = searcher.doc(doc_address)
            doc_dict = actual_doc.to_dict()

            highlights: dict[str, str] = {}
            try:
                if snippet_generator is None:
                    snippet_generator = tantivy.SnippetGenerator.create(
                        searcher,
                        highlight_query,
                        self._schema,
                        "content",
                    )

                content_html = snippet_generator.snippet_from_doc(actual_doc).to_html()
                if content_html:
                    highlights["content"] = content_html

                if search_mode is SearchMode.QUERY and "notes_text" in doc_dict:
                    # Use notes_text (plain text) for snippet generation — tantivy's
                    # SnippetGenerator does not support JSON fields.
                    if notes_snippet_generator is None:
                        notes_snippet_generator = tantivy.SnippetGenerator.create(
                            searcher,
                            notes_text_query,
                            self._schema,
                            "notes_text",
                        )
                    notes_html = notes_snippet_generator.snippet_from_doc(
                        actual_doc,
                    ).to_html()
                    if notes_html:
                        highlights["notes"] = notes_html

            except Exception:  # pragma: no cover
                logger.debug("Failed to generate highlights for doc %s", doc_id)

            hits.append(
                SearchHit(
                    id=doc_id,
                    score=score,
                    rank=rank,
                    highlights=highlights,
                ),
            )

        return hits

    def search_ids(
        self,
        query: str,
        user: SearchVisibility | None,
        *,
        sort_field: str | None = None,
        sort_reverse: bool = False,
        config: QueryConfiguration,
        search_mode: SearchMode = SearchMode.QUERY,
        filter_query: str | None = None,
        limit: int | None = None,
    ) -> list[int]:
        """
        Return document IDs matching a query — no highlights or scores.

        This is the lightweight companion to search(). Use it when you need the
        full set of matching IDs (e.g. for ``selection_data``) but don't need
        scores, ranks, or highlights.

        Args:
            query: User's search query
            user: User for permission filtering (None for superuser/no filtering)
            sort_field: Field to sort by (None for relevance ranking)
            sort_reverse: Whether to reverse the sort order
            search_mode: Query parsing mode (QUERY, TEXT, or TITLE)
            filter_query: Optional advanced query results must also match
            limit: Maximum number of IDs to return (None = all matching docs)

        Returns:
            List of document IDs in the requested order
        """
        self._ensure_open()
        user_query = self._parse_query(query, search_mode, config)
        if filter_query is not None:
            user_query = user_query.and_must_match(
                tantivy.Query.const_score_query(
                    self._parse_query(filter_query, SearchMode.QUERY, config),
                    0.0,  # zero so doesn't affect relevance
                ),
            )
        final_query = self._apply_permission_filter(user_query, user)

        searcher = self._index.searcher()
        effective_limit = limit if limit is not None else searcher.num_docs
        if effective_limit <= 0:
            return []

        if sort_field and sort_field in self.SORT_FIELD_MAP:
            mapped_field = self.SORT_FIELD_MAP[sort_field]
            results = searcher.search(
                final_query,
                limit=effective_limit,
                order_by_field=mapped_field,
                order=tantivy.Order.Desc if sort_reverse else tantivy.Order.Asc,
            )
            all_hits = [(hit[1],) for hit in results.hits]
        else:
            results = searcher.search(final_query, limit=effective_limit)
            all_hits = [(hit[1], hit[0]) for hit in results.hits]

            # Normalize scores and apply threshold (relevance search only)
            if all_hits:
                max_score = max(hit[1] for hit in all_hits) or 1.0
                all_hits = [(hit[0], hit[1] / max_score) for hit in all_hits]

            threshold = config.fuzzy_threshold
            if threshold is not None:
                all_hits = [hit for hit in all_hits if hit[1] >= threshold]

        return cast(
            "list[int]",
            searcher.fast_field_values("id", [doc_addr for doc_addr, *_ in all_hits]),
        )

    def autocomplete(
        self,
        term: str,
        limit: int,
        user: SearchVisibility | None = None,
    ) -> list[str]:
        """
        Get autocomplete suggestions for search queries.

        Returns words that start with the given term prefix, ranked by document
        frequency (how many documents contain each word). Optionally filters
        results to only words from documents visible to the specified user.

        NOTE: This is the hottest search path (called per keystroke).
        A future improvement would be to cache results in Redis, keyed by
        (prefix, user_id), and invalidate on index writes.

        Args:
            term: Prefix to match against autocomplete words
            limit: Maximum number of suggestions to return
            user: User for permission filtering (None for no filtering)

        Returns:
            List of word suggestions ordered by frequency, then alphabetically
        """
        self._ensure_open()
        normalized_term = ascii_fold(term.lower())
        if not normalized_term:
            return []

        searcher = self._index.searcher()

        permission_query = None
        # Intersect with permission filter so autocomplete words from
        # invisible documents don't leak to other users.
        if user is not None:
            permission_query = self._build_permission_filter(user)

        matches = searcher.terms_with_prefix(
            "autocomplete_word",
            normalized_term,
            permission_query,
            limit,
        )

        return [x[0] for x in matches]

    def more_like_this_ids(
        self,
        doc_id: int,
        user: SearchVisibility | None,
        *,
        limit: int | None = None,
    ) -> list[int]:
        """
        Return IDs of documents similar to the given document — no highlights.

        Lightweight companion to more_like_this(). The original document is
        excluded from results.

        Args:
            doc_id: Primary key of the reference document, or of one of its
                versions
            user: User for permission filtering (None for no filtering)
            limit: Maximum number of IDs to return (None = all matching docs)

        Returns:
            List of similar document IDs (excluding the original)
        """
        self._ensure_open()
        searcher = self._index.searcher()

        id_query = tantivy.Query.term_query(self._schema, "id", doc_id)
        results = searcher.search(id_query, limit=1)

        if not results.hits:
            return []

        doc_address = results.hits[0][1]
        mlt_query = tantivy.Query.more_like_this_query(
            doc_address,
            min_doc_frequency=1,
            max_doc_frequency=None,
            min_term_frequency=1,
            max_query_terms=12,
            min_word_length=None,
            max_word_length=None,
            boost_factor=None,
        )

        final_query = self._apply_permission_filter(mlt_query, user)

        effective_limit = limit if limit is not None else searcher.num_docs
        try:
            # Fetch one extra to account for excluding the original document
            results = searcher.search(final_query, limit=effective_limit + 1)
        except BaseException:  # pragma: no cover
            # Tantivy 0.26 panics in BM25 idf scoring when the index holds
            # soft-deleted documents (doc_freq can exceed the alive doc count),
            # which only surfaces for the More Like This query. The panic crosses
            # the pyo3 boundary as a `pyo3_runtime.PanicException` — a
            # BaseException, not an Exception — so catch BaseException and degrade
            # to "no similar documents" instead of bubbling a 500 to the client.
            # Fixed upstream: https://github.com/quickwit-oss/tantivy/pull/2964
            # Remove once the bundled tantivy includes that fix.
            logger.warning(
                "More Like This scoring panicked (likely stale tantivy segment "
                "stats after deletions); returning no results. A search index "
                "reindex will rebuild consistent statistics.",
            )
            return []

        addrs = [addr for _score, addr in results.hits]
        all_ids = cast("list[int]", searcher.fast_field_values("id", addrs))
        ids = [rid for rid in all_ids if rid != doc_id]
        return ids[:limit] if limit is not None else ids

    def batch_update(self, lock_timeout: float = 30.0) -> WriteBatch:
        """
        Get a batch context manager for bulk index operations.

        Use this for efficient bulk document updates/deletions. All operations
        within the batch are committed atomically at the end of the context.

        Args:
            lock_timeout: Seconds to wait for file lock acquisition

        Returns:
            WriteBatch context manager

        Raises:
            SearchIndexLockError: If lock cannot be acquired within timeout
        """
        self._ensure_open()
        return WriteBatch(self, lock_timeout)

    def rebuild(
        self,
        records: Iterable[IndexRecord],
        writer_heap_bytes: int = 512_000_000,
    ) -> None:
        """
        Rebuild the entire search index from scratch.

        Wipes the existing index and re-indexes all provided documents.
        On failure, restores the previous index state to keep the backend usable.

        Args:
            records: Plain records supplied lazily by the persistence adapter.
            writer_heap_bytes: Tantivy writer memory budget (split across the
                writer's threads). Larger values buffer more docs in RAM before
                flushing a segment, deferring merge work; they do not avoid it.
        """
        wipe_index(self._path)
        # The marker covers the window where the empty index is already stamped
        # as current but not yet populated, so an interrupted rebuild is retried.
        with rebuild_in_progress(self._path):
            new_index = tantivy.Index(build_schema(), path=str(self._path))
            _write_sentinels(self._path, language=self._language)
            register_tokenizers(new_index, self._language)

            # Retain the previous reader if rebuilding fails.
            old_index, old_schema = self._raw_index, self._raw_schema
            self._raw_index = new_index
            self._raw_schema = new_index.schema
            try:
                writer = new_index.writer(heap_size=writer_heap_bytes)
                for record in records:
                    writer.add_document(
                        tantivy.Document.from_dict(record, self._schema)
                    )
                writer.commit()
                # Wait for background merge threads to finish so all segments
                # are fully merged and persisted before the index is considered
                # rebuilt.
                writer.wait_merging_threads()
                new_index.reload()
            except BaseException:  # pragma: no cover
                # Restore old index on failure so the backend remains usable
                self._raw_index = old_index
                self._raw_schema = old_schema
                raise
