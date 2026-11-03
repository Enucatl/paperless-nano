from __future__ import annotations

import logging
import tempfile
import uuid
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Literal
from typing import NamedTuple

from celery import chord
from celery import group
from celery import shared_task
from django.conf import settings
from django.db import transaction
from django.db.models import Case
from django.db.models import F
from django.db.models import Max
from django.db.models import OuterRef
from django.db.models import Q
from django.db.models import Subquery
from django.db.models import When
from django.db.models.functions import Coalesce
from django.utils import timezone

from documents import pdf_ops
from documents.data_models import ConsumableDocument
from documents.data_models import DocumentMetadataOverrides
from documents.data_models import DocumentSource
from documents.models import Correspondent
from documents.models import Document
from documents.models import DocumentType
from documents.models import PaperlessTask
from documents.models import StoragePath
from documents.models import Tag
from documents.permissions import PermissionsSpec
from documents.permissions import set_permissions_for_objects
from documents.plugins.helpers import DocumentsStatusManager
from documents.tasks import bulk_update_documents
from documents.tasks import consume_file
from documents.tasks import remove_document_from_index
from documents.updates import modify_document_custom_fields
from documents.versioning import get_latest_version_for_root
from documents.versioning import get_root_document
from documents.versioning import versions_newest_first

if TYPE_CHECKING:
    from collections.abc import Mapping

    from django.contrib.auth.models import User

if settings.AUDIT_LOG_ENABLED:
    from auditlog.models import LogEntry

logger: logging.Logger = logging.getLogger("paperless.bulk_edit")

SourceMode = Literal["latest_version", "explicit_selection"]


class SourceModeChoices:
    LATEST_VERSION: SourceMode = "latest_version"
    EXPLICIT_SELECTION: SourceMode = "explicit_selection"


class ResolvedDocPair(NamedTuple):
    root_doc: Document
    source_doc: Document


@shared_task(bind=True)
def restore_archive_serial_numbers_task(
    self,
    backup: dict[int, int | None],
    *args,
    **kwargs,
) -> None:
    from documents.job_callbacks import run_callback

    run_callback(
        self.request.id,
        self.name,
        (backup, *args),
        kwargs,
        lambda: restore_archive_serial_numbers(backup),
    )


def release_archive_serial_numbers(
    doc_ids: list[int],
    *,
    expected_asns: dict[int, int | None] | None = None,
) -> dict[int, int | None]:
    """
    Clears ASNs on documents that are about to be replaced so new documents
    can be assigned ASNs without uniqueness collisions. Returns a backup map
    of doc_id -> previous ASN for potential restoration.
    """
    qs = Document.objects.filter(
        id__in=doc_ids,
        archive_serial_number__isnull=False,
    ).only("pk", "archive_serial_number")
    backup = dict(qs.values_list("pk", "archive_serial_number"))
    if expected_asns is None:
        qs.update(archive_serial_number=None)
    else:
        released = {}
        for doc_id, asn in backup.items():
            if expected_asns.get(doc_id) == asn and Document.objects.filter(
                pk=doc_id,
                archive_serial_number=asn,
            ).update(archive_serial_number=None):
                released[doc_id] = asn
        backup = released
    logger.info(f"Released archive serial numbers for documents {list(backup.keys())}")
    return backup


def restore_archive_serial_numbers(backup: dict[int, int | None]) -> None:
    """
    Restores ASNs using the provided backup map, intended for
    rollback when replacement consumption fails.
    """
    for doc_id, asn in backup.items():
        Document.objects.filter(
            pk=doc_id,
            archive_serial_number__isnull=True,
        ).update(archive_serial_number=asn)
    logger.info(f"Restored archive serial numbers for documents {list(backup.keys())}")


def _resolve_root_and_source_doc(
    doc: Document,
    *,
    source_mode: SourceMode = SourceModeChoices.LATEST_VERSION,
) -> ResolvedDocPair:
    root_doc = get_root_document(doc)

    if source_mode == SourceModeChoices.EXPLICIT_SELECTION:
        return ResolvedDocPair(root_doc=root_doc, source_doc=doc)

    # Version IDs are explicit by default, only a selected root resolves to latest
    if doc.root_document_id is not None:
        return ResolvedDocPair(root_doc=root_doc, source_doc=doc)

    return ResolvedDocPair(
        root_doc=root_doc,
        source_doc=get_latest_version_for_root(root_doc),
    )


def _scratch_path(name: str) -> Path:
    """A path inside a fresh directory under SCRATCH_DIR."""
    return Path(tempfile.mkdtemp(dir=settings.SCRATCH_DIR)) / name


