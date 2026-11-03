"""Lock retry timing and Celery dispatch for search writes."""

from __future__ import annotations

import logging
import random
import time
from typing import TYPE_CHECKING
from typing import Final

from documents.exceptions import SearchIndexLockError

if TYPE_CHECKING:
    from documents.search._engine import WriteBatch

logger = logging.getLogger("paperless.search")

_LOCK_TIMEOUT_SECONDS: Final[float] = 10.0
_LOCK_RETRY_ATTEMPTS: Final[int] = 4
_LOCK_BACKOFF_BASE: Final[float] = 1.0
_LOCK_BACKOFF_CAP: Final[float] = 10.0


def enter_batch_with_retry(batch: WriteBatch) -> None:
    """Acquire the engine batch with the existing bounded, jittered backoff."""
    for attempt in range(_LOCK_RETRY_ATTEMPTS):
        try:
            batch.__enter__()
            return
        except SearchIndexLockError:
            if attempt == _LOCK_RETRY_ATTEMPTS - 1:
                raise
            sleep_s = random.uniform(
                0,
                min(_LOCK_BACKOFF_CAP, _LOCK_BACKOFF_BASE * (2**attempt)),
            )
            logger.debug(
                "Index lock contention; retrying in %.2fs (attempt %d/%d)",
                sleep_s,
                attempt + 1,
                _LOCK_RETRY_ATTEMPTS,
            )
            time.sleep(sleep_s)


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
