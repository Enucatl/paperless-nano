"""Behavior at the document-update operation boundary."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING
from unittest import mock

import pytest

from documents.models import CustomField
from documents.models import Document
from documents.models import MatchingModel
from documents.models import StoragePath
from documents.models import Workflow
from documents.models import WorkflowAction
from documents.models import WorkflowRun
from documents.models import WorkflowTrigger
from documents.search import get_backend
from documents.signals import document_updated
from documents.updates import document_metadata_update
from documents.updates import document_update
from documents.updates import file_updates_deferred
from documents.updates import save_document_metadata
from paperless_testing.factories import DocumentFactory
from paperless_testing.factories import TagFactory
from paperless_testing.factories import UserFactory
from paperless_testing.permissions import grant_global
from paperless_testing.permissions import grant_object

if TYPE_CHECKING:
    from django.contrib.auth.models import User
    from pytest_django.fixtures import Settings
    from rest_framework.test import APIClient

    from paperless_testing.dirs import PaperlessDirs

pytestmark = pytest.mark.django_db


@pytest.mark.parametrize("trash", [False, True])
def test_response_precedes_workflows_but_search_observes_their_result(
    admin_client: APIClient,
    admin_user: User,
    paperless_dirs: PaperlessDirs,
    *,
    trash: bool,
) -> None:
    """An update returns its snapshot even when the workflow subsequently trashes it."""
    document = DocumentFactory(owner=admin_user, content="boundaryneedle")
    trigger = WorkflowTrigger.objects.create(
        type=WorkflowTrigger.WorkflowTriggerType.DOCUMENT_UPDATED,
    )
    action = WorkflowAction.objects.create(
        type=(
            WorkflowAction.WorkflowActionType.MOVE_TO_TRASH
            if trash
            else WorkflowAction.WorkflowActionType.ASSIGNMENT
        ),
        assign_title="workflowtitle",
    )
    workflow = Workflow.objects.create(name="After update")
    workflow.triggers.add(trigger)
    workflow.actions.add(action)
    backend = get_backend()
    backend.add_or_update(document)

    response = admin_client.patch(
        f"/api/documents/{document.pk}/",
        {"title": "requestedtitle"},
        format="json",
    )

    assert response.status_code == 200
    assert response.data["title"] == "requestedtitle"
    assert response.data["deleted_at"] is None
    assert WorkflowRun.global_objects.filter(document=document).count() == 1
    assert list(backend.search_ids("boundaryneedle", user=None)) == (
        [] if trash else [document.pk]
    )
    if not trash:
        document.refresh_from_db()
        assert document.title == "workflowtitle"
        assert list(backend.search_ids("title:workflowtitle", user=None)) == [
            document.pk,
        ]
        assert list(backend.search_ids("title:requestedtitle", user=None)) == []


def test_combined_update_persists_before_ordered_workflows_and_file_moves(
    admin_client: APIClient,
    admin_user: User,
    paperless_dirs: PaperlessDirs,
) -> None:
    """Tags and custom fields cannot discard pending scalar or workflow changes."""
    parent = TagFactory(name="parent")
    child = TagFactory(name="child", tn_parent=parent)
    workflow_tag = TagFactory(name="workflow")
    field = CustomField.objects.create(
        name="reference",
        data_type=CustomField.FieldDataType.STRING,
    )
    storage = StoragePath.objects.create(
        name="Reference folders",
        path="{{ custom_fields|get_cf_value('reference') }}/{{ title }}",
    )
    document = DocumentFactory(
        owner=admin_user,
        title="before",
        mime_type="application/pdf",
    )
    original = paperless_dirs.originals_dir / "before.pdf"
    original.write_bytes(b"preserve original bytes")
    Document.objects.filter(pk=document.pk).update(filename="before.pdf")

    first_trigger = WorkflowTrigger.objects.create(
        type=WorkflowTrigger.WorkflowTriggerType.DOCUMENT_UPDATED,
    )
    first_trigger.filter_has_tags.add(child)
    first_action = WorkflowAction.objects.create(assign_title="first", order=0)
    first_action.assign_tags.add(workflow_tag)
    first = Workflow.objects.create(name="First", order=0)
    first.triggers.add(first_trigger)
    first.actions.add(first_action)
    second_trigger = WorkflowTrigger.objects.create(
        type=WorkflowTrigger.WorkflowTriggerType.DOCUMENT_UPDATED,
    )
    second_trigger.filter_has_tags.add(workflow_tag)
    second_action = WorkflowAction.objects.create(assign_title="second")
    second = Workflow.objects.create(name="Second", order=1)
    second.triggers.add(second_trigger)
    second.actions.add(second_action)

    response = admin_client.patch(
        f"/api/documents/{document.pk}/",
        {
            "title": "requested",
            "storage_path": storage.pk,
            "tags": [child.pk],
            "custom_fields": [{"field": field.pk, "value": "invoice"}],
        },
        format="json",
    )

    assert response.status_code == 200
    assert response.data["title"] == "requested"
    assert set(response.data["tags"]) == {parent.pk, child.pk}
    document.refresh_from_db()
    assert document.title == "second"
    assert document.storage_path == storage
    assert set(document.tags.values_list("pk", flat=True)) == {
        parent.pk,
        child.pk,
        workflow_tag.pk,
    }
    assert document.custom_fields.get(field=field).value == "invoice"
    assert document.filename == "invoice/second.pdf"
    assert document.source_path.read_bytes() == b"preserve original bytes"
    assert not original.exists()
    assert list(
        WorkflowRun.objects.filter(document=document)
        .order_by("pk")
        .values_list("workflow_id", flat=True),
    ) == [first.pk, second.pk]


@pytest.mark.parametrize("change", ["owner", "set_permissions"])
def test_shared_editor_cannot_change_ownership_or_grants(
    rest_api_client: APIClient,
    paperless_dirs: PaperlessDirs,
    change: str,
) -> None:
    """Object change permission does not confer permission to change access."""
    owner = UserFactory()
    editor = UserFactory()
    grant_global(editor, "view_document", "change_document")
    document = DocumentFactory(owner=owner, title="before")
    grant_object(editor, document, "view_document", "change_document")
    rest_api_client.force_authenticate(editor)
    value = editor.pk if change == "owner" else {"view": {"users": []}}

    response = rest_api_client.patch(
        f"/api/documents/{document.pk}/",
        {"title": "forbidden", change: value},
        format="json",
    )

    assert response.status_code == 403
    document.refresh_from_db()
    assert document.owner == owner
    assert document.title == "before"
    assert editor.has_perm("view_document", document)
    assert not WorkflowRun.objects.filter(document=document).exists()


def test_file_move_failure_preserves_file_and_accepts_metadata_update(
    admin_client: APIClient,
    admin_user: User,
    paperless_dirs: PaperlessDirs,
    settings: Settings,
) -> None:
    """A failed rename retains the old file path without rejecting saved metadata."""
    settings.FILENAME_FORMAT = "{title}"
    document = DocumentFactory(
        owner=admin_user,
        title="before",
        mime_type="application/pdf",
        content="boundaryneedle",
    )
    original = paperless_dirs.originals_dir / "before.pdf"
    original.write_bytes(b"preserve original bytes")
    Document.objects.filter(pk=document.pk).update(filename="before.pdf")

    with mock.patch.object(
        shutil,
        "move",
        side_effect=PermissionError("denied"),
    ) as move:
        response = admin_client.patch(
            f"/api/documents/{document.pk}/",
            {"title": "after"},
            format="json",
        )

    assert response.status_code == 200
    assert response.data["title"] == "after"
    document.refresh_from_db()
    assert document.title == "after"
    assert document.filename == "before.pdf"
    move.assert_called_once()
    assert document.source_path.read_bytes() == b"preserve original bytes"
    assert not (paperless_dirs.originals_dir / "after.pdf").exists()
    assert list(get_backend().search_ids("title:after", user=None)) == [document.pk]


def test_metadata_scope_is_nested_document_local_and_restored_after_failure(
    paperless_dirs: PaperlessDirs,
    settings: Settings,
) -> None:
    """Deferring one document's files must neither defer another nor leak on failure."""
    settings.FILENAME_FORMAT = "{title}"
    first = DocumentFactory(title="first", mime_type="application/pdf")
    second = DocumentFactory(title="second", mime_type="application/pdf")
    for document in (first, second):
        filename = f"{document.title}.pdf"
        (paperless_dirs.originals_dir / filename).write_bytes(b"original")
        Document.objects.filter(pk=document.pk).update(filename=filename)
        document.refresh_from_db()

    with pytest.raises(RuntimeError, match="interrupted"):
        with document_metadata_update(first):
            with document_metadata_update(first):
                first.title = "changed-first"
                first.save(update_fields=["title"])
            assert file_updates_deferred(first.pk)
            assert first.source_path.is_file()
            second.title = "changed-second"
            second.save(update_fields=["title"])
            assert second.filename == "changed-second.pdf"
            assert second.source_path.read_bytes() == b"original"
            raise RuntimeError("interrupted")

    assert not file_updates_deferred(first.pk)
    save_document_metadata(first, {"title": "retry"})
    assert first.filename == "retry.pdf"
    assert first.source_path.read_bytes() == b"original"
    assert not (paperless_dirs.originals_dir / "first.pdf").exists()