def set_correspondent(
    doc_ids: list[int],
    correspondent: Correspondent,
) -> Literal["OK"]:
    if correspondent:
        correspondent = Correspondent.objects.only("pk").get(id=correspondent)

    qs = (
        Document.objects.filter(Q(id__in=doc_ids) & ~Q(correspondent=correspondent))
        .select_related("correspondent")
        .only("pk", "correspondent__id")
    )
    affected_docs = list(qs.values_list("pk", flat=True))
    qs.update(correspondent=correspondent)

    bulk_update_documents.apply_async(
        kwargs={"document_ids": affected_docs},
        headers={"trigger_source": PaperlessTask.TriggerSource.SYSTEM},
    )

    return "OK"


def set_storage_path(doc_ids: list[int], storage_path: StoragePath) -> Literal["OK"]:
    if storage_path:
        storage_path = StoragePath.objects.only("pk").get(id=storage_path)

    qs = (
        Document.objects.filter(
            Q(id__in=doc_ids) & ~Q(storage_path=storage_path),
        )
        .select_related("storage_path")
        .only("pk", "storage_path__id")
    )
    affected_docs = list(qs.values_list("pk", flat=True))
    qs.update(storage_path=storage_path)

    bulk_update_documents.apply_async(
        kwargs={"document_ids": affected_docs},
        headers={"trigger_source": PaperlessTask.TriggerSource.SYSTEM},
    )

    return "OK"


def set_document_type(doc_ids: list[int], document_type: DocumentType) -> Literal["OK"]:
    if document_type:
        document_type = DocumentType.objects.only("pk").get(id=document_type)

    qs = (
        Document.objects.filter(Q(id__in=doc_ids) & ~Q(document_type=document_type))
        .select_related("document_type")
        .only("pk", "document_type__id")
    )
    affected_docs = list(qs.values_list("pk", flat=True))
    qs.update(document_type=document_type)

    bulk_update_documents.apply_async(
        kwargs={"document_ids": affected_docs},
        headers={"trigger_source": PaperlessTask.TriggerSource.SYSTEM},
    )

    return "OK"


def add_tag(doc_ids: list[int], tag: int) -> Literal["OK"]:
    tag_obj = Tag.objects.get(pk=tag)
    tags_to_add = [tag_obj, *tag_obj.get_ancestors()]

    DocumentTagRelationship = Document.tags.through
    to_create = []
    affected_docs: set[int] = set()

    for t in tags_to_add:
        qs = Document.objects.filter(Q(id__in=doc_ids) & ~Q(tags__id=t.id)).only("pk")
        doc_ids_missing_tag = list(qs.values_list("pk", flat=True))
        affected_docs.update(doc_ids_missing_tag)
        to_create.extend(
            DocumentTagRelationship(document_id=doc, tag_id=t.id)
            for doc in doc_ids_missing_tag
        )

    if to_create:
        DocumentTagRelationship.objects.bulk_create(to_create)

    if affected_docs:
        bulk_update_documents.apply_async(
            kwargs={"document_ids": list(affected_docs)},
            headers={"trigger_source": PaperlessTask.TriggerSource.SYSTEM},
        )

    return "OK"


def remove_tag(doc_ids: list[int], tag: int) -> Literal["OK"]:
    tag_obj = Tag.objects.get(pk=tag)
    tag_ids = [tag_obj.id, *tag_obj.get_descendants_pks()]

    DocumentTagRelationship = Document.tags.through
    qs = DocumentTagRelationship.objects.filter(
        document_id__in=doc_ids,
        tag_id__in=tag_ids,
    )
    affected_docs = list(qs.values_list("document_id", flat=True).distinct())
    qs.delete()

    if affected_docs:
        bulk_update_documents.apply_async(
            kwargs={"document_ids": affected_docs},
            headers={"trigger_source": PaperlessTask.TriggerSource.SYSTEM},
        )

    return "OK"


