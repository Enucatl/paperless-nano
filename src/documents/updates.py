"""Document metadata persistence and the ordered document-update operation."""

from __future__ import annotations

import logging
import uuid
from contextlib import contextmanager
from contextlib import nullcontext
from contextvars import ContextVar
from typing import TYPE_CHECKING
from typing import Any

from django.conf import settings
from django.db.models import Q
from django.utils import timezone
from django.utils.translation import gettext as _
from rest_framework.exceptions import PermissionDenied

from documents.models import CustomField
from documents.models import CustomFieldInstance
from documents.models import Document
from documents.models import Tag
from documents.models import WorkflowTrigger
from documents.permissions import set_permissions_for_objects

if TYPE_CHECKING:
    from collections.abc import Iterator
    from uuid import UUID

    from django.contrib.auth.models import User

logger = logging.getLogger("paperless.updates")
_metadata_update_documents: ContextVar[frozenset[int]] = ContextVar(
    "document_metadata_update_documents",
    default=frozenset(),
)


@contextmanager
def document_metadata_update(*documents: Document) -> Iterator[None]:
    """Persist related changes before reconciling filenames, without recursive workflows.

    The scope is per document and execution context; other requests and linked
    documents retain their own file handling. No database transaction is added:
    rollback cannot undo a published file move.
    """
    token = _metadata_update_documents.set(
        _metadata_update_documents.get() | {document.pk for document in documents},
    )
    try:
        yield
    finally:
        _metadata_update_documents.reset(token)

    from documents.file_handling import reconcile_document_files

    for document in documents:
        if document.pk not in _metadata_update_documents.get():
            reconcile_document_files(document)


@contextmanager
def document_update(
    document: Document,
    *,
    sender: object = None,
    logging_group: UUID | None = None,
    skip_ai_index: bool = False,
    index: bool = True,
    reconcile_files: bool = False,
) -> Iterator[None]:
    """Apply an update, then workflows, notifications and the final search record.

    Callers validate input and persist it inside the scope. API callers also
    capture their response there, preserving the pre-workflow response contract.
    Bulk callers enter after their synchronous queryset edit, request file
    reconciliation, and retain batched search/AI updates. Other callers reconcile
    through metadata persistence. A failed write does not run follow-up work.
    """
    from documents.search import get_backend
    from documents.signals import document_updated
    from documents.workflows.runner import run_workflows

    yield

    # Version content and relation prefetches on the caller may predate the edit.
    from documents.versioning import annotate_effective_content

    document = annotate_effective_content(Document.objects.all()).get(pk=document.pk)
    run_workflows(
        trigger_type=WorkflowTrigger.WorkflowTriggerType.DOCUMENT_UPDATED,
        document=document,
        logging_group=logging_group,
    )
    if reconcile_files:
        # Queryset writes bypass metadata persistence and its file reconciliation.
        with document_metadata_update(document):
            pass
    document_updated.send(
        sender=sender,
        document=document,
        logging_group=logging_group,
        skip_ai_index=skip_ai_index,
    )
    if index:
        backend = get_backend()
        final_doc = Document.objects.filter(pk=document.pk).first()
        if final_doc is None:
            backend.remove(document.pk)
        else:
            backend.add_or_update(final_doc)


def update_document_version_label(
    root_document: Document,
    version: Document,
    label: str | None,
    *,
    user: User | None,
    sender: object = None,
) -> None:
    """Save an authorized version's validated label and complete the root update.

    The caller checks root access and version membership before invoking this
    operation. Mutation and audit precede workflows, notification, and indexing.
    """
    with document_update(root_document, sender=sender):
        old_label = version.version_label
        version.version_label = label
        with document_metadata_update(version):
            version.save(update_fields=["version_label"])
        root_document.modified = timezone.now()
        Document.objects.filter(pk=root_document.pk).update(
            modified=root_document.modified,
        )
        if settings.AUDIT_LOG_ENABLED and old_label != version.version_label:
            from auditlog.models import LogEntry

            LogEntry.objects.log_create(
                instance=root_document,
                changes={"Version Label": [old_label, version.version_label]},
                action=LogEntry.Action.UPDATE,
                actor=user if user and user.is_authenticated else None,
                additional_data={
                    "reason": "Version label updated",
                    "version_id": version.id,
                },
            )


