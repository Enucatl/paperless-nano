"""Exercise installed adapters against core contracts and baseline responses."""

import json
import subprocess
import sys
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from paperless_inference.providers import MetadataProvider
from paperless_inference.providers import PaddleOCRProvider


@pytest.fixture
def metadata_request(monkeypatch):
    """Supply retained permitted candidates and effective metadata configuration."""
    monkeypatch.setenv("INFERENCE_METADATA_ENDPOINT", "http://metadata:8080/v1")
    monkeypatch.setenv("INFERENCE_METADATA_MODEL", "test-model-revision")
    provider = MetadataProvider()
    return {
        "version": 1,
        "revision": "retained",
        "identity": provider.identity,
        "content": "Invoice",
        "metadata": {"paperless_version": "3.4.1"},
        "candidates": {
            "correspondents": [{"id": 4, "name": "Example GmbH"}],
            "tags": [{"id": 3, "name": "language:de"}],
        },
        "managed_fields": {
            "summary": {"id": 1, "data_type": "longtext"},
            "processing_date": {"id": 2, "data_type": "date"},
            "audit": {"id": 3, "data_type": "longtext"},
        },
        "configuration": provider.configuration,
        "date": "2026-11-05",
        "document_date_max": "2026-11-05",
    }


def response(monkeypatch, content):
    """Replace transport only, preserving actual schema and normalization logic."""
    complete = AsyncMock(
        return_value=SimpleNamespace(message={"content": content}, reasoning=None)
    )
    monkeypatch.setattr("paperless_inference.metadata.complete", complete)
    return complete


def test_metadata_preserves_full_baseline(monkeypatch, metadata_request):
    """Normalize fields and preserve resolver diagnostics in a valid proposal."""
    complete = response(
        monkeypatch,
        json.dumps(
            {
                "title": "A" * 200,
                "date": "2024-02-29",
                "summary": "   ",
                "correspondent": "Example Ltd",
                "languages": ["bad", "DE", "en"],
            }
        ),
    )
    retained = deepcopy(metadata_request)
    result = MetadataProvider().enrich(metadata_request)
    assert result["title"] == "A" * 128
    assert result["document_date"] == "2024-02-29"
    assert result["summary"] == ""
    assert result["processing_date"] == "2026-11-05"
    assert result["correspondent"] == {"id": 4}
    assert result["language"] == {"id": 3}
    assert result["audit"]["correspondent_resolution"]["action"] == "existing"
    assert metadata_request == retained
    assert complete.call_args.kwargs["response_format"]["type"] == "json_schema"


@pytest.mark.parametrize("effort", [None, "", "high"])
def test_metadata_reasoning_default_and_override(monkeypatch, metadata_request, effort):
    """Preserve baseline minimal reasoning and explicit override or opt-out."""
    if effort is None:
        monkeypatch.delenv("INFERENCE_METADATA_REASONING_EFFORT", raising=False)
    else:
        monkeypatch.setenv("INFERENCE_METADATA_REASONING_EFFORT", effort)
    complete = response(monkeypatch, '{"title": "Invoice"}')
    MetadataProvider().enrich(metadata_request)
    expected = "minimal" if effort is None else effort
    if expected:
        assert complete.call_args.kwargs["reasoning_effort"] == expected
    else:
        assert "reasoning_effort" not in complete.call_args.kwargs


@pytest.mark.parametrize("invalid_date", ["not-a-date", "1899-12-31", "2099-01-01"])
def test_invalid_dates_preserve_other_metadata(
    monkeypatch, metadata_request, invalid_date
):
    """Invalid optional dates never discard valid summary or language proposals."""
    response(
        monkeypatch,
        json.dumps({"title": "Invoice", "date": invalid_date, "languages": []}),
    )
    result = MetadataProvider().enrich(metadata_request)
    assert "document_date" not in result
    assert result["title"] == "Invoice"
    assert result["language"] == {"create": "language:und"}


def test_empty_content_does_not_call_endpoint(monkeypatch, metadata_request):
    """An empty document produces no metadata mutations."""
    complete = response(monkeypatch, "{}")
    metadata_request["content"] = " \n "
    assert set(MetadataProvider().enrich(metadata_request)) == {
        "version",
        "revision",
        "identity",
    }
    complete.assert_not_called()


def test_metadata_matches_additional_properties_by_field_name(
    monkeypatch, metadata_request
):
    """Extra extractor properties reach existing fields without an ID allowlist."""
    metadata_request["managed_fields"].update(
        invoice_number={"id": 4, "data_type": "string"},
        reviewed={"id": 5, "data_type": "boolean"},
        omitted={"id": 6, "data_type": "integer"},
        unknown={"id": 7, "data_type": "float"},
    )
    response(
        monkeypatch,
        json.dumps(
            {
                "title": "Invoice",
                "invoice_number": "INV-123",
                "reviewed": False,
                "unknown": None,
                "unknown_property": "ignored",
            }
        ),
    )
    retained = deepcopy(metadata_request)
    result = MetadataProvider().enrich(metadata_request)
    assert result["custom_fields"] == [
        {"field": 4, "data_type": "string", "value": "INV-123"},
        {"field": 5, "data_type": "boolean", "value": False},
    ]
    assert metadata_request == retained


