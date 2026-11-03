"""Inference adapters; training and activation are separate operations."""

from documents.classification import ClassificationRequest
from documents.classification import ClassificationResult


class SklearnProvider:
    """Adapt a verified existing classifier to plain inference snapshots."""

    def __init__(self, classifier, identity: tuple[str, str]) -> None:
        self.classifier = classifier
        self.identity = identity

    def classify(self, request: ClassificationRequest) -> ClassificationResult:
        """Keep existing predictions, excluding labels no longer permitted."""
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
