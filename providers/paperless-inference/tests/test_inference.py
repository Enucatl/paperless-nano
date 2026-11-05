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
