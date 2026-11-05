"""Independent SQL-shaped providers exercise the public search boundary."""

from __future__ import annotations

import os
import subprocess
import sys
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import replace
from pathlib import Path

import pytest
from django.contrib.auth.models import Group
from django.core.exceptions import PermissionDenied
from django.core.management import call_command
from django.db import connection
from django.db.models import Q
from django.utils.html import escape

from documents.models import Document
from documents.search import SearchHit
from documents.search import SearchMode
from documents.search import SearchPage
from documents.search import SearchQueryError
from documents.search import get_backend
from documents.search import reset_backend
from documents.versioning import annotate_effective_content
from paperless_testing.factories import DocumentFactory
from paperless_testing.factories import UserFactory
from paperless_testing.permissions import grant_object


class DatabaseProvider:
    """Independent replacement using authoritative SQL rather than an index schema.

    Writes record facts for assertions; search reads current document rows. This
    intentionally small test implementation supports literal queries only.
    """

    def __init__(self):
        self.records = {}
        self.closed = False
        self.optimized = False
        self.rebuilt = False

    def open(self):
        """No separate storage is required for an authoritative SQL reader."""

    def close(self):
        """Expose lifetime to tests."""
        self.closed = True

    def query(self, request):
        """Compose authorized scope before filtering, ordering and pagination."""
        if request.filter_query or any(c in request.query for c in ":*()"):
            raise SearchQueryError("This provider supports literal text only")
        rows = annotate_effective_content(request.scope)
        if request.similar_to is not None:
            seed = request.scope.get(pk=request.similar_to)
            rows = rows.filter(title=seed.title).exclude(pk=seed.pk)
        elif request.query:
            predicate = Q(title__icontains=request.query)
            if request.mode != SearchMode.TITLE:
                predicate |= Q(effective_content__icontains=request.query)
            rows = rows.filter(predicate)
        if request.ordering not in {
            None,
            "score",
            "id",
            "title",
            "created",
            "modified",
            "added",
        }:
            raise SearchQueryError("Unsupported ordering")
        ordering = request.ordering or "id"
        if ordering == "score":
            ordering = "id"
        return rows.order_by(("-" if request.descending else "") + ordering, "pk")

    def matching_ids(self, request):
        """Return an unevaluated SQL query, not a Python intersection."""
        ids = self.query(request).values_list("pk", flat=True)
        stop = request.offset + request.limit if request.limit is not None else None
        return ids[request.offset : stop]

    def search(self, request):
        """Count and page in the database, retaining a lazy selection query."""
        rows = self.query(request)
        count = rows.count()
        stop = request.offset + request.limit if request.limit is not None else None
        ids = list(rows.values_list("pk", flat=True)[request.offset : stop])
        hits = self.highlight(request, ids, request.offset + 1)
        return SearchPage(
            count,
            hits,
            request.offset,
            lambda: self.matching_ids(replace(request, offset=0, limit=None)),
        )

    def highlight(self, request, ids, rank_start):
        """Use only authorized rows to generate even illustrative highlights."""
        titles = dict(request.scope.filter(pk__in=ids).values_list("pk", "title"))
        return [
            SearchHit(
                id=pk,
                score=1.0,
                rank=rank,
                highlights={"title": str(escape(titles[pk]))},
            )
            for rank, pk in enumerate(ids, rank_start)
            if pk in titles
        ]

    def autocomplete(self, term, limit, scope):
        """Use live permissions for prefix completion."""
        return list(
            scope.filter(title__startswith=term)
            .order_by("title")
            .values_list("title", flat=True)
            .distinct()[:limit]
        )

    @contextmanager
    def batch_update(self):
        """Represent an atomic replacement batch without lock-specific methods."""
        before = deepcopy(self.records)
        try:
            yield self
        except BaseException:
            self.records = before
            raise

    def add_or_update(self, record):
        """Capture neutral facts supplied by core."""
        self.records[record["id"][0]] = record

    def remove(self, document_id):
        """Accept explicit deletions after the core row is gone."""
        self.records.pop(document_id, None)

    def rebuild(self, records, *, writer_heap_bytes):
        """Own replacement storage rebuild policy."""
        self.records = {record["id"][0]: record for record in records}
        self.rebuilt = True

    def needs_rebuild(self):
        """Return replacement-specific freshness."""
        return not self.rebuilt

    def wipe(self):
        """Discard only derived test state."""
        self.records.clear()

    def optimize(self):
        """Expose provider maintenance dispatch."""
        self.optimized = True

    def last_modified(self):
        """No separate index timestamp exists."""
        return None


