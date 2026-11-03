"""Plain search records, explicit query inputs and adapter-owned recovery."""

from datetime import UTC
from datetime import datetime
from datetime import timedelta
from datetime import timezone

import filelock
import pytest
from django.contrib.auth.models import Group
from django.utils import timezone as django_timezone

from documents.exceptions import SearchIndexLockError
from documents.models import Document
from documents.models import Note
from documents.search._engine import TantivyEngine
from documents.search._persistence import documents_for_ids
from documents.search._persistence import index_records
from documents.search._persistence import record_for_document
from documents.search._types import QueryConfiguration
from paperless_testing.factories import DocumentBarcodeFactory
from paperless_testing.factories import DocumentFactory
from paperless_testing.factories import TagFactory
from paperless_testing.factories import UserFactory
from paperless_testing.permissions import grant_object

pytestmark = pytest.mark.search


@pytest.mark.django_db
def test_record_snapshot_retains_root_metadata_version_content_and_grants(
    django_db_blocker,
    backend,
) -> None:
    """A mapped record remains usable with all database access blocked."""
    owner = UserFactory()
    viewer = UserFactory()
    group = Group.objects.create(name="reviewers")
    root = DocumentFactory(title="Cafe\u0301 report", content="old text", owner=owner)
    version = DocumentFactory(
        root_document=root,
        version_index=1,
        content="Current version content",
    )
    root.tags.add(TagFactory(name="Finance"))
    Note.objects.create(document=root, note="Reviewed", user=viewer)
    DocumentBarcodeFactory(document=version, value="LATEST-42")
    grant_object(viewer, root, "view_document")
    grant_object(group, root, "view_document")

    record = record_for_document(version)
    assert record["id"] == [root.pk]
    assert record["title"] == ["Café report"]
    assert record["content"] == ["Current version content"]
    assert record["tag"] == ["Finance"]
    assert record["notes"] == [{"note": "Reviewed", "user": viewer.username}]
    assert record["barcodes"][0]["value"] == "LATEST-42"
    assert record["owner_id"] == [owner.pk]
    assert record["viewer_id"] == [viewer.pk]
    assert record["viewer_group_id"] == [group.pk]

    Document.objects.filter(pk=version.pk).update(content="Later edit")
    with django_db_blocker.block():
        with backend.engine.batch_update() as batch:
            batch.add_or_update(record)
        assert backend.engine.search_ids(
            "current",
            None,
            config=QueryConfiguration("", None, UTC),
        ) == [root.pk]


@pytest.mark.django_db
def test_record_stream_defers_reads_until_iteration(django_assert_num_queries) -> None:
    """Constructing a stream does not freeze state before the writer is acquired."""
    document = DocumentFactory(title="Old title")
    with django_assert_num_queries(0):
        records = index_records(documents_for_ids([document.pk]), chunk_size=1)
    Document.objects.filter(pk=document.pk).update(title="Final workflow title")
    records = list(records)
    assert len(records) == 1
    assert records[0]["title"] == ["Final workflow title"]


def test_engine_uses_explicit_configuration_without_database(
    tmp_path, settings
) -> None:
    """The engine uses its inputs even when Django's fuzzy setting differs."""
    settings.ADVANCED_FUZZY_SEARCH_THRESHOLD = 1.1
    engine = TantivyEngine(tmp_path, language="")
    try:
        with engine.batch_update() as batch:
            batch.add_or_update({"id": [41], "content": ["invoice"]})
        assert engine.search_ids(
            "invoice",
            None,
            config=QueryConfiguration("", None, UTC),
        ) == [41]
        assert (
            engine.search_ids(
                "invoice",
                None,
                config=QueryConfiguration("", 1.1, UTC),
            )
            == []
        )
    finally:
        engine.close()


def test_engine_reports_lock_failure_without_retry_or_dispatch(
    tmp_path, mocker
) -> None:
    """An engine write attempts the lock once and reports failure to its caller."""
    engine = TantivyEngine(tmp_path, language="")
    acquire = mocker.patch(
        "documents.search._engine.filelock.FileLock.acquire",
        side_effect=filelock.Timeout("busy"),
    )
    sleep = mocker.patch("documents.search_jobs.time.sleep")
    dispatch = mocker.patch("documents.search_jobs.defer_index_write")
    try:
        with pytest.raises(SearchIndexLockError):
            with engine.batch_update(lock_timeout=0):
                pass
        acquire.assert_called_once_with(timeout=0)
        sleep.assert_not_called()
        dispatch.assert_not_called()
    finally:
        engine.close()


def test_writer_construction_failure_releases_acquired_lock(tmp_path, mocker) -> None:
    """An index-open failure after lock acquisition does not block later writers."""
    engine = TantivyEngine(tmp_path, language="")
    engine.open()
    batch = engine.batch_update(lock_timeout=0)
    release = mocker.spy(filelock.FileLock, "release")
    constructor = mocker.patch(
        "documents.search._engine.tantivy.Index",
        side_effect=RuntimeError("cannot open writer"),
    )
    try:
        with pytest.raises(RuntimeError, match="cannot open writer"):
            batch.__enter__()
        release.assert_called_once()
        mocker.stop(constructor)
        with engine.batch_update(lock_timeout=0):
            pass
    finally:
        engine.close()


@pytest.mark.django_db
def test_adapter_refreshes_groups_and_timezone_between_queries(backend) -> None:
    """A cached adapter does not retain revoked memberships or request timezones."""
    user = UserFactory()
    group = Group.objects.create(name="readers")
    user.groups.add(group)
    document = DocumentFactory(
        owner=UserFactory(),
        added=datetime(2026, 6, 15, 2, tzinfo=UTC),
    )
    grant_object(group, document, "view_document")
    backend.add_or_update(document)

    with django_timezone.override(UTC):
        assert backend.search_ids("added:2026-06-15", user) == [document.pk]
    with django_timezone.override(timezone(timedelta(hours=-4))):
        assert backend.search_ids("added:2026-06-14", user) == [document.pk]
        assert backend.search_ids("added:2026-06-15", user) == []
    user.groups.remove(group)
    assert backend.search_ids("*", user) == []
