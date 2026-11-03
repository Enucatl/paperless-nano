"""Document metadata persistence and the ordered document-update operation."""

from __future__ import annotations

import logging
from contextlib import contextmanager
from contextlib import nullcontext
from contextvars import ContextVar
from typing import TYPE_CHECKING
from typing import Any

from django.conf import settings
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
_deferred_file_updates: ContextVar[frozenset[int]] = ContextVar(
    "deferred_document_file_updates",
    default=frozenset(),
)


def file_updates_deferred(document_id: int) -> bool:
    """Tell legacy receivers whether an explicit operation owns this document."""
    return document_id in _deferred_file_updates.get()


@contextmanager
def document_metadata_update(document: Document) -> Iterator[None]:
    """Persist related changes before reconciling filenames, without recursive workflows.

    The scope is per document and execution context; other requests and linked
    documents retain their own file handling. No database transaction is added:
    rollback cannot undo a published file move.
    """
    token = _deferred_file_updates.set(_deferred_file_updates.get() | {document.pk})
    try:
        yield
    finally:
        _deferred_file_updates.reset(token)

    if not file_updates_deferred(document.pk):
        from documents.signals.handlers import update_filename_and_move_files

        update_filename_and_move_files(Document, document)


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
    from documents.signals.handlers import run_workflows

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
    from documents import bulk_edit

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
                        bulk_edit.remove_doclink(
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
    from documents import bulk_edit

    if field.data_type == CustomField.FieldDataType.DOCUMENTLINK:
        bulk_edit.reflect_doclinks(document, field, value)
    instance, _created = CustomFieldInstance.objects.update_or_create(
        document=document,
        field=field,
        defaults={CustomFieldInstance.get_value_field_name(field.data_type): value},
    )
    return instance