class PostgreSQLProvider(DatabaseProvider):
    """Executable contract assessment using PostgreSQL full-text expressions."""

    def query(self, request):
        """Authorize, match, rank and order entirely in the SQL expression tree."""
        from django.contrib.postgres.search import SearchQuery
        from django.contrib.postgres.search import SearchRank
        from django.contrib.postgres.search import SearchVector

        if (
            request.filter_query
            or request.mode == SearchMode.QUERY
            or request.similar_to
        ):
            raise SearchQueryError(
                "The assessment implements literal text/title queries only"
            )
        rows = annotate_effective_content(request.scope)
        vector = SearchVector("title", config="english")
        if request.mode != SearchMode.TITLE:
            vector += SearchVector("effective_content", config="english")
        query = SearchQuery(request.query, config="english", search_type="plain")
        return (
            rows.annotate(search_vector=vector, relevance=SearchRank(vector, query))
            .filter(search_vector=query)
            .order_by("-relevance", "pk")
        )


@pytest.fixture
def replacement(settings, paperless_dirs):
    """Select an installed class without native search storage."""
    settings.SEARCH_PROVIDER = __name__ + ".DatabaseProvider"
    settings.INDEX_DIR = paperless_dirs.index_dir / "never-created"
    reset_backend()
    yield get_backend()
    assert not settings.INDEX_DIR.exists()
    reset_backend()


@pytest.mark.django_db
def test_sql_provider_paginates_filters_and_live_permissions(replacement, admin_client):
    """Normal HTTP search delegates count/page with current authorized scope."""
    viewer = UserFactory()
    owner = UserFactory()
    group = Group.objects.create(name="search readers")
    viewer.groups.add(group)
    private = DocumentFactory(owner=owner, title="invoice private")
    shared = DocumentFactory(owner=owner, title="invoice shared")
    public = DocumentFactory(title="invoice public", owner=None)
    grant_object(group, shared, "view_document")
    request = replacement.request(
        "invoice", viewer, mode=SearchMode.TEXT, ordering="title", limit=1
    )
    page = replacement.search(request)
    assert page.count == 2
    assert page.hits[0]["id"] == public.pk
    assert list(page.matching_ids()) == [public.pk, shared.pk]
    assert replacement.search(replace(request, offset=1)).hits[0]["id"] == shared.pk
    assert replacement.autocomplete("invoice", 10, viewer) == [
        public.title,
        shared.title,
    ]
    viewer.groups.remove(group)
    assert replacement.search(request).count == 1
    assert replacement.autocomplete("invoice", 10, viewer) == [public.title]
    assert (
        replacement.highlight_hits("invoice", [private.pk, shared.pk], user=viewer)
        == []
    )
    assert replacement.more_like_this_ids(private.pk, viewer) == []
    response = admin_client.get(
        "/api/documents/",
        {"text": "invoice", "page_size": 1, "page": 2, "ordering": "title"},
    )
    assert response.status_code == 200
    assert response.data["count"] == 3
    assert len(response.data["results"]) == 1
    assert response.data["results"][0]["id"] == public.pk
    response = admin_client.get("/api/documents/", {"query": "title:invoice"})
    assert response.status_code == 400
    response = admin_client.get(
        "/api/documents/",
        {"text": "invoice", "page_size": 1, "page": "last", "ordering": "title"},
    )
    assert response.status_code == 200
    assert response.data["results"][0]["id"] == shared.pk
    response = admin_client.get("/api/documents/", {"text": "invoice", "page": 0})
    assert response.status_code == 404


@pytest.mark.django_db
def test_replacement_receives_facts_updates_deletes_and_maintenance(replacement):
    """Core owns effective content and final reads; the provider owns derived state."""
    root = DocumentFactory(title="original", content="old")
    version = DocumentFactory(root_document=root, content="effective text")
    Document.objects.filter(pk=root.pk).update(title="final")
    replacement.add_or_update(version)
    facts = replacement.provider.records[root.pk]
    assert facts["title"] == ["final"]
    assert facts["content"] == ["effective text"]
    assert (
        not {
            "title_sort",
            "simple_title",
            "bigram_title",
            "autocomplete_word",
            "notes_text",
        }
        & facts.keys()
    )
    assert replacement.search_ids("effective", search_mode=SearchMode.TEXT) == [root.pk]
    with pytest.raises(RuntimeError, match="abort"):
        with replacement.batch_update() as batch:
            batch.remove(root.pk)
            raise RuntimeError("abort")
    assert root.pk in replacement.provider.records
    identifier = root.pk
    root.delete()
    replacement.remove(identifier)
    assert identifier not in replacement.provider.records
    other = DocumentFactory(title="rebuild")
    call_command("document_index", "reindex", recreate=True, no_progress_bar=True)
    # Rebuild closes the service; the original selected instance exposes the call.
    assert replacement.provider.closed
    call_command("document_index", "optimize")
    assert get_backend().provider.optimized
    assert Document.objects.filter(pk=other.pk).exists()
    assert get_backend().last_modified() is None