def modify_tags(
    doc_ids: list[int],
    add_tags: list[int],
    remove_tags: list[int],
) -> Literal["OK"]:
    qs = Document.objects.filter(id__in=doc_ids).only("pk")
    affected_docs = list(qs.values_list("pk", flat=True))
    DocumentTagRelationship = Document.tags.through

    # add with all ancestors
    expanded_add_tags: set[int] = set()
    add_tag_objects = Tag.objects.filter(pk__in=add_tags)
    for t in add_tag_objects:
        expanded_add_tags.add(int(t.id))
        expanded_add_tags.update(int(pk) for pk in t.get_ancestors_pks())

    # remove with all descendants
    expanded_remove_tags: set[int] = set()
    remove_tag_objects = Tag.objects.filter(pk__in=remove_tags)
    for t in remove_tag_objects:
        expanded_remove_tags.add(int(t.id))
        expanded_remove_tags.update(int(pk) for pk in t.get_descendants_pks())

    with transaction.atomic():
        if expanded_remove_tags:
            DocumentTagRelationship.objects.filter(
                document_id__in=affected_docs,
                tag_id__in=expanded_remove_tags,
            ).delete()

        to_create = []
        if expanded_add_tags:
            existing_pairs = set(
                DocumentTagRelationship.objects.filter(
                    document_id__in=affected_docs,
                    tag_id__in=expanded_add_tags,
                ).values_list("document_id", "tag_id"),
            )

            to_create = [
                DocumentTagRelationship(document_id=doc, tag_id=tag)
                for doc in affected_docs
                for tag in expanded_add_tags
                if (doc, tag) not in existing_pairs
            ]

            if to_create:
                DocumentTagRelationship.objects.bulk_create(
                    to_create,
                    ignore_conflicts=True,
                )

    if affected_docs:
        bulk_update_documents.apply_async(
            kwargs={"document_ids": affected_docs},
            headers={"trigger_source": PaperlessTask.TriggerSource.SYSTEM},
        )

    return "OK"


def modify_custom_fields(
    doc_ids: list[int],
    add_custom_fields: list[int] | dict,
    remove_custom_fields: list[int],
) -> Literal["OK"]:
    affected_docs = modify_document_custom_fields(
        doc_ids,
        add_custom_fields,
        remove_custom_fields,
    )

    bulk_update_documents.apply_async(
        kwargs={"document_ids": affected_docs},
        headers={"trigger_source": PaperlessTask.TriggerSource.SYSTEM},
    )

    return "OK"


@shared_task(bind=True)
def delete(
    self,
    doc_ids: list[int],
    *,
    expected_revisions: dict | None = None,
    actor_id: int | None = None,
) -> Literal["OK"]:
    """Guard asynchronous deletion callbacks against repeated delivery."""
    from documents.job_callbacks import run_callback

    def apply_delete() -> Literal["OK"]:
        """Recheck admitted callback targets before deleting their current state."""
        if expected_revisions is None:
            return _delete(doc_ids, raise_errors=bool(self.request.id))
        from django.contrib.auth.models import User

        from documents.job_callbacks import deletion_revision
        from documents.jobs import RejectedResult
        from documents.permissions import permitted_document_ids

        with transaction.atomic():
            documents = list(
                Document.objects.select_for_update()
                .filter(pk__in=doc_ids)
                .order_by("pk")
            )
            for document in documents:
                if deletion_revision(document) != expected_revisions.get(
                    str(document.pk)
                ):
                    raise RejectedResult("Deletion target changed after admission")
            if actor_id is not None:
                actor = User.objects.filter(pk=actor_id, is_active=True).first()
                if (
                    actor is None
                    or not actor.has_perm("documents.delete_document")
                    or Document.objects.filter(pk__in=[doc.pk for doc in documents])
                    .exclude(
                        pk__in=permitted_document_ids(actor, perm="delete_document")
                    )
                    .exists()
                ):
                    raise RejectedResult(
                        "The requesting user can no longer delete these documents"
                    )
            return _delete([doc.pk for doc in documents], raise_errors=True)

    return run_callback(
        self.request.id,
        self.name,
        (doc_ids,),
        {"expected_revisions": expected_revisions, "actor_id": actor_id},
        apply_delete,
    )


def _delete(doc_ids: list[int], *, raise_errors: bool = False) -> Literal["OK"]:
    """Delete selected documents and retain their existing completion policy."""
    try:
        root_ids = (
            Document.objects.filter(id__in=doc_ids, root_document__isnull=True)
            .values_list("id", flat=True)
            .distinct()
        )
        version_ids = (
            Document.objects.filter(root_document_id__in=root_ids)
            .exclude(id__in=doc_ids)
            .values_list("id", flat=True)
            .distinct()
        )
        delete_ids = list({*doc_ids, *version_ids})

        Document.objects.filter(id__in=delete_ids).delete(transaction_id=uuid.uuid4())

        from documents.search import get_backend

        with get_backend().batch_update() as batch:
            for id in delete_ids:
                batch.remove(id)

        status_mgr = DocumentsStatusManager()
        status_mgr.send_documents_deleted(delete_ids)
    except Exception as e:
        if "Data too long for column" in str(e):
            logger.warning(
                "Detected a possible incompatible database column. See https://docs.paperless-ngx.com/troubleshooting/#convert-uuid-field",
            )
        logger.error(f"Error deleting documents: {e!s}")
        if raise_errors:
            raise

    return "OK"