def test_workflow_failure_stops_notifications_and_index_until_retry(
    paperless_dirs: PaperlessDirs,
) -> None:
    """Failed required work must not announce success or leave file deferral active."""
    document = DocumentFactory(title="before", content="boundaryneedle")
    backend = get_backend()
    backend.add_or_update(document)
    notification = mock.Mock()
    document_updated.connect(notification)
    try:
        with mock.patch(
            "documents.workflows.runner.run_workflows",
            side_effect=RuntimeError("workflow failed"),
        ):
            with pytest.raises(RuntimeError, match="workflow failed"):
                with document_update(document):
                    save_document_metadata(document, {"title": "after"})
        notification.assert_not_called()
        assert not file_updates_deferred(document.pk)
        document.refresh_from_db()
        assert document.title == "after"
        assert list(backend.search_ids("title:before", user=None)) == [document.pk]

        with mock.patch("documents.workflows.runner.run_workflows") as workflows:
            with document_update(document):
                pass
        workflows.assert_called_once()
        notification.assert_called_once()
        assert list(backend.search_ids("title:after", user=None)) == [document.pk]
    finally:
        document_updated.disconnect(notification)


def test_missing_custom_field_value_rejects_entire_update(
    admin_client: APIClient,
    admin_user: User,
    paperless_dirs: PaperlessDirs,
) -> None:
    """PATCH still requires a complete custom-field value before saving metadata."""
    document = DocumentFactory(owner=admin_user, title="before")
    field = CustomField.objects.create(
        name="reference",
        data_type=CustomField.FieldDataType.STRING,
    )

    response = admin_client.patch(
        f"/api/documents/{document.pk}/",
        {"title": "after", "custom_fields": [{"field": field.pk}]},
        format="json",
    )

    assert response.status_code == 400
    assert response.data == {
        "custom_fields": [{"value": ["This field is required."]}],
    }
    document.refresh_from_db()
    assert document.title == "before"
    assert not document.custom_fields.exists()
    assert not WorkflowRun.objects.filter(document=document).exists()


