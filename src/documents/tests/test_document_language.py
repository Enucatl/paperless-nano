"""Exercise native document language persistence and permission boundaries."""

import importlib
from types import SimpleNamespace

import pytest
from django.apps import apps
from django.core import serializers
from django.db import connection

from documents.job_state import document_revision
from documents.models import Document
from documents.models import SavedView
from documents.models import SavedViewFilterRule
from documents.models import Tag
from paperless_testing.factories import DocumentFactory
from paperless_testing.factories import TagFactory
from paperless_testing.factories import UserFactory
from paperless_testing.permissions import grant_global
from paperless_testing.permissions import grant_object

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures("paperless_dirs")]


@pytest.mark.parametrize("language", ["de", "cmn", "und", ""])
def test_api_language_edit(admin_client, language):
    """Canonical, unknown and explicitly unset values survive a document edit."""
    document = DocumentFactory(language="en")
    before = document_revision(document)
    response = admin_client.patch(
        f"/api/documents/{document.pk}/",
        {"language": language},
        format="json",
    )
    assert response.status_code == 200
    assert response.data["language"] == language
    document.refresh_from_db()
    assert document.language == language
    assert document_revision(document) != before
    assert (
        admin_client.get(f"/api/documents/{document.pk}/").data["language"] == language
    )


@pytest.mark.parametrize("language", ["EN", "xx", "language:de", "zh-cn", ["de"], None])
def test_api_rejects_invalid_language(admin_client, language):
    """API input cannot store unsupported codes, aliases, relations or lists."""
    document = DocumentFactory(language="en")
    response = admin_client.patch(
        f"/api/documents/{document.pk}/",
        {"language": language},
        format="json",
    )
    assert response.status_code == 400
    assert "language" in response.data
    document.refresh_from_db()
    assert document.language == "en"


def test_document_language_exact_and_set_filters(admin_client):
    """Document filters select stored values, including explicit unknown values."""
    english = DocumentFactory(language="en")
    german = DocumentFactory(language="de")
    DocumentFactory(language="und")
    unset = DocumentFactory(language="")
    blank = admin_client.get("/api/documents/", {"language": ""})
    assert blank.status_code == 200
    assert [item["id"] for item in blank.data["results"]] == [unset.pk]
    exact = admin_client.get("/api/documents/", {"language": "de"})
    assert exact.status_code == 200
    assert [item["id"] for item in exact.data["results"]] == [german.pk]
    multiple = admin_client.get("/api/documents/", {"language__in": "en,de"})
    assert multiple.status_code == 200
    assert {item["id"] for item in multiple.data["results"]} == {english.pk, german.pk}
    with_unset = admin_client.get("/api/documents/", {"language__in": ",en"})
    assert with_unset.status_code == 200
    assert {item["id"] for item in with_unset.data["results"]} == {english.pk, unset.pk}


def test_distinct_languages_respect_filters_permissions_and_trash(rest_api_client):
    """Counts include only matching accessible roots and never hidden or trashed values."""
    viewer = UserFactory()
    other = UserFactory()
    grant_global(viewer, "view_document")
    rest_api_client.force_authenticate(viewer)
    tag = TagFactory()
    owned = DocumentFactory(owner=viewer, language="de")
    shared = DocumentFactory(owner=other, language="de")
    grant_object(viewer, shared, "view_document")
    owned.tags.add(tag)
    shared.tags.add(tag)
    DocumentFactory(owner=viewer, language="en")
    DocumentFactory(owner=viewer, language="und")
    DocumentFactory(owner=viewer, language="")
    DocumentFactory(owner=other, language="fr")
    DocumentFactory(owner=other, language="de")
    trashed = DocumentFactory(owner=viewer, language="it")
    trashed.delete()
    DocumentFactory(owner=viewer, language="ja", root_document=owned, version_index=1)
    response = rest_api_client.get("/api/documents/languages/")
    assert response.status_code == 200
    assert response.data == [
        {"language": "", "document_count": 1},
        {"language": "de", "document_count": 2},
        {"language": "en", "document_count": 1},
        {"language": "und", "document_count": 1},
    ]
    filtered = rest_api_client.get(
        "/api/documents/languages/", {"tags__id__in": tag.pk}
    )
    assert filtered.status_code == 200
    assert filtered.data == [{"language": "de", "document_count": 2}]
    filtered = rest_api_client.get("/api/documents/languages/", {"language": "en"})
    assert filtered.data == [{"language": "en", "document_count": 1}]


