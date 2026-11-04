"""Bulk metadata changes retain synchronous writes and deferred workflows."""

from __future__ import annotations

import shutil
from typing import TYPE_CHECKING
from unittest import mock

import pytest

from documents import bulk_edit
from documents.models import CustomField
from documents.models import CustomFieldInstance
from documents.models import Document
from documents.models import PaperlessTask
from documents.models import Workflow
from documents.models import WorkflowAction
from documents.models import WorkflowRun
from documents.models import WorkflowTrigger
from documents.search import get_backend
from documents.tasks import bulk_update_documents
from paperless_testing.factories import DocumentFactory
from paperless_testing.factories import StoragePathFactory

if TYPE_CHECKING:
    from paperless_testing.dirs import PaperlessDirs

pytestmark = pytest.mark.django_db


def test_bulk_custom_fields_move_once_using_all_new_values(
    paperless_dirs: PaperlessDirs,
) -> None:
    """A filename must never reflect only part of a multi-field update."""
    first = CustomField.objects.create(
        name="first",
        data_type=CustomField.FieldDataType.STRING,
    )
    second = CustomField.objects.create(
        name="second",
        data_type=CustomField.FieldDataType.STRING,
    )
    storage = StoragePathFactory(
        path=(
            "{{ custom_fields|get_cf_value('first') }}-"
            "{{ custom_fields|get_cf_value('second') }}"
        ),
    )
    document = DocumentFactory(storage_path=storage, mime_type="application/pdf")
    CustomFieldInstance.objects.create(
        document=document,
        field=first,
        value_text="oldfirst",
    )
    CustomFieldInstance.objects.create(
        document=document,
        field=second,
        value_text="oldsecond",
    )
    original = paperless_dirs.originals_dir / "oldfirst-oldsecond.pdf"
    original.write_bytes(b"preserve document bytes")
    Document.objects.filter(pk=document.pk).update(filename=original.name)
    destination = paperless_dirs.originals_dir / "newfirst-newsecond.pdf"

    with (
        mock.patch.object(shutil, "move", wraps=shutil.move) as move,
        mock.patch("documents.bulk_edit.bulk_update_documents.apply_async") as enqueue,
    ):
        bulk_edit.modify_custom_fields(
            [document.pk],
            add_custom_fields={first.pk: "newfirst", second.pk: "newsecond"},
            remove_custom_fields=[],
        )

    move.assert_called_once_with(original, destination)
    enqueue.assert_called_once_with(
        kwargs={"document_ids": [document.pk]},
        headers={"trigger_source": PaperlessTask.TriggerSource.SYSTEM},
    )
    document.refresh_from_db()
    assert document.filename == destination.name
    assert document.source_path.read_bytes() == b"preserve document bytes"
    assert not original.exists()


def test_bulk_custom_fields_defer_workflows_and_index_final_state(
    paperless_dirs: PaperlessDirs,
) -> None:
    """Metadata is visible immediately; queued completion runs workflows once."""
    field = CustomField.objects.create(
        name="reference",
        data_type=CustomField.FieldDataType.STRING,
    )
    document = DocumentFactory(title="before", content="bulkboundaryneedle")
    trigger = WorkflowTrigger.objects.create(
        type=WorkflowTrigger.WorkflowTriggerType.DOCUMENT_UPDATED,
    )
    action = WorkflowAction.objects.create(assign_title="workflowtitle")
    workflow = Workflow.objects.create(name="After bulk update")
    workflow.triggers.add(trigger)
    workflow.actions.add(action)
    backend = get_backend()
    backend.add_or_update(document)

    with mock.patch(
        "documents.bulk_edit.bulk_update_documents.apply_async",
    ) as enqueue:
        result = bulk_edit.modify_custom_fields(
            [document.pk],
            add_custom_fields={field.pk: "updated"},
            remove_custom_fields=[],
        )

    assert result == "OK"
    enqueue.assert_called_once()
    document.refresh_from_db()
    assert document.title == "before"
    assert document.custom_fields.get(field=field).value == "updated"
    assert not WorkflowRun.objects.filter(document=document).exists()
    assert list(backend.search_ids("title:before", user=None)) == [document.pk]

    bulk_update_documents(**enqueue.call_args.kwargs["kwargs"])

    document.refresh_from_db()
    assert document.title == "workflowtitle"
    assert WorkflowRun.objects.filter(document=document, workflow=workflow).count() == 1
    assert list(backend.search_ids("title:workflowtitle", user=None)) == [document.pk]
    assert list(backend.search_ids("title:before", user=None)) == []


