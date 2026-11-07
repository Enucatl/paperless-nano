"""Core-owned operation descriptions for independently packaged HTTP workers."""

from pathlib import Path

PARSER_CONTRACT = "paperless.external.parser:v1"
METADATA_CONTRACT = {"provider": "paperless.external.metadata", "contract": 1}


def is_metadata_contract(identity: object) -> bool:
    """Recognize this exact contract without treating a boolean as version one."""
    return (
        isinstance(identity, dict)
        and identity == METADATA_CONTRACT
        and type(identity.get("contract")) is int
    )


def validate_worker(worker: dict) -> None:
    """Validate reported execution provenance without prescribing an implementation."""
    if (
        not isinstance(worker, dict)
        or "implementation" not in worker
        or worker.keys() - {"implementation", "model", "prompt"}
        or any(
            not isinstance(value, str) or not value.strip() for value in worker.values()
        )
    ):
        raise ValueError("Worker provenance must identify its implementation")


class ExternalParser:
    """Describe parser work without loading an OCR engine into the server."""

    identity = PARSER_CONTRACT
    uses_remote_service = True
    replay_safe = True

    def configuration_snapshot(self) -> dict:
        """Keep model endpoints, credentials and tuning entirely on the worker."""
        return {}

    def selection(self, request: dict, root: Path) -> dict:
        """Require external execution of this versioned result contract."""
        return {
            "identity": self.identity,
            "uses_remote_service": True,
            "replay_safe": True,
            "external_only": True,
        }

    def process(self, request: dict, root: Path) -> dict:
        """Reject direct execution; only a leased worker can compute this operation."""
        raise RuntimeError(
            "External parser work must be claimed through the operation API"
        )


class ExternalMetadata:
    """Describe metadata proposals without choosing a model or prompt."""

    identity = METADATA_CONTRACT
    configuration = {}
    uses_remote_service = True
    replay_safe = True
    external_only = True

    def enrich(self, request: dict) -> dict:
        """Reject direct execution; core only validates and applies worker proposals."""
        raise RuntimeError(
            "External metadata work must be claimed through the operation API"
        )
