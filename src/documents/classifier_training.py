"""Train and activate the built-in classifier independently of job execution."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

from django.conf import settings
from django.core.cache import cache
from filelock import FileLock

from documents.caching import CLASSIFIER_HASH_KEY
from documents.caching import CLASSIFIER_MODIFIED_KEY
from documents.caching import CLASSIFIER_VERSION_KEY
from documents.models import Correspondent
from documents.models import DocumentType
from documents.models import StoragePath
from documents.models import Tag

logger = logging.getLogger("paperless.classifier")


def train_classifier(
    *,
    status_callback: Callable[[str], None] | None = None,
) -> str:
    """Serialize training jobs and activate a complete model on success."""
    if not settings.CLASSIFIER_ENABLED:
        return "Classifier disabled, not training"
    with FileLock(f"{settings.MODEL_FILE}.training.lock"):
        return _train_classifier(status_callback=status_callback)


def _train_classifier(
    *,
    status_callback: Callable[[str], None] | None = None,
) -> str:
    """Gather training data and publish only a successfully fitted model."""
    if (
        not Tag.objects.filter(matching_algorithm=Tag.MATCH_AUTO).exists()
        and not DocumentType.objects.filter(matching_algorithm=Tag.MATCH_AUTO).exists()
        and not Correspondent.objects.filter(matching_algorithm=Tag.MATCH_AUTO).exists()
        and not StoragePath.objects.filter(matching_algorithm=Tag.MATCH_AUTO).exists()
    ):
        result = "No automatic matching items, not training"
        logger.info(result)
        # Special case, items were once auto and trained, so remove the model
        # and prevent its use again
        from documents.classifier import classifier_model_lock

        with classifier_model_lock():
            settings.MODEL_FILE.unlink(missing_ok=True)
            cache.delete_many(
                [CLASSIFIER_MODIFIED_KEY, CLASSIFIER_HASH_KEY, CLASSIFIER_VERSION_KEY],
            )
        return result

    from documents.classifier import DocumentClassifier
    from documents.classifier import load_classifier

    classifier = load_classifier()

    if not classifier:
        classifier = DocumentClassifier()

    if classifier.train(status_callback=status_callback):
        logger.info(
            f"Saving updated classifier model to {settings.MODEL_FILE}...",
        )
        classifier.save()
        return "Training completed successfully"
    else:
        logger.debug("Training data unchanged.")
        return "Training data unchanged"
