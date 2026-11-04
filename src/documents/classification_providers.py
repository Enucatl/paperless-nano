"""Inference adapters; training and activation are separate operations."""

import json
from hashlib import sha256

from documents.classification import ClassificationRequest
from documents.classification import ClassificationResult


class SklearnProvider:
    """Adapt a verified existing classifier to plain inference snapshots."""

    def __init__(
        self, classifier=None, identity: tuple[str, str] | None = None
    ) -> None:
        """Keep model loading lazy until inference is requested."""
        self.classifier = classifier
        self._identity = identity

    @property
    def configuration(self) -> dict:
        """Declare the settings that affect built-in inference."""
        from django.conf import settings

        return {
            "language": settings.CLASSIFIER_LANGUAGE,
            "threshold": settings.CLASSIFIER_MATCH_THRESHOLD,
        }

    def _model_identity(self, model: str | None) -> tuple[str, str]:
        """Bind one model to its effective inference configuration."""
        digest = sha256(
            json.dumps([model, self.configuration], sort_keys=True).encode()
        ).hexdigest()
        return ("sklearn:1", digest)

    @property
    def identity(self) -> tuple[str, str] | None:
        """Expose a loaded snapshot or the active model without loading its data."""
        if self._identity is not None:
            return self._identity
        if self.classifier is not None:
            return self._model_identity(self.classifier.model_identity)
        from documents.classifier import active_model_identity

        model = active_model_identity()
        return self._model_identity(model) if model else None

    @property
    def current_identity(self) -> tuple[str, str] | None:
        """Reject activation changes while permitting explicitly unpersisted models."""
        if self.classifier is not None and self.classifier.model_identity is None:
            return self.identity
        from documents.classifier import active_model_identity

        model = active_model_identity()
        return self._model_identity(model) if model else None

    def classify(self, request: ClassificationRequest) -> ClassificationResult:
        """Keep existing predictions, excluding labels no longer permitted."""
        if self.classifier is None:
            from documents.classification import ClassificationProviderError
            from documents.classifier import load_classifier

            self.classifier = load_classifier()
            if self.classifier is None:
                raise ClassificationProviderError("Classification model is unavailable")
        methods = {
            "tags": self.classifier.predict_tags,
            "correspondents": self.classifier.predict_correspondent,
            "document_types": self.classifier.predict_document_type,
            "storage_paths": self.classifier.predict_storage_path,
        }
        assignments = {}
        for field, candidates in request["candidates"].items():
            predicted = methods[field](request["content"])
            values = predicted if field == "tags" else [predicted]
            allowed = {candidate["id"] for candidate in candidates}
            assignments[field] = [int(value) for value in values if value in allowed]
        return ClassificationResult(
            version=1,
            revision=request["revision"],
            provider=self.identity[0],
            model=self.identity[1],
            assignments=assignments,
        )


class NameTaggingProvider:
    """Small alternative provider: suggest auto tags named in the content.

    This opt-in example has no training, ORM, or processing-library dependency.
    It deliberately proposes no scalar assignments; core rules still apply.
    """

    identity = ("name-tags:1", "casefold-substring:1")

    def classify(self, request: ClassificationRequest) -> ClassificationResult:
        """Propose only supplied tag candidates with names present in the text."""
        content = request["content"].casefold()
        return ClassificationResult(
            version=1,
            revision=request["revision"],
            provider=self.identity[0],
            model=self.identity[1],
            assignments={
                field: [
                    candidate["id"]
                    for candidate in candidates
                    if field == "tags"
                    and candidate["name"]
                    and candidate["name"].casefold() in content
                ]
                for field, candidates in request["candidates"].items()
            },
        )
