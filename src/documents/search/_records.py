"""Map persisted document state to plain search index records."""

from __future__ import annotations

import unicodedata
from datetime import UTC
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from documents.models import Document
    from documents.search.contract import DocumentFacts


def normalize_search_text(text: str) -> str:
    """Normalize Unicode encoding without choosing a search representation."""
    return unicodedata.normalize("NFC", text)


def document_to_record(
    document: Document,
    *,
    viewer_ids: list[int],
    viewer_group_ids: list[int],
) -> DocumentFacts:
    """Map a root document and explicit grants to independent field values.

    Effective version content and related values are resolved here, before the
    record reaches the engine. Bulk callers prefetch relations and annotate
    effective content to avoid a query for each document.
    """
    content = normalize_search_text(document.get_effective_content() or "")
    title = normalize_search_text(document.title)
    record: DocumentFacts = {
        "id": [document.pk],
        "checksum": [document.checksum],
        "title": [title],
        "content": [content],
    }
    if document.original_filename:
        record["original_filename"] = [
            normalize_search_text(document.original_filename),
        ]

    if document.correspondent:
        correspondent = normalize_search_text(document.correspondent.name)
        record["correspondent"] = [correspondent]
    if document.document_type:
        document_type = normalize_search_text(document.document_type.name)
        record["document_type"] = [document_type]
    if document.storage_path:
        record["storage_path"] = [normalize_search_text(document.storage_path.name)]

    for tag in document.tags.all():
        tag_name = normalize_search_text(tag.name)
        record.setdefault("tag", []).append(tag_name)

    for note in document.notes.all():
        note_text = normalize_search_text(note.note)
        record.setdefault("notes", []).append(
            {
                "note": note_text,
                "user": normalize_search_text(note.user.username)
                if note.user
                else None,
            },
        )
    record["num_notes"] = [len(record.get("notes", []))]

    for cfi in document.custom_fields.all():
        search_value = cfi.value_for_search
        if search_value is not None:
            record.setdefault("custom_fields", []).append(
                {
                    "name": normalize_search_text(cfi.field.name),
                    "value": normalize_search_text(search_value),
                },
            )
    for barcode in document.get_effective_barcodes():
        record.setdefault("barcodes", []).append(
            {
                "value": normalize_search_text(barcode.value),
                "format": normalize_search_text(barcode.format),
            },
        )

    created_date = datetime(
        document.created.year,
        document.created.month,
        document.created.day,
        tzinfo=UTC,
    )
    record["created"] = [created_date]
    record["modified"] = [document.modified]
    record["added"] = [document.added]
    if document.archive_serial_number is not None:
        record["asn"] = [document.archive_serial_number]
    if document.page_count is not None:
        record["page_count"] = [document.page_count]
    if document.owner_id:
        record["owner_id"] = [document.owner_id]
    if viewer_ids:
        record["viewer_id"] = list(viewer_ids)
    if viewer_group_ids:
        record["viewer_group_id"] = list(viewer_group_ids)
    return record