def delete_document_version(
    root_document: Document,
    version: Document,
    *,
    user: User | None,
    sender: object = None,
) -> int:
    """Delete an authorized non-root version and complete its root update.

    The caller checks root access, version membership, and root protection.
    Return the current version ID selected before completion workflows run.
    """
    from documents.search import get_backend
    from documents.versioning import versions_newest_first

    with document_update(root_document, sender=sender):
        get_backend().remove(version.pk)
        version_id = version.id
        version.delete()
        root_document.modified = timezone.now()
        Document.objects.filter(pk=root_document.pk).update(
            modified=root_document.modified,
        )
        if settings.AUDIT_LOG_ENABLED:
            from auditlog.models import LogEntry

            LogEntry.objects.log_create(
                instance=root_document,
                changes={"Version Deleted": ["None", version_id]},
                action=LogEntry.Action.UPDATE,
                actor=user if user and user.is_authenticated else None,
                additional_data={
                    "reason": "Version deleted",
                    "version_id": version_id,
                },
            )
        current = versions_newest_first(
            Document.objects.filter(
                Q(id=root_document.id) | Q(root_document=root_document),
            ),
        ).first()
        current_version_id = current.id if current else root_document.id
    return current_version_id


def save_document_metadata(
    instance: Document,
    validated_data: dict[str, Any],
    *,
    user: User | None = None,
) -> Document:
    """Apply validated metadata, permissions and related fields as one file update.

    Field/schema validation stays with the input adapter. Permission changes are
    checked here before any mutation, including reciprocal document links.
    Filename columns are never copied from a possibly stale caller instance.
    """
    if (
        ("owner" in validated_data and validated_data["owner"] != instance.owner)
        or "set_permissions" in validated_data
    ) and not (
        instance.owner is None
        or (user is not None and (user.is_superuser or instance.owner == user))
    ):
        raise PermissionDenied(_("Insufficient permissions."))

    validated_data = validated_data.copy()
    actor = nullcontext()
    if settings.AUDIT_LOG_ENABLED:
        from auditlog.context import set_actor

        actor = set_actor(user)
    with actor, document_metadata_update(instance):
        if "created_date" in validated_data:
            if "created" not in validated_data:
                validated_data["created"] = validated_data["created_date"]
            logger.warning(
                "created_date is deprecated, use created instead",
            )
            validated_data.pop("created_date")
        if instance.custom_fields.count() > 0 and "custom_fields" in validated_data:
            incoming_custom_fields = [
                field["field"] for field in validated_data["custom_fields"]
            ]
            for custom_field_instance in instance.custom_fields.filter(
                field__data_type=CustomField.FieldDataType.DOCUMENTLINK,
            ):
                if (
                    custom_field_instance.field not in incoming_custom_fields
                    and custom_field_instance.value is not None
                ):
                    # Doc link field is being removed entirely
                    for doc_id in custom_field_instance.value:
                        remove_doclink(
                            instance,
                            custom_field_instance.field,
                            doc_id,
                        )
        if "tags" in validated_data:
            # Respect tag hierarchy on updates:
            # - Adding a child adds its ancestors
            # - Removing a parent removes all its descendants
            prev_tags = set(instance.tags.all())
            requested_tags = set(validated_data["tags"])

            # Tags newly added in this update and the ancestors they require
            added_tags = requested_tags - prev_tags
            required_by_add_tags = set(added_tags)
            for t in added_tags:
                required_by_add_tags.update(t.get_ancestors())

            # Tags being removed in this update and all descendants, except
            # those required by a tag that is being added in this same update
            removed_tags = prev_tags - requested_tags
            blocked_tags = set(removed_tags)
            for t in removed_tags:
                blocked_tags.update(t.get_descendants())
            blocked_tags.difference_update(required_by_add_tags)

            # Add all parent tags
            final_tags = set(requested_tags)
            for t in requested_tags:
                final_tags.update(t.get_ancestors())

            # Drop removed parents and their descendants
            final_tags.difference_update(blocked_tags)

            validated_data["tags"] = list(final_tags)
        if validated_data.get("remove_inbox_tags"):
            current_tag_ids = {t.pk for t in instance.tags.all()}
            tags = (
                validated_data["tags"]
                if "tags" in validated_data
                else list(instance.tags.all())
            )

            # Tags newly added in this update, plus their ancestors, are kept
            keep_ids: set[int] = set()
            for tag in tags:
                if tag.pk not in current_tag_ids:
                    keep_ids.add(tag.pk)
                    keep_ids.update(int(pk) for pk in tag.get_ancestors_pks())

            # Remove inbox tags and their descendants, except those being kept
            remove_ids: set[int] = set()
            for inbox_tag in (
                Tag.objects.filter(is_inbox_tag=True)
                .exclude(pk__in=keep_ids)
                .only("pk", "tn_descendants_pks")
            ):
                remove_ids.add(inbox_tag.pk)
                remove_ids.update(int(pk) for pk in inbox_tag.get_descendants_pks())

            validated_data["tags"] = [t for t in tags if t.pk not in remove_ids]

        permissions = validated_data.pop("set_permissions", None)
        tags = validated_data.pop("tags", None)
        custom_fields = validated_data.pop("custom_fields", None)
        validated_data.pop("remove_inbox_tags", None)
        if permissions is not None:
            set_permissions_for_objects(permissions, Document, [instance.pk])

        update_fields = []
        for name, value in validated_data.items():
            if name not in {"filename", "archive_filename"}:
                setattr(instance, name, value)
                update_fields.append(name)
        if "modified" not in update_fields:
            update_fields.append("modified")
        instance.save(update_fields=update_fields)
        if tags is not None:
            instance.tags.set(tags)
        if custom_fields is not None:
            kept_ids = []
            for field_data in custom_fields:
                field_instance = save_custom_field_value(
                    instance,
                    field_data["field"],
                    field_data["value"],
                )
                kept_ids.append(field_instance.pk)
            instance.custom_fields.exclude(pk__in=kept_ids).delete()
        CustomFieldInstance.deleted_objects.filter(document=instance).delete()
        instance.refresh_from_db()
    return instance