def reprocess(
    doc_ids: list[int],
    *,
    remote_ocr: bool = False,
    user: User | None = None,
    trigger_source: PaperlessTask.TriggerSource = PaperlessTask.TriggerSource.MANUAL,
) -> Literal["OK"]:
    """
    Re-run parsing for the given documents.

    Consumption workflows do not run here, so ``remote_ocr`` is how the user
    asks for the remote engine when it is not configured to handle everything.

    A root document with versions reprocesses its latest version, which is the
    file whose content, archive and thumbnail are shown for it.
    """
    from documents.jobs import submit_reprocess

    latest_version = versions_newest_first(
        Document.objects.filter(root_document=OuterRef("pk")),
    ).values("id")[:1]
    source_ids = (
        Document.objects.filter(id__in=doc_ids)
        .annotate(
            source_id=Case(
                When(root_document__isnull=False, then=F("id")),
                default=Coalesce(Subquery(latest_version), F("id")),
            ),
        )
        .order_by()
        .values_list("source_id", flat=True)
        .distinct()
    )
    for source_id in source_ids:
        submit_reprocess(
            source_id,
            actor_id=user.pk if user else None,
            remote_ocr=remote_ocr,
            trigger_source=trigger_source,
        )

    return "OK"


def set_permissions(
    doc_ids: list[int],
    set_permissions: PermissionsSpec,
    *,
    owner: User | None = None,
    merge: bool = False,
) -> Literal["OK"]:
    qs = Document.objects.filter(id__in=doc_ids).select_related("owner")

    if merge:
        # If merging, only set owner for documents that don't have an owner
        qs.filter(owner__isnull=True).update(owner=owner)
    else:
        qs.update(owner=owner)

    affected_docs = list(qs.values_list("pk", flat=True))
    set_permissions_for_objects(
        permissions=set_permissions,
        model=Document,
        pks=affected_docs,
        merge=merge,
    )

    bulk_update_documents.apply_async(
        kwargs={"document_ids": affected_docs},
        headers={"trigger_source": PaperlessTask.TriggerSource.SYSTEM},
    )

    return "OK"


def rotate(
    doc_ids: list[int],
    degrees: int,
    *,
    source_mode: SourceMode = SourceModeChoices.LATEST_VERSION,
    user: User | None = None,
    trigger_source: PaperlessTask.TriggerSource = PaperlessTask.TriggerSource.WEB_UI,
) -> Literal["OK"]:
    logger.info(
        f"Attempting to rotate {len(doc_ids)} documents by {degrees} degrees.",
    )
    docs_by_id = {
        doc.id: doc
        for doc in Document.objects.select_related("root_document").filter(
            id__in=doc_ids,
        )
    }
    docs_by_root_id: dict[int, ResolvedDocPair] = {}
    for doc_id in doc_ids:
        doc = docs_by_id.get(doc_id)
        if doc is None:
            continue
        pair = _resolve_root_and_source_doc(doc, source_mode=source_mode)
        docs_by_root_id.setdefault(pair.root_doc.id, pair)

    for pair in docs_by_root_id.values():
        if pair.source_doc.mime_type != "application/pdf":
            logger.warning(
                f"Document {pair.root_doc.id} is not a PDF, skipping rotation.",
            )
            continue
        try:
            # Write rotated output to a temp file and create a new version via consume pipeline
            filepath: Path = (
                Path(tempfile.mkdtemp(dir=settings.SCRATCH_DIR))
                / f"{pair.root_doc.id}_rotated.pdf"
            )
            pdf_ops.rotate_pdf(pair.source_doc.source_path, filepath, degrees)

            # Preserve metadata/permissions via overrides; mark as new version
            overrides = DocumentMetadataOverrides().from_document(pair.root_doc)
            if user is not None:
                overrides.actor_id = user.id

            consume_file.apply_async(
                kwargs={
                    "input_doc": ConsumableDocument(
                        source=DocumentSource.ConsumeFolder,
                        original_file=filepath,
                        root_document_id=pair.root_doc.id,
                    ),
                    "overrides": overrides,
                },
                headers={"trigger_source": trigger_source},
            )
            logger.info(
                f"Queued new rotated version for document {pair.root_doc.id} by {degrees} degrees",
            )
        except Exception as e:
            logger.exception(f"Error rotating document {pair.root_doc.id}: {e}")

    return "OK"


