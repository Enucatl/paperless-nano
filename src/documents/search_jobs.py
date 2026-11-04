"""Deferred fresh-state writes requested by the selected search provider."""

import logging

logger = logging.getLogger("paperless.search")


def defer_index_write(doc_id: int, *, remove: bool = False) -> None:
    """Schedule a fresh-state write or idempotent removal after lock exhaustion."""
    from documents.tasks import index_document
    from documents.tasks import remove_document_from_index

    logger.error(
        "Search index lock exhausted for document %d; scheduling deferred %s",
        doc_id,
        "removal" if remove else "write",
    )
    task = remove_document_from_index if remove else index_document
    task.apply_async(args=[doc_id], countdown=60)