def save_custom_field_value(
    document: Document,
    field: CustomField,
    value: Any,
) -> CustomFieldInstance:
    """Store a validated field value and maintain reciprocal document links."""
    from documents.file_handling import filename_uses_custom_fields

    with (
        document_metadata_update(document)
        if filename_uses_custom_fields(document)
        else nullcontext()
    ):
        if field.data_type == CustomField.FieldDataType.DOCUMENTLINK:
            reflect_doclinks(document, field, value)
        instance, _created = CustomFieldInstance.objects.update_or_create(
            document=document,
            field=field,
            defaults={CustomFieldInstance.get_value_field_name(field.data_type): value},
        )
    return instance


def complete_document_updates(document_ids: list[int]) -> None:
    """Complete persisted bulk edits with workflows and batched search updates.

    The Celery task is the execution adapter; this operation keeps the existing
    cache invalidation, file reconciliation and one AI batch update policy.
    """
    from documents.caching import clear_document_caches
    from documents.search import get_backend
    from documents.versioning import annotate_effective_content
    from paperless.config import AIConfig

    document_ids = list(document_ids)
    # Workflows match effective version content; the batch index reads the
    # final state again after all workflows have run.
    documents = annotate_effective_content(
        Document.objects.filter(id__in=document_ids),
    )

    for doc in documents:
        clear_document_caches(doc.pk)
        with document_update(
            doc,
            logging_group=uuid.uuid4(),
            skip_ai_index=True,  # bulk path calls update_llm_index once below
            index=False,
            reconcile_files=True,
        ):
            pass

    with get_backend().batch_update() as batch:
        batch.add_or_update_ids(document_ids)

    ai_config = AIConfig()
    if ai_config.llm_index_enabled:
        from paperless_ai.indexing import update_llm_index

        update_llm_index(
            rebuild=False,
            document_ids=document_ids,
        )


def modify_document_custom_fields(
    doc_ids: list[int],
    add_custom_fields: list[int] | dict,
    remove_custom_fields: list[int],
) -> list[int]:
    """Apply bulk custom-field changes before reconciling each affected file.

    Inputs have already passed API validation. Keep additions before removals,
    including reciprocal links between documents in the same batch. Return the
    affected IDs for asynchronous workflow completion by the caller.
    """
    from documents.file_handling import filename_uses_custom_fields

    qs = Document.objects.filter(id__in=doc_ids).only("pk")
    affected_docs = list(qs.values_list("pk", flat=True))
    # Ensure add_custom_fields is a list of (int, value) tuples, supports old API
    add_custom_fields = (
        [(int(field), value) for field, value in add_custom_fields.items()]
        if isinstance(add_custom_fields, dict)
        else [(int(field), None) for field in add_custom_fields]
    )

    # Resolved once, instead of re-querying the same field for every document
    custom_fields_by_id: dict[int, CustomField] = CustomField.objects.in_bulk(
        [field_id for field_id, _ in add_custom_fields],
    )
    docs_by_id = Document.objects.defer("content").in_bulk(affected_docs)
    with document_metadata_update(
        *(doc for doc in docs_by_id.values() if filename_uses_custom_fields(doc)),
    ):
        for field_id, value in add_custom_fields:
            custom_field = custom_fields_by_id[field_id]
            is_doclink = (
                custom_field.data_type == CustomField.FieldDataType.DOCUMENTLINK
            )
            for doc_id in affected_docs:
                if is_doclink and value and doc_id in value:
                    # Prevent self-linking
                    continue
                save_custom_field_value(docs_by_id[doc_id], custom_field, value)

        # For doc link fields that are being removed, remove symmetrical links.
        # select_related avoids a per-instance reload of the document and field.
        for doclink_being_removed_instance in CustomFieldInstance.objects.filter(
            document_id__in=affected_docs,
            field__id__in=remove_custom_fields,
            field__data_type=CustomField.FieldDataType.DOCUMENTLINK,
            value_document_ids__isnull=False,
        ).select_related("field", "document"):
            for target_doc_id in doclink_being_removed_instance.value:
                remove_doclink(
                    document=doclink_being_removed_instance.document,
                    field=doclink_being_removed_instance.field,
                    target_doc_id=target_doc_id,
                )

        # Finally, remove the custom fields
        CustomFieldInstance.objects.filter(
            document_id__in=affected_docs,
            field_id__in=remove_custom_fields,
        ).hard_delete()

    return affected_docs


