"""Preserve the deployed runner's Docker-secret authentication contract."""

import pytest

from paperless_inference.inference import read_secret
from paperless_inference.providers import MetadataProvider


@pytest.mark.parametrize("name", ["OPENAI_API_KEY", "OPENROUTER_API_KEY"])
def test_file_secret_precedes_environment_without_entering_snapshot(
    monkeypatch, tmp_path, name
):
    """Use mounted credentials at execution without retaining their bytes."""
    secret = tmp_path / "api-key"
    secret.write_text("  mounted-secret\n")
    monkeypatch.setenv(name, "environment-secret")
    monkeypatch.setenv(name + "_FILE", str(secret))
    monkeypatch.setenv("INFERENCE_METADATA_ENDPOINT", "https://example.com/v1")
    monkeypatch.setenv("INFERENCE_METADATA_MODEL", "test-model")
    assert read_secret(name) == "mounted-secret"
    snapshot = repr(MetadataProvider().configuration)
    assert "mounted-secret" not in snapshot
    assert "environment-secret" not in snapshot
    secret.write_text("rotated-secret")
    assert read_secret(name) == "rotated-secret"


def test_missing_secret_file_preserves_environment_fallback(monkeypatch, tmp_path):
    """Retain the previous runner's fallback when a mount is absent."""
    monkeypatch.setenv("OPENAI_API_KEY_FILE", str(tmp_path / "missing"))
    monkeypatch.setenv("OPENAI_API_KEY", "fallback-secret")
    assert read_secret("OPENAI_API_KEY") == "fallback-secret"
    monkeypatch.delenv("OPENAI_API_KEY")
    assert read_secret("OPENAI_API_KEY") is None


def test_blank_secret_preserves_existing_environment(monkeypatch, tmp_path):
    """The previous runner injected only nonempty file secrets into the environment."""
    secret = tmp_path / "blank"
    secret.write_text(" \n")
    monkeypatch.setenv("OPENAI_API_KEY_FILE", str(secret))
    monkeypatch.setenv("OPENAI_API_KEY", "fallback-secret")
    assert read_secret("OPENAI_API_KEY") == "fallback-secret"


@pytest.mark.parametrize(
    ("outcomes", "delays", "error"),
    [
        ([429, "flex"], [2], None),
        (["transport", 503, "flex"], [2, 4], None),
        ([429, 429, 429], [2, 4], "exhausted"),
        ([400], [], "HTTP 400"),
        (["default"], [], "did not confirm"),
        ([None], [], "did not confirm"),
    ],
)
async def test_flex_payload_and_retries(monkeypatch, tmp_path, outcomes, delays, error):
    """Exercise actual shared-client serialization and retry classification."""
    import asyncio
    from unittest.mock import AsyncMock
    from unittest.mock import Mock

    import niquests

    from paperless_inference.providers import MetadataProvider

    key = tmp_path / "key"
    key.write_text("dedicated-key")
    for name, value in {
        "OPENAI_API_KEY_FILE": str(key),
        "INFERENCE_METADATA_ENDPOINT": "https://api.openai.com/v1",
        "INFERENCE_METADATA_MODEL": "configured-model",
        "INFERENCE_METADATA_SERVICE_TIER": "flex",
        "INFERENCE_METADATA_REASONING_EFFORT": "none",
        "INFERENCE_METADATA_TEMPERATURE": "omit",
        "INFERENCE_METADATA_MAX_COMPLETION_TOKENS": "123",
        "INFERENCE_METADATA_EXTRA_KWARGS": '{"max_tokens": 456}',
    }.items():
        monkeypatch.setenv(name, value)

    from paperless_inference.config import ProviderConfig
    from paperless_inference.metadata import StructuredOutputStrategy

    replies = []
    for outcome in outcomes:
        if outcome == "transport":
            replies.append(niquests.ConnectionError("offline"))
            continue
        response = niquests.Response()
        response.status_code = outcome if isinstance(outcome, int) else 200
        response.json = lambda tier=outcome: {
            "choices": [{"message": {"content": '{"title":"Invoice"}'}}],
            **({"service_tier": tier} if tier is not None else {}),
        }
        replies.append(response)
    post = AsyncMock(side_effect=replies)
    sleep = AsyncMock()
    timeout = Mock(wraps=asyncio.timeout)
    monkeypatch.setattr(asyncio, "timeout", timeout)
    monkeypatch.setattr(niquests.AsyncSession, "post", post)
    monkeypatch.setattr("paperless_inference.inference.asyncio.sleep", sleep)

    strategy = StructuredOutputStrategy()
    config = ProviderConfig.metadata()
    if error:
        with pytest.raises(RuntimeError, match=error):
            await strategy.extract("document", config)
    else:
        assert (await strategy.extract("document", config)).title == "Invoice"
        assert strategy.inference_audit["observed_service_tier"] == "flex"
        assert strategy.inference_audit["requested_service_tier"] == "flex"
    assert "dedicated-key" not in repr(MetadataProvider().configuration)
    assert [call.args[0] for call in sleep.await_args_list] == delays
    assert post.await_count == len(outcomes)
    assert [call.args[0] for call in timeout.call_args_list] == [550] + [180] * len(
        outcomes
    )
    for call in post.await_args_list:
        assert call.args == ("https://api.openai.com/v1/chat/completions",)
        assert call.kwargs["headers"]["Authorization"] == "Bearer dedicated-key"
        payload = call.kwargs["json"]
        assert payload["model"] == "configured-model"
        assert payload["service_tier"] == "flex"
        assert payload["reasoning_effort"] == "none"
        assert payload["max_completion_tokens"] == 123
        assert payload["response_format"]["type"] == "json_schema"
        assert "max_tokens" not in payload
        assert "temperature" not in payload


async def test_direct_openai_needs_its_own_key(monkeypatch):
    """Keep an OpenRouter credential out of direct requests."""
    from paperless_inference.inference import MissingOpenAIKey, complete

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY_FILE", raising=False)
    monkeypatch.setenv("OPENROUTER_API_KEY", "router-key")
    with pytest.raises(MissingOpenAIKey, match="OPENAI_API_KEY_FILE"):
        await complete(
            model="test",
            messages=[],
            endpoint="https://api.openai.com/v1",
            domain="test",
        )


async def test_short_lease_leaves_delivery_reserve(monkeypatch):
    """Refuse to start Flex when the actual lease has no inference budget."""
    import time
    from unittest.mock import AsyncMock

    import niquests

    from paperless_inference.inference import complete

    post = AsyncMock()
    monkeypatch.setattr(niquests.AsyncSession, "post", post)
    with pytest.raises(TimeoutError, match="Insufficient operation lease"):
        await complete(
            model="test",
            messages=[],
            endpoint="https://example.com/v1",
            domain="test",
            service_tier="flex",
            lease_until=time.time() + 59,
        )
    post.assert_not_called()


async def test_lease_budget_interrupts_inflight_request(monkeypatch):
    """Enforce the lease-derived budget even when transport has not timed out."""
    import asyncio
    import time
    from unittest.mock import AsyncMock

    import niquests

    from paperless_inference.inference import complete

    async def stalled(*args, **kwargs):
        await asyncio.Future()

    post = AsyncMock(side_effect=stalled)
    monkeypatch.setattr(niquests.AsyncSession, "post", post)
    with pytest.raises(TimeoutError):
        await complete(
            model="test",
            messages=[],
            endpoint="https://example.com/v1",
            domain="test",
            service_tier="flex",
            lease_until=time.time() + 60.1,
        )
    post.assert_awaited_once()
