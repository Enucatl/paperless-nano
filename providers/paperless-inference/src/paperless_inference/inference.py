"""Use the existing shared client with a bounded per-operation session."""

import asyncio
import os
import time
from pathlib import Path
from urllib.parse import urlsplit


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


class MissingOpenAIKey(ValueError):
    """Direct OpenAI requests require a dedicated OpenAI credential."""


async def complete(
    *,
    model: str,
    messages: list,
    endpoint: str,
    domain: str,
    lease_until: float | None = None,
    **kwargs,
):
    """Send completions through the shared client and bound Flex retries."""
    import niquests
    from shared_inference import InferenceClient
    from shared_inference.errors import InferenceError
    from shared_inference.errors import InferenceHTTPError
    from shared_inference.errors import InferenceTimeoutError

    host = urlsplit(endpoint).hostname or ""
    remote = host == "openrouter.ai" or host.endswith(".openrouter.ai")
    direct = host == "api.openai.com"
    key = read_secret("OPENROUTER_API_KEY" if remote else "OPENAI_API_KEY")
    if direct and not key:
        raise MissingOpenAIKey(
            "Set OPENAI_API_KEY_FILE or OPENAI_API_KEY for direct OpenAI"
        )
    if not (remote or direct) and not key:
        key = read_secret("OPENROUTER_API_KEY")

    kwargs.pop("metadata", None)
    flex = kwargs.get("service_tier") == "flex"
    attempt_timeout = 180 if flex else 120
    async with niquests.AsyncSession(timeout=attempt_timeout) as session:
        client = InferenceClient(
            base_url=endpoint,
            api_key=key,
            provider="openrouter" if remote else "openai-compatible",
            domain=domain,
            session=session,
        )
        if not flex:
            return await client.complete(model=model, messages=messages, **kwargs)

        budget = 550.0
        if lease_until is not None:
            # Reserve time for result processing, persistence and the 30s result POST.
            budget = min(budget, lease_until - time.time() - 60)
        if budget <= 0:
            raise TimeoutError("Insufficient operation lease for Flex inference")
        async with asyncio.timeout(budget):
            for delay in (2, 4, None):
                try:
                    async with asyncio.timeout(attempt_timeout):
                        result = await client.complete(
                            model=model, messages=messages, **kwargs
                        )
                except InferenceHTTPError as error:
                    details = (
                        error.raw.get("error") if isinstance(error.raw, dict) else None
                    )
                    code = details.get("code") if isinstance(details, dict) else None
                    if error.status_code not in {
                        408,
                        429,
                        500,
                        502,
                        503,
                        504,
                    } or code in ("insufficient_quota", "billing_hard_limit_reached"):
                        raise RuntimeError(
                            f"Flex request failed with HTTP {error.status_code}"
                        ) from None
                except TimeoutError, InferenceTimeoutError:
                    pass
                except InferenceError as error:
                    if not isinstance(
                        error.__cause__, niquests.ConnectionError
                    ) or isinstance(error.__cause__, niquests.exceptions.SSLError):
                        raise RuntimeError("Flex completion failed") from None
                else:
                    if result.raw.get("service_tier") != "flex":
                        raise RuntimeError(
                            "Flex completion did not confirm the requested service tier"
                        )
                    return result
                if delay is None:
                    raise RuntimeError("Flex completion exhausted three attempts")
                await asyncio.sleep(delay)