def merge(
    doc_ids: list[int],
    *,
    metadata_document_id: int | None = None,
    delete_originals: bool = False,
    archive_fallback: bool = False,
    source_mode: SourceMode = SourceModeChoices.LATEST_VERSION,
    user: User | None = None,
    trigger_source: PaperlessTask.TriggerSource = PaperlessTask.TriggerSource.WEB_UI,
) -> Literal["OK"]:
    from documents.job_callbacks import deletion_revision

    logger.info(
        f"Attempting to merge {len(doc_ids)} documents into a single document.",
    )
    qs = Document.objects.select_related("root_document").filter(id__in=doc_ids)
    docs_by_id = {doc.id: doc for doc in qs}
    expected_revisions = {
        str(doc.pk): deletion_revision(doc) for doc in docs_by_id.values()
    }
    affected_docs: list[int] = []
    handoff_asn: int | None = None
    with pdf_ops.PdfMerger() as merger:
        # use doc_ids to preserve order
        for doc_id in doc_ids:
            doc = docs_by_id.get(doc_id)
            if doc is None:
                continue
            pair = _resolve_root_and_source_doc(doc, source_mode=source_mode)
            try:
                # archive_path is None when there is no archive version
                archive_path = (
                    pair.source_doc.archive_path
                    if archive_fallback
                    and pair.source_doc.mime_type != "application/pdf"
                    else None
                )
                merger.add(
                    archive_path
                    if archive_path is not None
                    else pair.source_doc.source_path,
                )
                affected_docs.append(doc.id)
                if handoff_asn is None and doc.archive_serial_number is not None:
                    handoff_asn = doc.archive_serial_number
            except Exception as e:
                logger.exception(
                    f"Error merging document {doc.id}, it will not be included in the merge: {e}",
                )
        if len(affected_docs) == 0:
            logger.warning("No documents were merged")
            return "OK"

        filepath = (
            Path(
                tempfile.mkdtemp(dir=settings.SCRATCH_DIR),
            )
            / f"{'_'.join([str(doc_id) for doc_id in affected_docs])[:100]}_merged.pdf"
        )
        merger.save(filepath)

    if metadata_document_id:
        metadata_document = qs.get(id=metadata_document_id)
        if metadata_document is not None:
            overrides: DocumentMetadataOverrides = (
                DocumentMetadataOverrides.from_document(metadata_document)
            )
            overrides.title = metadata_document.title + " (merged)"
            if metadata_document.archive_serial_number is not None:
                handoff_asn = metadata_document.archive_serial_number
        else:
            overrides = DocumentMetadataOverrides()
    else:
        overrides = DocumentMetadataOverrides()

    if user is not None:
        overrides.owner_id = user.id
        overrides.actor_id = user.id
    if not delete_originals:
        overrides.skip_asn_if_exists = True

    if delete_originals and handoff_asn is not None:
        overrides.asn = handoff_asn

    logger.info("Adding merged document to the task queue.")

    consume_task = consume_file.s(
        input_doc=ConsumableDocument(
            source=DocumentSource.ConsumeFolder,
            original_file=filepath,
        ),
        overrides=overrides,
    ).set(headers={"trigger_source": trigger_source})

    if delete_originals:
        backup = release_archive_serial_numbers(
            affected_docs,
            expected_asns={
                doc_id: docs_by_id[doc_id].archive_serial_number
                for doc_id in affected_docs
            },
        )
        for doc_id in backup:
            expected_revisions[str(doc_id)]["fields"]["archive_serial_number"] = None
        logger.info(
            "Queueing removal of original documents after consumption of merged document",
        )
        try:
            consume_task.apply_async(
                link=[
                    delete.si(
                        affected_docs,
                        expected_revisions=expected_revisions,
                        actor_id=user.pk if user else None,
                    ),
                ],
                link_error=[restore_archive_serial_numbers_task.si(backup)],
            )
        except Exception:
            restore_archive_serial_numbers(backup)
            raise
    else:
        consume_task.apply_async()

    return "OK"


