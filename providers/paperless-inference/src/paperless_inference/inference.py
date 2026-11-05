"""Use the existing shared client with a bounded per-operation session."""

import os


async def complete(*, model: str, messages: list, endpoint: str, domain: str, **kwargs):
    """Preserve shared inference normalization and close its HTTP session."""
    import niquests
    from shared_inference import InferenceClient

    remote = "openrouter.ai" in endpoint
    key = (
        os.environ.get("OPENROUTER_API_KEY")
        if remote
        else (os.environ.get("OPENAI_API_KEY") or os.environ.get("OPENROUTER_API_KEY"))
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