@pytest.mark.django_db
def test_replacement_failure_propagates_and_provider_change_closes(
    replacement, mocker, settings
):
    """Unclassified SQL failures are not treated as filesystem lock contention."""
    doc = DocumentFactory()
    mocker.patch.object(
        replacement.provider,
        "batch_update",
        side_effect=RuntimeError("storage unavailable"),
    )
    dispatch = mocker.patch("documents.search._backend.defer_index_write")
    with pytest.raises(RuntimeError, match="storage unavailable"):
        replacement.add_or_update(doc)
    dispatch.assert_not_called()
    settings.SEARCH_PROVIDER = "missing.provider.Class"
    with pytest.raises(ImportError):
        get_backend()
    assert replacement.provider.closed


@pytest.mark.django_db
def test_postgresql_contract_runs_authorization_ranking_and_pagination(replacement):
    """Real PostgreSQL proves scope/ranking/count/page can remain SQL-side."""
    if connection.vendor != "postgresql":
        pytest.skip("PostgreSQL expression assessment")
    from documents.search._backend import SearchService

    service = SearchService(PostgreSQLProvider())
    viewer = UserFactory()
    root = DocumentFactory(owner=viewer, title="invoice invoice", content="old")
    DocumentFactory(root_document=root, content="paid invoice")
    DocumentFactory(owner=viewer, title="invoice")
    DocumentFactory(owner=UserFactory(), title="invoice secret")
    request = service.request("invoice", viewer, mode=SearchMode.TEXT, limit=1)
    page = service.search(request)
    assert page.count == 2
    assert page.hits[0]["id"] == root.pk
    assert "LIMIT 1" in str(service.provider.matching_ids(request).query)
    assert "ts_rank" in str(service.provider.query(request).query)
    assert "plainto_tsquery" in str(service.provider.query(request).query)
    assert service.search(replace(request, offset=1)).hits[0]["id"] != root.pk


def test_replacement_startup_does_not_import_tantivy(tmp_path):
    """Native modules may be absent even through settings, tasks and management."""
    code = """
import importlib.abc, sys
class RejectNative(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'tantivy' or fullname.startswith('documents.search._engine') or fullname.startswith('documents.search._schema'):
            raise AssertionError('Unselected implementation imported: ' + fullname)
sys.meta_path.insert(0, RejectNative())
import django
django.setup()
from documents.search import get_backend, needs_rebuild, wipe_index
from django.core.management import call_command
call_command("migrate", verbosity=0)
from unittest.mock import patch
from documents.management.commands.document_index import Command
from documents.tasks import index_optimize
provider = get_backend()
assert provider.last_modified() is None
assert needs_rebuild()
with patch("documents.search_operations.run_search.apply_async"):
    wipe_index()
    index_optimize()
assert 'tantivy' not in sys.modules
"""
    env = {
        **os.environ,
        "PAPERLESS_SEARCH_PROVIDER": __name__ + ".DatabaseProvider",
        "PAPERLESS_SEARCH_LANGUAGE": "provider-owned-language",
        "PAPERLESS_DATA_DIR": str(tmp_path),
    }
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[3],
        env=env,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


@pytest.mark.django_db
@pytest.mark.parametrize(
    "implementation",
    ["documents.search.tantivy.TantivyProvider", __name__ + ".DatabaseProvider"],
)
def test_each_provider_obeys_revoked_grants_without_reindex(
    implementation, settings, paperless_dirs
):
    """Read permissions come from current core state, not stale provider records."""
    from guardian.shortcuts import remove_perm

    settings.SEARCH_PROVIDER = implementation
    reset_backend()
    backend = get_backend()
    user = UserFactory()
    document = DocumentFactory(owner=UserFactory(), title="confidential invoice")
    grant_object(user, document, "view_document")
    backend.add_or_update(document)
    assert backend.search_ids("confidential", user, search_mode=SearchMode.TEXT) == [
        document.pk
    ]
    assert backend.autocomplete("confidential", 10, user)
    remove_perm("view_document", user, document)
    assert backend.search_ids("confidential", user, search_mode=SearchMode.TEXT) == []
    assert backend.autocomplete("confidential", 10, user) == []
    assert backend.highlight_hits("confidential", [document.pk], user=user) == []
    assert backend.more_like_this_ids(document.pk, user) == []
    reset_backend()


@pytest.mark.django_db
def test_page_does_not_materialize_selection_ids(replacement, admin_client, mocker):
    """Modern HTTP pagination needs only SQL count and the requested page."""
    DocumentFactory(title="invoice")
    mocker.patch.object(
        replacement.provider,
        "matching_ids",
        side_effect=AssertionError("all IDs loaded"),
    )
    response = admin_client.get(
        "/api/documents/",
        {"text": "invoice", "page_size": 1},
        HTTP_ACCEPT="application/json; version=10",
    )
    assert response.status_code == 200
    assert response.data["count"] == 1
    assert "all" not in response.data