def merge_as_versions(
    doc_ids: list[int],
    *,
    root_document_id: int,
    version_label: str | None = None,
    user: User | None = None,
) -> Literal["OK"]:
    with transaction.atomic():
        documents = list(
            # Ordered by pk so concurrent merges take the row locks in the same order
            Document.objects.select_for_update()
            .filter(id__in=doc_ids)
            .order_by("id")
            .defer("content"),
        )
        documents_by_id = {document.id: document for document in documents}

        source_ids = [doc_id for doc_id in doc_ids if doc_id != root_document_id]
        root_document = documents_by_id[root_document_id]
        next_version_index = (
            Document.global_objects.filter(
                root_document_id=root_document_id,
            ).aggregate(max_index=Max("version_index"))["max_index"]
            or 0
        )

        # A version gives up its ASN
        source_asns = [
            documents_by_id[source_id].archive_serial_number
            for source_id in source_ids
            if documents_by_id[source_id].archive_serial_number is not None
        ]

        updated_fields = ["root_document", "version_index", "archive_serial_number"]
        if version_label is not None:
            updated_fields.append("version_label")

        for source_id in source_ids:
            next_version_index += 1
            source_document = documents_by_id[source_id]
            source_document.root_document_id = root_document.pk
            source_document.version_index = next_version_index
            source_document.archive_serial_number = None
            if version_label is not None:
                source_document.version_label = version_label

        # bulk_update and not save() to avoid post_save now
        Document.objects.bulk_update(
            [documents_by_id[source_id] for source_id in source_ids],
            updated_fields,
        )

        root_updates = {"modified": timezone.now()}
        if source_asns and root_document.archive_serial_number is None:
            # If a version had one, hand the ASN over, the same as merge() does
            root_updates["archive_serial_number"] = source_asns.pop(0)
            logger.info(
                f"Document {root_document.id} took archive serial number "
                f"{root_updates['archive_serial_number']} from a document merged into it",
            )
        if source_asns:
            logger.warning(
                f"Archive serial number(s) {source_asns} were removed by merging "
                f"those documents as versions of document {root_document.id}",
            )

        Document.objects.filter(pk=root_document.pk).update(**root_updates)

        if settings.AUDIT_LOG_ENABLED:
            # update() doesn't fire auditlog signals, so manual
            LogEntry.objects.log_create(
                instance=root_document,
                changes={"Merged As Versions": ["None", source_ids]},
                action=LogEntry.Action.UPDATE,
                actor=user,
                additional_data={
                    "reason": "Merged as versions",
                    "version_ids": source_ids,
                },
            )

    # One batch rather than a task each
    from documents.search import SearchIndexLockError
    from documents.search import get_backend

    try:
        with get_backend().batch_update() as batch:
            for source_id in source_ids:
                batch.remove(source_id)
    except SearchIndexLockError:
        logger.error(
            f"Search index lock exhausted removing {source_ids}, "
            f"scheduling deferred index removal",
        )
        for source_id in source_ids:
            remove_document_from_index.apply_async(args=[source_id], countdown=60)

    bulk_update_documents.apply_async(
        kwargs={"document_ids": [root_document_id]},
        headers={"trigger_source": PaperlessTask.TriggerSource.SYSTEM},
    )

    # And as far as the frontend is concerned, they're deleted
    status_mgr = DocumentsStatusManager()
    status_mgr.send_documents_deleted(source_ids)

    return "OK"


def split(
    doc_ids: list[int],
    pages: list[list[int]],
    *,
    delete_originals: bool = False,
    source_mode: SourceMode = SourceModeChoices.LATEST_VERSION,
    user: User | None = None,
    trigger_source: PaperlessTask.TriggerSource = PaperlessTask.TriggerSource.WEB_UI,
) -> Literal["OK"]:
    from documents.job_callbacks import deletion_revision

    logger.info(
        f"Attempting to split document {doc_ids[0]} into {len(pages)} documents",
    )
    doc = Document.objects.select_related("root_document").get(id=doc_ids[0])
    pair = _resolve_root_and_source_doc(doc, source_mode=source_mode)
    expected_revisions = {str(doc.pk): deletion_revision(doc)}

    consume_tasks = []

    try:
        outputs = [
            (
                [pdf_ops.PageSpec(page) for page in split_doc],
                partial(_scratch_path, f"{doc.id}_{split_doc[0]}-{split_doc[-1]}.pdf"),
            )
            for split_doc in pages
        ]
        filepaths = pdf_ops.build_pdfs(pair.source_doc.source_path, outputs)

        for idx, (split_doc, filepath) in enumerate(
            zip(pages, filepaths, strict=True),
        ):
            overrides: DocumentMetadataOverrides = (
                DocumentMetadataOverrides().from_document(doc)
            )
            overrides.title = f"{doc.title} (split {idx + 1})"
            if user is not None:
                overrides.owner_id = user.id
                overrides.actor_id = user.id
            if not delete_originals:
                overrides.skip_asn_if_exists = True
            logger.info(
                f"Adding split document with pages {split_doc} to the task queue.",
            )
            consume_tasks.append(
                consume_file.s(
                    input_doc=ConsumableDocument(
                        source=DocumentSource.ConsumeFolder,
                        original_file=filepath,
                    ),
                    overrides=overrides,
                ).set(headers={"trigger_source": trigger_source}),
            )

        if delete_originals:
            backup = release_archive_serial_numbers(
                [doc.id],
                expected_asns={doc.pk: doc.archive_serial_number},
            )
            if doc.pk in backup:
                expected_revisions[str(doc.pk)]["fields"]["archive_serial_number"] = (
                    None
                )
            logger.info(
                "Queueing removal of original document after consumption of the split documents",
            )
            try:
                chord(
                    header=consume_tasks,
                    body=delete.si(
                        [doc.id],
                        expected_revisions=expected_revisions,
                        actor_id=user.pk if user else None,
                    ),
                ).on_error(
                    restore_archive_serial_numbers_task.si(backup),
                ).apply_async()
            except Exception:
                restore_archive_serial_numbers(backup)
                raise
        else:
            group(consume_tasks).delay()

    except Exception as e:
        logger.exception(f"Error splitting document {doc.id}: {e}")

    return "OK"


