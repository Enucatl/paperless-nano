"""Small provider configuration without web, queue or database dependencies."""

import json
import os
from dataclasses import dataclass
from importlib.resources import files
from urllib.parse import urlsplit


def endpoint(name: str) -> str:
    """Require an explicit HTTP endpoint without embedded credentials."""
    value = os.environ.get(name, "").rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{name} must name an HTTP endpoint")
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError(f"{name} must not contain credentials, query or fragment")
    return value


@dataclass
class ProviderConfig:
    """Retain only the inference settings consumed by the extracted callables."""

    ocr_endpoint: str = ""
    ocr_timeout: float = 600
    metadata_endpoint: str = ""
    metadata_model: str = ""
    metadata_prompt: str = ""
    metadata_response_format: str = "auto"
    metadata_kwargs: dict | None = None

    @classmethod
    def metadata(cls) -> "ProviderConfig":
        """Read metadata configuration, rejecting credential-bearing snapshots."""
        model = os.environ.get("INFERENCE_METADATA_MODEL", "")
        if not model:
            raise ValueError("INFERENCE_METADATA_MODEL is required")
        policy = os.environ.get("INFERENCE_METADATA_RESPONSE_FORMAT", "auto")
        if policy not in {"auto", "json_schema", "json_object", "none"}:
            raise ValueError("Invalid INFERENCE_METADATA_RESPONSE_FORMAT")
        kwargs = {
            "max_tokens": int(os.environ.get("INFERENCE_METADATA_MAX_TOKENS", "1000"))
        }
        if "INFERENCE_METADATA_TEMPERATURE" in os.environ:
            kwargs["temperature"] = float(os.environ["INFERENCE_METADATA_TEMPERATURE"])
        effort = os.environ.get("INFERENCE_METADATA_REASONING_EFFORT", "minimal")
        if effort:
            kwargs["reasoning_effort"] = effort
        extra = json.loads(os.environ.get("INFERENCE_METADATA_EXTRA_KWARGS", "{}"))
        if not isinstance(extra, dict) or extra.keys() - {
            "temperature",
            "top_p",
            "top_k",
            "max_tokens",
            "seed",
            "stop",
            "frequency_penalty",
            "presence_penalty",
            "reasoning_effort",
        }:
            raise ValueError(
                "Unsupported metadata extra kwargs; credentials stay in environment"
            )
        kwargs.update(extra)
        json.dumps(kwargs, allow_nan=False)
        return cls(
            metadata_endpoint=endpoint("INFERENCE_METADATA_ENDPOINT"),
            metadata_model=model.removeprefix("openrouter/").removeprefix("openai/"),
            metadata_prompt=files("paperless_inference")
            .joinpath("metadata_prompt.txt")
            .read_text(),
            metadata_response_format=policy,
            metadata_kwargs=kwargs,
        )

    def get_metadata_kwargs(self) -> dict:
        """Return a detached copy of the effective completion parameters."""
        return dict(self.metadata_kwargs or {})
