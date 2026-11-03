"""Map persisted document state to plain search index records."""

from __future__ import annotations

import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta
from typing import TYPE_CHECKING
from typing import Final
from typing import TypeAlias

from documents.search._query import extract_cjk_text
from documents.search._query import normalize_search_text
from documents.search._tokenizer import autocomplete_tokens

if TYPE_CHECKING:
    from documents.models import Document

IndexValue: TypeAlias = str | int | datetime | dict[str, str | None]
IndexRecord: TypeAlias = dict[str, list[IndexValue]]

logger = logging.getLogger("paperless.search")

# Tantivy stores dates as signed 64-bit nanoseconds since the Unix epoch.
_INDEX_DATE_NANOS_MIN: Final[int] = -(2**63)
_INDEX_DATE_NANOS_MAX: Final[int] = 2**63 - 1
_UNIX_EPOCH: Final[datetime] = datetime(1970, 1, 1, tzinfo=UTC)


def _is_indexable_date(value: datetime) -> bool:
    """Whether value, at whole-second precision, fits Tantivy's date range."""
    nanos = ((value - _UNIX_EPOCH) // timedelta(seconds=1)) * 1_000_000_000
    return _INDEX_DATE_NANOS_MIN <= nanos <= _INDEX_DATE_NANOS_MAX


def _extract_autocomplete_words(text_sources: list[str]) -> set[str]:
    """Normalize autocomplete words with the same analyzer as the index."""
    words: set[str] = set()
    for text in text_sources:
        if text:
            words.update(autocomplete_tokens(text))
    return words


def document_to_record(
    document: Document,
    *,
    viewer_ids: list[int],
    viewer_group_ids: list[int],
) -> IndexRecord:
    """Map a root document and explicit grants to independent field values.

    Effective version content and related values are resolved here, before the
    record reaches the engine. Bulk callers prefetch relations and annotate
    effective content to avoid a query for each document.
    """
    content = normalize_search_text(document.get_effective_content() or "")
    title = normalize_search_text(document.title)
    record: IndexRecord = {
        "id": [document.pk],
        "checksum": [document.checksum],
        "title": [title],
        "title_sort": [title],
        "simple_title": [title],
        "content": [content],
        "simple_content": [content],
    }
    if cjk_title := extract_cjk_text(title):
        record["bigram_title"] = [cjk_title]
    if content and (cjk_content := extract_cjk_text(content)):
        record["bigram_content"] = [cjk_content]
    if document.original_filename:
        record["original_filename"] = [
            normalize_search_text(document.original_filename),
        ]

    text_sources = [title, content]
    if document.correspondent:
        correspondent = normalize_search_text(document.correspondent.name)
        record["correspondent"] = [correspondent]
        record["correspondent_sort"] = [correspondent]
        if cjk_corr := extract_cjk_text(correspondent):
            record["bigram_correspondent"] = [cjk_corr]
        text_sources.append(correspondent)
    if document.document_type:
        document_type = normalize_search_text(document.document_type.name)
        record["document_type"] = [document_type]
        record["type_sort"] = [document_type]
        if cjk_type := extract_cjk_text(document_type):
            record["bigram_document_type"] = [cjk_type]
        text_sources.append(document_type)
    if document.storage_path:
        record["storage_path"] = [normalize_search_text(document.storage_path.name)]

    for tag in document.tags.all():
        tag_name = normalize_search_text(tag.name)
        record.setdefault("tag", []).append(tag_name)
        if cjk_tag := extract_cjk_text(tag_name):
            record.setdefault("bigram_tag", []).append(cjk_tag)
        text_sources.append(tag_name)

    # Snippets need a plain-text companion; structured queries use JSON notes.
    note_texts: list[str] = []
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
        note_texts.append(note_text)
    if note_texts:
        record["notes_text"] = [" ".join(note_texts)]
    record["num_notes"] = [len(note_texts)]

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
    if _is_indexable_date(created_date):
        record["created"] = [created_date]
    else:
        logger.warning(
            "Document %s has a created date (%s) outside the range the search "
            "index can store; it will be indexed without a created date",
            document.pk,
            document.created,
        )
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
    words = sorted(_extract_autocomplete_words(text_sources))
    if words:
        record["autocomplete_word"] = words
    return record
