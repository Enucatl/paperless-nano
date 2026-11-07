"""Execute the two supported contracts using locally selected installed providers."""

import json
import os
from pathlib import Path

from django.utils.module_loading import import_string

from paperless.parsers.contract import configuration_snapshot
from paperless.parsers.contract import execution_snapshot
from paperless.parsers.contract import resolve_reference
from paperless.parsers.contract import validate_result


class ProviderMismatch(ValueError):
    """The selected local provider cannot execute the retained configuration."""


def compute(claim: dict, root: Path) -> dict:
    """Check retained identity and configuration around isolated computation."""
    request = json.loads(json.dumps(claim["input"], allow_nan=False))
    if claim["operation"] == "parser.process":
        provider = import_string(
            os.environ.get(
                "PAPERLESS_PARSER", "paperless_inference.providers.PaddleOCRProvider"
            )
        )()
        request = request["request"]
        identity = provider.identity
        if (
            request.get("version") != 1
            or claim["plugin"] != identity
            or request["settings"] != configuration_snapshot(provider, {})
        ):
            raise ProviderMismatch("Parser provider configuration changed")
        resolve_reference(root, request["source"])
        selection = execution_snapshot(provider, request, root)
        if claim["input"]["selection"] != selection:
            raise ProviderMismatch("Parser provider selection changed")
        runtime_request = json.loads(json.dumps(request))
        runtime_request["invocation_id"] = claim["id"]
        result = provider.process(runtime_request, root)
        validate_result(request, result, root, identity)
        if (
            provider.identity != identity
            or request["settings"] != configuration_snapshot(provider, {})
            or execution_snapshot(provider, request, root) != selection
            or result.get("execution", result["provider"]) != selection["identity"]
        ):
            raise ProviderMismatch("Parser provider changed during execution")
        return result
    if claim["operation"] == "metadata.enrich":
        from documents.enrichment import validate_result as validate_metadata

        path = os.environ.get(
            "PAPERLESS_ENRICHMENT_PROVIDER",
            "paperless_inference.providers.MetadataProvider",
        )
        provider = import_string(path)()
        identity = json.loads(json.dumps(provider.identity))
        allow_remote = request["configuration"].get("allow_remote")
        configuration = json.loads(
            json.dumps(
                {
                    "provider": path,
                    "configuration": provider.configuration,
                    "allow_remote": allow_remote,
                    "uses_remote_service": getattr(
                        provider, "uses_remote_service", True
                    ),
                    "replay_safe": getattr(provider, "replay_safe", False),
                },
                allow_nan=False,
            )
        )
        if (
            request.get("version") != 1
            or claim["plugin"] != identity["provider"]
            or request["identity"] != identity
            or request["configuration"] != configuration
            or type(allow_remote) is not bool
            or type(configuration["uses_remote_service"]) is not bool
            or type(configuration["replay_safe"]) is not bool
            or (configuration["uses_remote_service"] and not allow_remote)
        ):
            raise ProviderMismatch("Metadata provider configuration changed")
        result = provider.enrich(json.loads(json.dumps(request)))
        validate_metadata(request, result, provider.identity)
        if (
            provider.identity != identity
            or provider.configuration != configuration["configuration"]
            or getattr(provider, "uses_remote_service", True)
            != configuration["uses_remote_service"]
            or getattr(provider, "replay_safe", False) != configuration["replay_safe"]
        ):
            raise ProviderMismatch("Metadata provider changed during execution")
        return result
    raise ValueError("Unsupported workstation operation")
