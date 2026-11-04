"""Tantivy schema derivations from provider-neutral document facts."""

import logging
from datetime import UTC
from datetime import datetime
from datetime import timedelta

from documents.search._query import extract_cjk_text
from documents.search._tokenizer import autocomplete_tokens
from documents.search.contract import DocumentFacts


def index_record(facts: DocumentFacts) -> DocumentFacts:
    """Derive sort, bigram, snippet, autocomplete and representable date fields."""
    record = {key: list(values) for key, values in facts.items()}
    for source, target in (
        ("title", "title_sort"),
        ("title", "simple_title"),
        ("content", "simple_content"),
        ("correspondent", "correspondent_sort"),
        ("document_type", "type_sort"),
    ):
        if source in record:
            record[target] = record[source]
    words = set()
    for field in ("title", "content", "correspondent", "document_type", "tag"):
        for value in record.get(field, []):
            if cjk := extract_cjk_text(value):
                record.setdefault("bigram_" + field, []).append(cjk)
            words.update(autocomplete_tokens(value))
    if words:
        record["autocomplete_word"] = sorted(words)
    notes = [note["note"] for note in record.get("notes", [])]
    if notes:
        record["notes_text"] = [" ".join(notes)]
    if record.get("created"):
        value = record["created"][0]
        nanos = (
            (value - datetime(1970, 1, 1, tzinfo=UTC)) // timedelta(seconds=1)
        ) * 1_000_000_000
        if not -(2**63) <= nanos <= 2**63 - 1:
            logging.getLogger("paperless.search").warning(
                "Document %s has a created date (%s) outside the range the search index can store; it will be indexed without a created date",
                record["id"][0],
                value,
            )
            del record["created"]
    return record