def test_bulk_doclink_replacement_updates_old_and_new_reciprocal_links(
    paperless_dirs: PaperlessDirs,
) -> None:
    """Replacing a link must remove its former target's reverse relationship."""
    field = CustomField.objects.create(
        name="related",
        data_type=CustomField.FieldDataType.DOCUMENTLINK,
    )
    document = DocumentFactory()
    previous_target = DocumentFactory()
    new_target = DocumentFactory()
    CustomFieldInstance.objects.create(
        document=document,
        field=field,
        value_document_ids=[previous_target.pk],
    )
    CustomFieldInstance.objects.create(
        document=previous_target,
        field=field,
        value_document_ids=[document.pk],
    )

    with mock.patch("documents.bulk_edit.bulk_update_documents.apply_async") as enqueue:
        bulk_edit.modify_custom_fields(
            [document.pk],
            add_custom_fields={field.pk: [new_target.pk]},
            remove_custom_fields=[],
        )

    enqueue.assert_called_once()
    assert document.custom_fields.get(field=field).value == [new_target.pk]
    assert previous_target.custom_fields.get(field=field).value == []
    assert new_target.custom_fields.get(field=field).value == [document.pk]


def test_bulk_doclink_removal_follows_all_additions_across_documents(
    paperless_dirs: PaperlessDirs,
) -> None:
    """Later reciprocal additions must not recreate a field already removed."""
    field = CustomField.objects.create(
        name="related",
        data_type=CustomField.FieldDataType.DOCUMENTLINK,
    )
    first = DocumentFactory()
    second = DocumentFactory()
    CustomFieldInstance.objects.create(
        document=first,
        field=field,
        value_document_ids=[second.pk],
    )
    CustomFieldInstance.objects.create(
        document=second,
        field=field,
        value_document_ids=[first.pk],
    )

    with mock.patch("documents.bulk_edit.bulk_update_documents.apply_async") as enqueue:
        bulk_edit.modify_custom_fields(
            [first.pk, second.pk],
            add_custom_fields={field.pk: [first.pk]},
            remove_custom_fields=[field.pk],
        )

    enqueue.assert_called_once()
    assert not CustomFieldInstance.global_objects.filter(
        document_id__in=[first.pk, second.pk],
        field=field,
    ).exists()


def test_reciprocal_link_removal_reconciles_target_without_legacy_receiver(
    paperless_dirs: PaperlessDirs,
) -> None:
    """Core link removal owns the target's file change even outside a bulk batch."""
    from documents.updates import remove_doclink

    field = CustomField.objects.create(
        name="related",
        data_type=CustomField.FieldDataType.DOCUMENTLINK,
    )
    storage = StoragePathFactory(
        path="{{ custom_fields|get_cf_value('related')|length }}/{{ title }}",
    )
    source = DocumentFactory()
    target = DocumentFactory(
        title="target",
        storage_path=storage,
        mime_type="application/pdf",
    )
    CustomFieldInstance.objects.create(
        document=target,
        field=field,
        value_document_ids=[source.pk],
    )
    original = paperless_dirs.originals_dir / "1/target.pdf"
    original.parent.mkdir()
    original.write_bytes(b"keep the target")
    Document.objects.filter(pk=target.pk).update(filename="1/target.pdf")

    remove_doclink(source, field, target.pk)

    target.refresh_from_db()
    assert target.custom_fields.get(field=field).value == []
    assert target.filename == "0/target.pdf"
    assert target.source_path.read_bytes() == b"keep the target"
    assert not original.exists()