def delete_pages(
    doc_ids: list[int],
    pages: list[int],
    *,
    source_mode: SourceMode = SourceModeChoices.LATEST_VERSION,
    user: User | None = None,
    trigger_source: PaperlessTask.TriggerSource = PaperlessTask.TriggerSource.WEB_UI,
) -> Literal["OK"]:
    logger.info(
        f"Attempting to delete pages {pages} from {len(doc_ids)} documents",
    )
    doc = Document.objects.select_related("root_document").get(id=doc_ids[0])
    pair = _resolve_root_and_source_doc(doc, source_mode=source_mode)

    try:
        # Produce edited PDF to a temp file and create a new version
        filepath: Path = (
            Path(tempfile.mkdtemp(dir=settings.SCRATCH_DIR))
            / f"{pair.root_doc.id}_pages_deleted.pdf"
        )
        pdf_ops.remove_pages(pair.source_doc.source_path, filepath, pages)

        overrides = DocumentMetadataOverrides().from_document(pair.root_doc)
        if user is not None:
            overrides.actor_id = user.id
        consume_file.apply_async(
            kwargs={
                "input_doc": ConsumableDocument(
                    source=DocumentSource.ConsumeFolder,
                    original_file=filepath,
                    root_document_id=pair.root_doc.id,
                ),
                "overrides": overrides,
            },
            headers={"trigger_source": trigger_source},
        )
        logger.info(
            f"Queued new version for document {pair.root_doc.id} after deleting pages {pages}",
        )
    except Exception as e:
        logger.exception(f"Error deleting pages from document {pair.root_doc.id}: {e}")

    return "OK"


def edit_pdf(
    doc_ids: list[int],
    operations: list[dict[str, int]],
    *,
    delete_original: bool = False,
    update_document: bool = False,
    include_metadata: bool = True,
    source_mode: SourceMode = SourceModeChoices.LATEST_VERSION,
    user: User | None = None,
    trigger_source: PaperlessTask.TriggerSource = PaperlessTask.TriggerSource.WEB_UI,
) -> Literal["OK"]:
    """
    Operations is a list of dictionaries describing the final PDF pages.
    Each entry must contain the original page number in `page` and may
    specify `rotate` in degrees and `doc` indicating the output
    document index (for splitting). Pages omitted from the list are
    discarded.
    """
    from documents.job_callbacks import deletion_revision

    logger.info(
        f"Editing PDF of document {doc_ids[0]} with {len(operations)} operations",
    )
    doc = Document.objects.select_related("root_document").get(id=doc_ids[0])
    pair = _resolve_root_and_source_doc(doc, source_mode=source_mode)
    expected_revisions = {str(doc.pk): deletion_revision(doc)}
    try:
        output_count = pdf_ops.validate_page_operations(
            operations,
            single_output=update_document,
        )
        page_specs: list[list[pdf_ops.PageSpec]] = [[] for _ in range(output_count)]
        for op in operations:
            page_specs[op.get("doc", 0)].append(
                pdf_ops.PageSpec(op["page"], op.get("rotate", 0)),
            )

        if update_document:
            # Create a new version from the edited PDF rather than replacing in-place
            (filepath,) = pdf_ops.build_pdfs(
                pair.source_doc.source_path,
                [
                    (
                        page_specs[0],
                        partial(_scratch_path, f"{pair.root_doc.id}_edited.pdf"),
                    ),
                ],
            )
            overrides = (
                DocumentMetadataOverrides().from_document(pair.root_doc)
                if include_metadata
                else DocumentMetadataOverrides()
            )
            if user is not None:
                overrides.owner_id = user.id
                overrides.actor_id = user.id
            consume_file.apply_async(
                kwargs={
                    "input_doc": ConsumableDocument(
                        source=DocumentSource.ConsumeFolder,
                        original_file=filepath,
                        root_document_id=pair.root_doc.id,
                    ),
                    "overrides": overrides,
                },
                headers={"trigger_source": trigger_source},
            )
        else:
            version_filepaths = pdf_ops.build_pdfs(
                pair.source_doc.source_path,
                [
                    (
                        specs,
                        partial(
                            _scratch_path,
                            f"{pair.root_doc.id}_edit_{idx}.pdf",
                        ),
                    )
                    for idx, specs in enumerate(page_specs, start=1)
                ],
            )
            consume_tasks = []
            overrides = (
                DocumentMetadataOverrides().from_document(pair.root_doc)
                if include_metadata
                else DocumentMetadataOverrides()
            )
            if user is not None:
                overrides.owner_id = user.id
                overrides.actor_id = user.id
            if not delete_original:
                overrides.skip_asn_if_exists = True
            if delete_original and output_count == 1:
                overrides.asn = pair.root_doc.archive_serial_number
            for version_filepath in version_filepaths:
                consume_tasks.append(
                    consume_file.s(
                        input_doc=ConsumableDocument(
                            source=DocumentSource.ConsumeFolder,
                            original_file=version_filepath,
                        ),
                        overrides=overrides,
                    ).set(headers={"trigger_source": trigger_source}),
                )

            if delete_original:
                backup = release_archive_serial_numbers(
                    [doc.id],
                    expected_asns={doc.pk: doc.archive_serial_number},
                )
                if doc.pk in backup:
                    expected_revisions[str(doc.pk)]["fields"][
                        "archive_serial_number"
                    ] = None
                try:
                    chord(
                        header=consume_tasks,
                        body=delete.si(
                            [doc.id],
                            expected_revisions=expected_revisions,
                            actor_id=user.pk if user else None,
                        ),
                    ).on_error(
                        restore_archive_serial_numbers_task.si(backup),
                    ).apply_async()
                except Exception:
                    restore_archive_serial_numbers(backup)
                    raise
            else:
                group(consume_tasks).delay()

    except Exception as e:
        logger.exception(f"Error editing document {pair.root_doc.id}: {e}")
        raise ValueError(
            f"An error occurred while editing the document: {e}",
        ) from e

    return "OK"