@pytest.mark.parametrize("policy", ["json_schema", "json_object", "none"])
def test_metadata_requests_existing_custom_fields(
    monkeypatch, metadata_request, policy
):
    """Strict schemas and every prompt expose retained extra field names and types."""
    monkeypatch.setenv("INFERENCE_METADATA_RESPONSE_FORMAT", policy)
    metadata_request["managed_fields"].update(
        invoice_number={"id": 4, "data_type": "string"},
        unsupported={"id": 5, "data_type": "documentlink"},
    )
    complete = response(monkeypatch, '{"invoice_number": "INV-123"}')
    MetadataProvider().enrich(metadata_request)
    kwargs = complete.call_args.kwargs
    assert (
        "invoice_number: Existing string custom field"
        in kwargs["messages"][0]["content"]
    )
    assert "unsupported:" not in kwargs["messages"][0]["content"]
    if policy == "json_schema":
        schema = kwargs["response_format"]["json_schema"]["schema"]
        assert schema["additionalProperties"] is False
        assert schema["properties"]["invoice_number"]["anyOf"] == [
            {"type": "string"},
            {"type": "null"},
        ]
        assert "invoice_number" in schema["required"]
        assert "audit" not in schema["properties"]
        assert "processing_date" not in schema["properties"]
        assert "unsupported" not in schema["properties"]


def test_metadata_ignores_unmatched_properties(monkeypatch, metadata_request):
    """A missing custom-field definition cannot reject valid ordinary metadata."""
    response(monkeypatch, '{"title": "Invoice", "invoice_number": "INV-123"}')
    result = MetadataProvider().enrich(metadata_request)
    assert result["title"] == "Invoice"
    assert "custom_fields" not in result


def test_metadata_accepts_additional_properties_without_baseline_fields(
    monkeypatch, metadata_request
):
    """A custom extractor may return only a named existing custom field."""
    metadata_request["managed_fields"]["invoice_number"] = {
        "id": 4,
        "data_type": "string",
    }
    response(monkeypatch, '{"invoice_number": "INV-123"}')
    result = MetadataProvider().enrich(metadata_request)
    assert result["custom_fields"] == [
        {"field": 4, "data_type": "string", "value": "INV-123"}
    ]


def test_endpoint_failure_propagates(monkeypatch, metadata_request):
    """Core receives the failed attempt instead of a fabricated success proposal."""
    complete = response(monkeypatch, "{}")
    complete.side_effect = TimeoutError("offline")
    with pytest.raises(TimeoutError, match="offline"):
        MetadataProvider().enrich(metadata_request)


def test_import_does_not_load_processing_libraries():
    """Discovery leaves model/client/pdf libraries unloaded."""
    subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import paperless_inference.providers; assert not {'pymupdf', 'pydantic', 'shared_inference', 'niquests', 'torch', 'onnxruntime'} & sys.modules.keys()",
        ],
        check=True,
    )


def test_secrets_cannot_enter_snapshot(monkeypatch, metadata_request):
    """Credential environment values are neither configuration nor identity."""
    monkeypatch.setenv("OPENAI_API_KEY", "not-in-snapshot")
    assert "not-in-snapshot" not in json.dumps(MetadataProvider().configuration)
    monkeypatch.setenv(
        "INFERENCE_METADATA_ENDPOINT", "https://user:password@example.com/v1"
    )
    with pytest.raises(ValueError, match="credentials"):
        MetadataProvider().configuration


def test_ocr_adapter_provenance_and_identity(monkeypatch, tmp_path):
    """Return source-bound provenance and reject service deployment drift."""
    from paperless_inference.files import file_reference
    from paperless_inference import paddle_ocr

    metadata = {"pipeline": "paddle", "model": "revision", "layout_model": "none"}
    monkeypatch.setenv("INFERENCE_OCR_ENDPOINT", "http://paddle:8080")
    for key, value in metadata.items():
        monkeypatch.setenv("INFERENCE_OCR_" + key.upper(), value)
    source = tmp_path / "source.pdf"
    source.write_bytes(b"%PDF-source")
    ref = file_reference(tmp_path, source)
    provenance = {
        "schema_version": 1,
        **metadata,
        "source_sha256": ref["sha256"],
        "page_count": 1,
        "pages": [],
    }
    fetch = AsyncMock(return_value=metadata)
    monkeypatch.setattr(paddle_ocr, "fetch_ocr_metadata", fetch)
    monkeypatch.setattr(
        paddle_ocr,
        "run_paddle_ocr",
        AsyncMock(return_value=("Text", provenance, 1, 0.1)),
    )
    provider = PaddleOCRProvider()
    request = {
        "version": 1,
        "revision": "snapshot",
        "source": ref,
        "mime_type": "application/pdf",
        "filename": "source.pdf",
        "settings": provider.configuration_snapshot(),
        "operation": "reprocess",
        "archive_policy": "always",
        "output_type": "pdfa",
        "output_dir": "output",
        "allow_remote": True,
    }
    result = provider.process(request, tmp_path)
    assert result["provenance"] == provenance
    assert result["text"] == "Text"
    fetch.return_value = {**metadata, "model": "changed"}
    with pytest.raises(ValueError, match="identity changed"):
        provider.process(request, tmp_path)
