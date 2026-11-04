"""Maintenance updates retain their own workflow and notification policies."""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING
from unittest import mock

import pytest
from django.contrib.admin.sites import AdminSite
from django.core.management import call_command

from documents.admin import DocumentAdmin
from documents.caching import get_suggestion_cache_key
from documents.models import Document
from documents.models import MatchingModel
from documents.models import Note
from paperless_testing.factories import CorrespondentFactory
from paperless_testing.factories import DocumentFactory
from paperless_testing.factories import TagFactory

if TYPE_CHECKING:
    from django.contrib.auth.models import User
    from pytest_django.fixtures import Settings
    from rest_framework.test import APIClient

    from paperless_testing.dirs import PaperlessDirs

pytestmark = pytest.mark.django_db


def test_admin_indexes_persisted_scalar_and_related_changes(
    paperless_dirs: PaperlessDirs,
) -> None:
    """Admin indexing must observe both model and related-field saves."""
    document = DocumentFactory(title="before")
    tag = TagFactory()
    admin = DocumentAdmin(Document, AdminSite())
    form = SimpleNamespace(instance=document, save_m2m=lambda: document.tags.set([tag]))
    indexed = []

    def capture_index(instance: Document) -> None:
        saved = Document.objects.get(pk=instance.pk)
        indexed.append((saved.title, list(saved.tags.all())))

    with (
        mock.patch("documents.search.get_backend") as backend,
        mock.patch("documents.workflows.runner.run_workflows") as workflows,
        mock.patch("documents.signals.document_updated.send") as notifications,
    ):
        backend.return_value.add_or_update.side_effect = capture_index
        document.title = "after"
        admin.save_model(None, document, form, change=True)
        assert not indexed
        admin.save_related(None, form, [], change=True)

    assert indexed == [("after", [tag])]
    workflows.assert_not_called()
    notifications.assert_not_called()


@pytest.mark.parametrize("suggest", [False, True])
def test_retagger_preserves_apply_and_suggestion_policies(
    paperless_dirs: PaperlessDirs,
    settings: Settings,
    *,
    suggest: bool,
) -> None:
    """Suggestion mode leaves files untouched; apply mode persists all assignments."""
    settings.FILENAME_FORMAT = "{correspondent}/{title}"
    document = DocumentFactory(
        title="invoice", content="needle", mime_type="application/pdf"
    )
    correspondent = CorrespondentFactory(
        name="Vendor",
        match="needle",
        matching_algorithm=MatchingModel.MATCH_LITERAL,
    )
    tag = TagFactory(match="needle", matching_algorithm=MatchingModel.MATCH_LITERAL)
    original = paperless_dirs.originals_dir / "original.pdf"
    original.write_bytes(b"keep the original")
    Document.objects.filter(pk=document.pk).update(filename=original.name)
    document.refresh_from_db()
    modified = document.modified
    args = ["--correspondent", "--tags"]
    if suggest:
        args.append("--suggest")

    with (
        mock.patch(
            "documents.management.commands.document_retagger.load_classifier",
            return_value=None,
        ),
        mock.patch("documents.workflows.runner.run_workflows") as workflows,
        mock.patch("documents.signals.document_updated.send") as notifications,
    ):
        call_command("document_retagger", *args, skip_checks=True)

    document.refresh_from_db()
    if suggest:
        assert document.correspondent is None
        assert not document.tags.exists()
        assert document.modified == modified
        assert document.source_path == original
    else:
        assert document.correspondent == correspondent
        assert list(document.tags.all()) == [tag]
        assert document.filename == "Vendor/invoice.pdf"
        assert not original.exists()
    assert document.source_path.read_bytes() == b"keep the original"
    workflows.assert_not_called()
    notifications.assert_not_called()