def reflect_doclinks(
    document: Document,
    field: CustomField,
    target_doc_ids: list[int],
) -> None:
    """
    Add or remove 'symmetrical' links to `document` on all `target_doc_ids`
    """

    if target_doc_ids is None:
        target_doc_ids = []

    # Check if any documents are going to be removed from the current list of links and remove the symmetrical links
    current_field_instance = CustomFieldInstance.objects.filter(
        field=field,
        document=document,
    ).first()
    if current_field_instance is not None and current_field_instance.value is not None:
        for doc_id in current_field_instance.value:
            if doc_id not in target_doc_ids:
                remove_doclink(
                    document=document,
                    field=field,
                    target_doc_id=doc_id,
                )

    # Create an instance if target doc doesn't have this field or append it to an existing one
    existing_custom_field_instances = {
        custom_field.document_id: custom_field
        for custom_field in CustomFieldInstance.objects.filter(
            field=field,
            document_id__in=target_doc_ids,
        )
    }
    custom_field_instances_to_create = []
    custom_field_instances_to_update = []
    for target_doc_id in target_doc_ids:
        target_doc_field_instance = existing_custom_field_instances.get(
            target_doc_id,
        )
        if target_doc_field_instance is None:
            custom_field_instances_to_create.append(
                CustomFieldInstance(
                    document_id=target_doc_id,
                    field=field,
                    value_document_ids=[document.id],
                ),
            )
        elif target_doc_field_instance.value is None:
            target_doc_field_instance.value_document_ids = [document.id]
            custom_field_instances_to_update.append(target_doc_field_instance)
        elif document.id not in target_doc_field_instance.value:
            target_doc_field_instance.value_document_ids.append(document.id)
            custom_field_instances_to_update.append(target_doc_field_instance)

    from documents.file_handling import filename_uses_custom_fields

    targets = Document.objects.filter(pk__in=target_doc_ids)
    with document_metadata_update(
        *(target for target in targets if filename_uses_custom_fields(target)),
    ):
        CustomFieldInstance.objects.bulk_create(custom_field_instances_to_create)
        CustomFieldInstance.objects.bulk_update(
            custom_field_instances_to_update,
            ["value_document_ids"],
        )
        Document.objects.filter(id__in=target_doc_ids).update(modified=timezone.now())


def remove_doclink(
    document: Document,
    field: CustomField,
    target_doc_id: int,
) -> None:
    """
    Removes a 'symmetrical' link to `document` from the target document's existing custom field instance
    """
    # select_related: a signal receiver (auditlog) touches .document/.field on
    # the save() below, without this that is a per-call reload query
    target_doc_field_instance = (
        CustomFieldInstance.objects.filter(document_id=target_doc_id, field=field)
        .select_related("document", "field")
        .first()
    )
    if (
        target_doc_field_instance is not None
        and document.id in target_doc_field_instance.value
    ):
        from documents.file_handling import filename_uses_custom_fields

        target = target_doc_field_instance.document
        with (
            document_metadata_update(target)
            if filename_uses_custom_fields(target)
            else nullcontext()
        ):
            target_doc_field_instance.value.remove(document.id)
            target_doc_field_instance.save()
    Document.objects.filter(id=target_doc_id).update(modified=timezone.now())


def update_select_custom_field_documents(
    custom_field_id: int,
    select_options: list[dict[str, str]],
) -> None:
    """Clear removed select values and reconcile files that use their labels."""
    from documents.file_handling import filename_uses_custom_fields

    option_labels = {option["id"]: option["label"] for option in select_options}
    fields = CustomFieldInstance.objects.filter(field_id=custom_field_id)
    fields.exclude(value_select__in=option_labels.keys()).update(value_select=None)
    for field in fields.select_related("document").iterator():
        if filename_uses_custom_fields(field.document):
            with document_metadata_update(field.document):
                pass