@pytest.mark.parametrize("workflow_failure", [False, True])
def test_bulk_completion_reconciles_after_workflows_before_batched_indexes(
    paperless_dirs: PaperlessDirs,
    settings,
    *,
    workflow_failure: bool,
) -> None:
    """Bulk writes survive workflow failure; later stages observe final filenames."""
    from documents.file_handling import reconcile_document_files
    from documents.signals import document_updated
    from documents.updates import complete_document_updates

    settings.AI_ENABLED = True
    settings.LLM_EMBEDDING_BACKEND = "huggingface"
    document = DocumentFactory(title="before", mime_type="application/pdf")
    original = paperless_dirs.originals_dir / "before.pdf"
    original.write_bytes(b"original")
    Document.objects.filter(pk=document.pk).update(filename=original.name)
    storage = StoragePathFactory(path="after/{{ title }}")
    with mock.patch("documents.bulk_edit.bulk_update_documents.apply_async"):
        bulk_edit.set_storage_path([document.pk], storage.pk)
    document.refresh_from_db()
    assert document.storage_path == storage
    assert document.filename == "before.pdf"
    events = []

    def workflow(**kwargs) -> None:
        """Record the filename visible when completion starts."""
        events.append(("workflow", kwargs["document"].filename))
        if workflow_failure:
            raise RuntimeError("workflow failed")
        Document.objects.filter(pk=document.pk).update(title="final")

    def reconcile(instance: Document) -> None:
        """Run real file reconciliation at the observed boundary."""
        reconcile_document_files(instance)
        events.append(("files", instance.filename))

    def notification(**kwargs) -> None:
        """Observe filenames delivered by the notification event."""
        events.append(("notification", kwargs["document"].filename))
        assert kwargs["skip_ai_index"] is True

    with (
        mock.patch("documents.workflows.runner.run_workflows", side_effect=workflow),
        mock.patch(
            "documents.file_handling.reconcile_document_files", side_effect=reconcile
        ),
        mock.patch.object(document_updated, "send", side_effect=notification),
        mock.patch("documents.search.get_backend") as backend,
        mock.patch("paperless_ai.indexing.update_llm_index") as ai,
    ):
        batch = backend.return_value.batch_update.return_value.__enter__.return_value
        batch.add_or_update_ids.side_effect = lambda ids: events.append(("search", ids))
        ai.side_effect = lambda **kwargs: events.append(("ai", kwargs["document_ids"]))
        if workflow_failure:
            with pytest.raises(RuntimeError, match="workflow failed"):
                complete_document_updates([document.pk])
        else:
            complete_document_updates([document.pk])

    document.refresh_from_db()
    assert document.storage_path == storage
    assert events == (
        [("workflow", "before.pdf")]
        if workflow_failure
        else [
            ("workflow", "before.pdf"),
            ("files", "after/final.pdf"),
            ("notification", "after/final.pdf"),
            ("search", [document.pk]),
            ("ai", [document.pk]),
        ]
    )
    assert document.source_path.read_bytes() == b"original"


def test_reciprocal_link_addition_reconciles_both_documents(
    paperless_dirs: PaperlessDirs,
) -> None:
    """Creating links reconciles each target after all reciprocal writes."""
    from documents.updates import save_custom_field_value

    field = CustomField.objects.create(
        name="related",
        data_type=CustomField.FieldDataType.DOCUMENTLINK,
    )
    storage = StoragePathFactory(
        path="{{ custom_fields|get_cf_value('related', [])|length }}/{{ title }}",
    )
    source = DocumentFactory(
        title="source", storage_path=storage, mime_type="application/pdf"
    )
    target = DocumentFactory(
        title="target", storage_path=storage, mime_type="application/pdf"
    )
    for document in (source, target):
        path = paperless_dirs.originals_dir / f"{document.title}.pdf"
        path.write_bytes(document.title.encode())
        Document.objects.filter(pk=document.pk).update(filename=path.name)
        document.refresh_from_db()

    save_custom_field_value(source, field, [target.pk])

    for document in (source, target):
        document.refresh_from_db()
        assert document.filename == f"1/{document.title}.pdf"
        assert document.source_path.read_bytes() == document.title.encode()