def test_renamer_invalidates_suggestions_without_update_workflows(
    paperless_dirs: PaperlessDirs,
    settings: Settings,
) -> None:
    """An explicit rename retains the cache invalidation of the former save signal."""
    from django.core.cache import cache

    settings.FILENAME_FORMAT = "{title}"
    document = DocumentFactory(title="renamed", mime_type="application/pdf")
    original = paperless_dirs.originals_dir / "original.pdf"
    original.write_bytes(b"keep the original")
    Document.objects.filter(pk=document.pk).update(filename=original.name)
    cache.set(get_suggestion_cache_key(document.pk), "cached suggestions")

    with (
        mock.patch("documents.workflows.runner.run_workflows") as workflows,
        mock.patch("documents.signals.document_updated.send") as notifications,
    ):
        call_command("document_renamer", skip_checks=True)

    document.refresh_from_db()
    assert document.filename == "renamed.pdf"
    assert document.source_path.read_bytes() == b"keep the original"
    assert not original.exists()
    assert cache.get(get_suggestion_cache_key(document.pk)) is None
    workflows.assert_not_called()
    notifications.assert_not_called()


def test_notes_update_index_without_document_update_workflows(
    admin_client: APIClient,
    admin_user: User,
    paperless_dirs: PaperlessDirs,
) -> None:
    """Adding and deleting a note indexes each final state without metadata workflows."""
    document = DocumentFactory(owner=admin_user)
    indexed_notes = []

    def capture_index(instance: Document) -> None:
        indexed_notes.append(
            list(Note.objects.filter(document=instance).values_list("note", flat=True))
        )

    with (
        mock.patch("documents.search.get_backend") as backend,
        mock.patch("documents.workflows.runner.run_workflows") as workflows,
        mock.patch("documents.signals.document_updated.send") as notifications,
    ):
        backend.return_value.add_or_update.side_effect = capture_index
        response = admin_client.post(
            f"/api/documents/{document.pk}/notes/",
            {"note": "needle"},
            format="json",
        )
        assert response.status_code == 200
        note = Note.objects.get(document=document)
        response = admin_client.delete(
            f"/api/documents/{document.pk}/notes/?id={note.pk}"
        )
        assert response.status_code == 200

    assert indexed_notes == [["needle"], []]
    workflows.assert_not_called()
    notifications.assert_not_called()


@pytest.mark.parametrize(
    "option", ["--correspondent", "--document_type", "--tags", "--storage_path"]
)
def test_retagger_without_matches_does_not_reconcile_files(
    paperless_dirs: PaperlessDirs,
    settings: Settings,
    option: str,
) -> None:
    """An apply pass with no assignments must leave filenames and timestamps alone."""
    settings.FILENAME_FORMAT = "{title}"
    document = DocumentFactory(title="invoice", mime_type="application/pdf")
    original = paperless_dirs.originals_dir / "unchanged.pdf"
    original.write_bytes(b"keep the original")
    Document.objects.filter(pk=document.pk).update(filename=original.name)
    document.refresh_from_db()
    modified = document.modified

    with mock.patch(
        "documents.management.commands.document_retagger.load_classifier",
        return_value=None,
    ):
        call_command("document_retagger", option, skip_checks=True)

    document.refresh_from_db()
    assert document.modified == modified
    assert document.source_path == original
    assert original.read_bytes() == b"keep the original"
    assert not (paperless_dirs.originals_dir / "invoice.pdf").exists()


def test_retagger_removing_only_tags_reconciles_files(
    paperless_dirs: PaperlessDirs,
    settings: Settings,
) -> None:
    """Removal without a replacement tag still owns the resulting filename."""
    from documents.signals.handlers import set_tags

    settings.FILENAME_FORMAT = "{title} {tag_list}"
    document = DocumentFactory(title="invoice", mime_type="application/pdf")
    tag = TagFactory(
        name="old", match="unmatched", matching_algorithm=MatchingModel.MATCH_LITERAL
    )
    document.tags.add(tag)
    original = paperless_dirs.originals_dir / "invoice old.pdf"
    original.write_bytes(b"original")
    Document.objects.filter(pk=document.pk).update(filename=original.name)
    document.refresh_from_db()

    with mock.patch("documents.matching.match_tags", return_value=[]):
        added, removed = set_tags(None, document, replace=True)

    assert not added
    assert removed == {tag}
    document.refresh_from_db()
    assert not document.tags.exists()
    assert document.filename != original.name
    assert document.source_path.read_bytes() == b"original"
    assert not original.exists()