def remove_password(
    doc_ids: list[int],
    password: str,
    *,
    update_document: bool = False,
    delete_original: bool = False,
    include_metadata: bool = True,
    source_mode: SourceMode = SourceModeChoices.LATEST_VERSION,
    user: User | None = None,
    trigger_source: PaperlessTask.TriggerSource = PaperlessTask.TriggerSource.WEB_UI,
    source_paths_by_id: Mapping[int, Path] | None = None,
) -> Literal["OK"]:
    """
    Remove password protection from PDF documents.
    """
    from documents.job_callbacks import deletion_revision

    for doc_id in doc_ids:
        doc = Document.objects.select_related("root_document").get(id=doc_id)
        pair = _resolve_root_and_source_doc(doc, source_mode=source_mode)
        expected_revisions = {str(doc.pk): deletion_revision(doc)}
        try:
            logger.info(
                f"Attempting password removal from document {pair.root_doc.id}",
            )
            # The caller may supply an explicit source path (e.g. the staged
            # file during consumption, before source_path is populated).
            source_path = (source_paths_by_id or {}).get(
                doc.id,
                pair.source_doc.source_path,
            )
            if not pdf_ops.needs_decrypt(source_path):
                logger.info(
                    "Skipping password removal for document %s because the "
                    "source PDF is not encrypted",
                    pair.root_doc.id,
                )
                continue

            filepath = pdf_ops.decrypt_pdf(
                source_path,
                partial(_scratch_path, f"{pair.root_doc.id}_unprotected.pdf"),
                password,
            )

            if update_document:
                # Create a new version rather than modifying the root/original in place.
                overrides = (
                    DocumentMetadataOverrides().from_document(pair.root_doc)
                    if include_metadata
                    else DocumentMetadataOverrides()
                )
                if user is not None:
                    overrides.owner_id = user.id
                    overrides.actor_id = user.id
                consume_file.apply_async(
                    kwargs={
                        "input_doc": ConsumableDocument(
                            source=DocumentSource.ConsumeFolder,
                            original_file=filepath,
                            root_document_id=pair.root_doc.id,
                        ),
                        "overrides": overrides,
                    },
                    headers={"trigger_source": trigger_source},
                )
            else:
                consume_tasks = []
                overrides = (
                    DocumentMetadataOverrides().from_document(pair.root_doc)
                    if include_metadata
                    else DocumentMetadataOverrides()
                )
                if user is not None:
                    overrides.owner_id = user.id
                    overrides.actor_id = user.id

                consume_tasks.append(
                    consume_file.s(
                        input_doc=ConsumableDocument(
                            source=DocumentSource.ConsumeFolder,
                            original_file=filepath,
                        ),
                        overrides=overrides,
                    ).set(headers={"trigger_source": trigger_source}),
                )

                if delete_original:
                    chord(
                        header=consume_tasks,
                        body=delete.si(
                            [doc.id],
                            expected_revisions=expected_revisions,
                            actor_id=user.pk if user else None,
                        ),
                    ).delay()
                else:
                    group(consume_tasks).delay()

        except Exception as e:
            logger.exception(
                f"Error removing password from document {pair.root_doc.id}: {e}",
            )
            raise ValueError(
                f"An error occurred while removing the password: {e}",
            ) from e

    return "OK"