def test_workflows_match_new_latest_version_content_after_update(
    admin_client: APIClient,
    admin_user: User,
    paperless_dirs: PaperlessDirs,
) -> None:
    """Workflow matching must not reuse the request's stale content prefetch."""
    document = DocumentFactory(owner=admin_user, content="root content")
    version = DocumentFactory(root_document=document, content="old version content")
    trigger = WorkflowTrigger.objects.create(
        type=WorkflowTrigger.WorkflowTriggerType.DOCUMENT_UPDATED,
        matching_algorithm=MatchingModel.MATCH_LITERAL,
        match="newversionneedle",
    )
    action = WorkflowAction.objects.create(assign_title="matched new content")
    workflow = Workflow.objects.create(name="Match latest content")
    workflow.triggers.add(trigger)
    workflow.actions.add(action)

    response = admin_client.patch(
        f"/api/documents/{document.pk}/",
        {"content": "newversionneedle"},
        format="json",
    )

    assert response.status_code == 200
    assert response.data["content"] == "newversionneedle"
    document.refresh_from_db()
    version.refresh_from_db()
    assert document.content == "root content"
    assert version.content == "newversionneedle"
    assert document.title == "matched new content"
    assert WorkflowRun.objects.filter(document=document, workflow=workflow).count() == 1


@pytest.mark.parametrize("failure", ["notification", "search"])
def test_completion_failure_does_not_return_the_prepared_api_response(
    admin_client: APIClient,
    admin_user: User,
    paperless_dirs: PaperlessDirs,
    failure: str,
) -> None:
    """Saved metadata survives failure after workflows; HTTP success does not."""
    document = DocumentFactory(owner=admin_user, title="before")
    backend = get_backend()
    backend.add_or_update(document)
    with (
        mock.patch("documents.workflows.runner.run_workflows") as workflows,
        mock.patch.object(document_updated, "send") as notification,
        mock.patch.object(backend, "add_or_update") as index,
    ):
        failed_step = notification if failure == "notification" else index
        failed_step.side_effect = RuntimeError(f"{failure} failed")
        with pytest.raises(RuntimeError, match=f"{failure} failed"):
            admin_client.patch(
                f"/api/documents/{document.pk}/",
                {"title": "after"},
                format="json",
            )

    document.refresh_from_db()
    assert document.title == "after"
    workflows.assert_called_once()
    notification.assert_called_once()
    assert index.call_count == (failure == "search")
    assert list(backend.search_ids("title:before", user=None)) == [document.pk]


@pytest.mark.parametrize("operation", ["label", "delete"])
def test_version_mutation_survives_completion_failure(
    admin_client: APIClient,
    admin_user: User,
    paperless_dirs: PaperlessDirs,
    operation: str,
) -> None:
    """Version endpoints persist mutations before invoking root completion."""
    root = DocumentFactory(owner=admin_user)
    version = DocumentFactory(root_document=root, version_label="before")
    original_modified = root.modified
    with (
        mock.patch(
            "documents.workflows.runner.run_workflows",
            side_effect=RuntimeError("workflow failed"),
        ),
        mock.patch.object(document_updated, "send") as notification,
        pytest.raises(RuntimeError, match="workflow failed"),
    ):
        url = f"/api/documents/{root.pk}/versions/{version.pk}/"
        if operation == "label":
            admin_client.patch(url, {"version_label": "after"}, format="json")
        else:
            admin_client.delete(url)

    root.refresh_from_db()
    assert root.modified > original_modified
    notification.assert_not_called()
    if operation == "label":
        version.refresh_from_db()
        assert version.version_label == "after"
    else:
        assert not Document.objects.filter(pk=version.pk).exists()
