"""Use the existing shared client with a bounded per-operation session."""

import os
from pathlib import Path


def read_secret(name: str) -> str | None:
    """Prefer a Docker secret file, retaining the baseline environment fallback."""
    filename = os.environ.get(name + "_FILE")
    if filename:
        try:
            path = Path(filename)
            if path.is_file():
                return path.read_text().strip() or os.environ.get(name)
        except OSError, ValueError:
            pass
    return os.environ.get(name)


async def complete(*, model: str, messages: list, endpoint: str, domain: str, **kwargs):
    """Preserve shared inference normalization and close its HTTP session."""
    import niquests
    from shared_inference import InferenceClient

    remote = "openrouter.ai" in endpoint
    key = (
        read_secret("OPENROUTER_API_KEY")
        if remote
        else (read_secret("OPENAI_API_KEY") or read_secret("OPENROUTER_API_KEY"))
    )
    kwargs.pop("metadata", None)
    async with niquests.AsyncSession(timeout=120) as session:
        client = InferenceClient(
            base_url=endpoint,
            api_key=key,
            provider="openrouter" if remote else "openai-compatible",
            domain=domain,
            session=session,
        )
        return await client.complete(model=model, messages=messages, **kwargs)
