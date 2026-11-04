"""Serializable inference contract, independent of Django and job execution."""

from typing import NotRequired
from typing import Protocol
from typing import TypedDict


class Candidate(TypedDict):
    """A value the core permits this request to propose."""

    id: int
    name: str


class ClassificationRequest(TypedDict):
    """Versioned content, metadata and permitted candidate snapshot."""

    version: int
    revision: str
    content: str
    metadata: dict
    candidates: dict[str, list[Candidate]]


class ClassificationResult(TypedDict):
    """Proposals only; persistence and rule matching belong to the core."""

    version: int
    revision: str
    provider: str
    model: str
    assignments: dict[str, list[int]]
    suggestions: NotRequired[dict]


class ClassificationProvider(Protocol):
    """Inference interface with no HTTP, task, or database inputs.

    Implementations may expose a JSON-object ``configuration`` snapshot. A
    ``current_identity`` property can validate an active model without loading
    it; otherwise core compares a fresh selected provider's identity. Legacy
    construction adapters belong to the implementation, not the orchestrator.
    """

    @property
    def identity(self) -> tuple[str, str]:
        """Return implementation version and immutable model/configuration identity."""
        ...

    def classify(self, request: ClassificationRequest) -> ClassificationResult:
        """Propose permitted assignments for this snapshot without side effects."""
        ...


class InvalidClassificationResult(ValueError):
    """A malformed, forbidden, superseded, or stale proposal cannot be applied."""


class ClassificationProviderError(RuntimeError):
    """Inference failed without producing a result; a fresh attempt is safe."""


def validate_result(
    request: ClassificationRequest,
    result: ClassificationResult,
    identity: tuple[str, str],
) -> None:
    """Reject incompatible results and values outside the supplied candidates."""
    if (
        not isinstance(result, dict)
        or request["version"] != 1
        or type(result.get("version")) is not int
        or result.get("version") != 1
        or result.get("revision") != request["revision"]
        or (result.get("provider"), result.get("model")) != identity
        or not isinstance(result.get("assignments"), dict)
        or set(result["assignments"]) != set(request["candidates"])
    ):
        raise InvalidClassificationResult("Classification contract or identity changed")
    for field, values in result["assignments"].items():
        allowed = {candidate["id"] for candidate in request["candidates"][field]}
        if not isinstance(values, list) or any(
            type(value) is not int or value not in allowed for value in values
        ):
            raise InvalidClassificationResult(
                "Classification proposed forbidden values"
            )