def test_bulk_language_edit_and_validation(admin_client, mocker):
    """Bulk publication updates revisions and rejects invalid values atomically."""
    mocker.patch("documents.bulk_edit.bulk_update_documents.apply_async")
    first = DocumentFactory(language="en")
    second = DocumentFactory(language="")
    before = document_revision(first)
    payload = {
        "documents": [first.pk, second.pk],
        "method": "set_language",
        "parameters": {"language": "de"},
    }
    response = admin_client.post("/api/documents/bulk_edit/", payload, format="json")
    assert response.status_code == 200
    first.refresh_from_db()
    assert document_revision(first) != before
    assert list(Document.objects.values_list("language", flat=True)) == ["de", "de"]
    payload["parameters"]["language"] = "invalid"
    response = admin_client.post("/api/documents/bulk_edit/", payload, format="json")
    assert response.status_code == 400
    assert list(Document.objects.values_list("language", flat=True)) == ["de", "de"]


def test_language_edits_require_document_permission(rest_api_client, mocker):
    """A reader cannot change language through single or bulk updates."""
    mocker.patch("documents.bulk_edit.bulk_update_documents.apply_async")
    viewer = UserFactory()
    grant_global(viewer, "view_document")
    document = DocumentFactory(owner=UserFactory(), language="en")
    grant_object(viewer, document, "view_document")
    rest_api_client.force_authenticate(viewer)
    response = rest_api_client.patch(
        f"/api/documents/{document.pk}/",
        {"language": "de"},
        format="json",
    )
    assert response.status_code == 403
    response = rest_api_client.post(
        "/api/documents/bulk_edit/",
        {
            "documents": [document.pk],
            "method": "set_language",
            "parameters": {"language": "de"},
        },
        format="json",
    )
    assert response.status_code == 403
    document.refresh_from_db()
    assert document.language == "en"


@pytest.mark.parametrize("language", ["de", "und", ""])
def test_language_export_import_serialization_roundtrip(language):
    """The Django manifest serializer used by export/import preserves native language."""
    document = DocumentFactory(language=language)
    manifest = serializers.serialize("json", [document], use_natural_foreign_keys=True)
    Document.objects.filter(pk=document.pk).update(language="en")
    for entry in serializers.deserialize("json", manifest):
        entry.save()
    document.refresh_from_db()
    assert document.language == language


def test_language_tag_backfill_preserves_tags_and_saved_filters():
    """Backfill zero, single, alias and conflicting claims without deleting user data."""
    migration = importlib.import_module("documents.migrations.0033_document_language")
    german = TagFactory(name="language:de")
    english = TagFactory(name="language:en")
    arabic = TagFactory(name="language:ar")
    invalid = TagFactory(name="language:invalid")
    unrelated = TagFactory(name="important")
    empty = DocumentFactory()
    trashed = DocumentFactory()
    trashed.tags.add(german)
    trashed.delete()
    one = DocumentFactory()
    one.tags.add(german, unrelated)
    conflict = DocumentFactory()
    conflict.tags.add(german, english, unrelated)
    alias = DocumentFactory()
    alias.tags.add(arabic)
    unsupported = DocumentFactory()
    unsupported.tags.add(invalid)
    view = SavedView.objects.create(name="German documents")
    rule = SavedViewFilterRule.objects.create(
        saved_view=view, rule_type=6, value=str(german.pk)
    )
    tags_before = set(
        Document.tags.through.objects.values_list("document_id", "tag_id")
    )
    migration.backfill_language(apps, SimpleNamespace(connection=connection))
    assert dict(Document.global_objects.values_list("pk", "language")) == {
        empty.pk: "",
        trashed.pk: "de",
        one.pk: "de",
        conflict.pk: "und",
        alias.pk: "arb",
        unsupported.pk: "",
    }
    assert (
        set(Document.tags.through.objects.values_list("document_id", "tag_id"))
        == tags_before
    )
    assert {german.pk, english.pk, arabic.pk, invalid.pk, unrelated.pk} <= set(
        Tag.objects.values_list("pk", flat=True),
    )
    rule.refresh_from_db()
    assert (rule.rule_type, rule.value) == (6, str(german.pk))