@pytest.mark.django_db
def test_core_rejects_out_of_scope_provider_hits(replacement, mocker):
    """A broken provider cannot attach another document's highlights to a page."""
    user = UserFactory()
    private = DocumentFactory(owner=UserFactory())
    mocker.patch.object(
        replacement.provider,
        "search",
        return_value=SearchPage(
            1,
            [
                SearchHit(
                    id=private.pk, score=1.0, rank=1, highlights={"content": "private"}
                )
            ],
            0,
            lambda: [private.pk],
        ),
    )
    with pytest.raises(ValueError, match="outside authorized scope"):
        replacement.search(replacement.request("private", user))


@pytest.mark.django_db
def test_similarity_seed_authorization_is_live_and_independent_of_filters(
    replacement, mocker
):
    """A private seed never reaches providers, even after request-time revocation."""
    from guardian.shortcuts import remove_perm

    viewer = UserFactory()
    seed = DocumentFactory(owner=UserFactory())
    result = DocumentFactory(owner=viewer)
    request = replacement.request(
        user=viewer,
        similar_to=seed.pk,
        scope=Document.objects.filter(pk=result.pk),
    )
    search = mocker.patch.object(
        replacement.provider,
        "search",
        return_value=SearchPage(0, [], 0, list),
    )
    with pytest.raises(PermissionDenied):
        replacement.search(request)
    search.assert_not_called()

    grant_object(viewer, seed, "view_document")
    assert replacement.search(request).count == 0
    search.assert_called_once()
    search.reset_mock()
    remove_perm("view_document", viewer, seed)
    with pytest.raises(PermissionDenied):
        replacement.search(request)
    search.assert_not_called()


@pytest.mark.parametrize("provider", ["", "external.search.SQLProvider"])
def test_docker_initializes_only_selected_search_storage(tmp_path, provider):
    """Folder setup does not require Tantivy storage for an installed replacement."""
    script = (
        Path(__file__).resolve().parents[4]
        / "docker/rootfs/etc/s6-overlay/s6-rc.d/init-folders/run"
    )
    command = 'mkdir() { printf "%s\\n" "$@"; }; find() { :; }; . "$1"'
    result = subprocess.run(
        ["bash", "-c", command, "test", str(script)],
        env={
            **os.environ,
            "PAPERLESS_DATA_DIR": str(tmp_path / "data"),
            "PAPERLESS_SEARCH_PROVIDER": provider,
            "USER_IS_NON_ROOT": "1",
        },
        capture_output=True,
        text=True,
        check=True,
    )
    assert (str(tmp_path / "data/index") in result.stdout) == (not provider)


@pytest.mark.django_db
@pytest.mark.parametrize(
    "implementation",
    ["documents.search.tantivy.TantivyProvider", __name__ + ".DatabaseProvider"],
)
def test_provider_request_orders_without_a_view(
    implementation, settings, paperless_dirs
):
    """Provider-neutral ordering works without relying on HTTP filter setup."""
    settings.SEARCH_PROVIDER = implementation
    reset_backend()
    backend = get_backend()
    last = DocumentFactory(title="z invoice")
    first = DocumentFactory(title="a invoice")
    for document in (last, first):
        backend.add_or_update(document)
    result = backend.search(
        backend.request("invoice", mode=SearchMode.TEXT, ordering="title", limit=1)
    )
    assert result.count == 2
    assert result.hits[0]["id"] == first.pk
    assert list(result.matching_ids()) == [first.pk, last.pk]
    reset_backend()


def test_provider_cache_uses_only_declared_configuration(replacement, settings, mocker):
    """Reuse resources until provider-declared effective configuration changes."""
    settings.TEST_SEARCH_CACHE_VERSION = 1
    mocker.patch.object(
        DatabaseProvider,
        "configuration",
        new=property(lambda self: {"version": settings.TEST_SEARCH_CACHE_VERSION}),
        create=True,
    )
    factory = mocker.Mock(wraps=DatabaseProvider)
    mocker.patch("documents.search._backend.import_string", return_value=factory)
    first = get_backend()
    assert replacement.provider.closed
    assert get_backend() is first
    settings.SEARCH_LANGUAGE = "provider-independent-query-setting"
    assert get_backend() is first
    factory.assert_called_once()
    settings.TEST_SEARCH_CACHE_VERSION = 2
    assert get_backend() is not first
    assert first.provider.closed
    assert factory.call_count == 2


@pytest.fixture(autouse=True)
def process_search_queue(queued_search_processor):
    """Exercise provider writes through claim, submission and validation."""
