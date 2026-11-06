"""Pure contract tests independent of Django and inference dependencies."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from documents.enrichment import InvalidEnrichmentResult
from documents.enrichment import validate_result


@pytest.fixture
def example():
    """Load the sanitized retained invoice and proposal."""
    return json.loads(
        (Path(__file__).parent / "fixtures/enrichment/invoice.json").read_text()
    )


def test_sanitized_example_roundtrips(example):
    """The documented request/result is valid after durable JSON transport."""
    validate_result(
        example["request"], example["result"], example["request"]["identity"]
    )


@pytest.mark.parametrize(
    ("key", "value"),
    [
        ("version", True),
        ("version", 2),
        ("revision", "stale"),
        ("identity", {"provider": "different", "model": "m", "prompt": "p"}),
        ("title", None),
        ("title", ""),
        ("title", "x" * 129),
        ("document_date", "1899-12-31"),
        ("document_date", "2027-01-01"),
        ("document_date", "2026-02-30"),
        ("document_date", "20261001"),
        ("summary", None),
        ("processing_date", "2026-11-06"),
        ("audit", []),
        ("audit", {"score": float("nan")}),
        ("correspondent", {"id": True}),
        ("correspondent", {"id": 999}),
        ("correspondent", {"id": 7, "create": "Example"}),
        ("correspondent", {"create": " "}),
        ("language", {"id": 5}),
        ("language", {"create": "language:EN"}),
        ("language", {"create": "inbox"}),
        ("custom_fields", [{"field": 90, "data_type": "longtext", "value": "bad"}]),
        ("custom_fields", [{"field": 10, "data_type": "longtext", "value": "bad"}]),
        ("custom_fields", [{"field": 13, "data_type": "boolean", "value": 1}]),
        ("custom_fields", [{"field": 13, "data_type": "integer", "value": 1}]),
        ("unexpected", "must not be silently ignored"),
    ],
)
def test_rejects_malformed_or_forbidden_proposals(example, key, value):
    """Reject untrusted values before any write takes place."""
    example["result"][key] = value
    with pytest.raises(InvalidEnrichmentResult):
        validate_result(
            example["request"], example["result"], example["request"]["identity"]
        )


def test_preserve_and_blank_summary(example):
    """Omission preserves; a supplied blank summary explicitly clears it."""
    result = {
        key: example["result"][key] for key in ("version", "revision", "identity")
    }
    validate_result(example["request"], result, example["request"]["identity"])
    result["summary"] = ""
    validate_result(example["request"], result, example["request"]["identity"])


def test_empty_content_only_allows_noop(example):
    """Empty documents cannot acquire invented metadata or processing stamps."""
    example["request"]["content"] = " \n"
    with pytest.raises(InvalidEnrichmentResult):
        validate_result(
            example["request"], example["result"], example["request"]["identity"]
        )
    result = {
        key: example["result"][key] for key in ("version", "revision", "identity")
    }
    validate_result(example["request"], result, example["request"]["identity"])


def test_rejects_duplicate_custom_field(example):
    """Duplicate writes cannot depend on provider list order."""
    example["result"]["custom_fields"] *= 2
    with pytest.raises(InvalidEnrichmentResult):
        validate_result(
            example["request"], example["result"], example["request"]["identity"]
        )


def test_rejects_changed_current_identity(example):
    """An old result cannot be accepted after a prompt or model swap."""
    current = deepcopy(example["request"]["identity"])
    current["prompt"] = "new-prompt"
    with pytest.raises(InvalidEnrichmentResult):
        validate_result(example["request"], example["result"], current)


@pytest.mark.parametrize("role", ["summary", "processing_date", "audit"])
def test_missing_field_definition_is_ignored(example, role):
    """Missing definitions leave valid metadata usable without creating schema."""
    del example["request"]["managed_fields"][role]
    validate_result(
        example["request"], example["result"], example["request"]["identity"]
    )


@pytest.mark.parametrize("relation", [{"id": 8}, {"create": "language:und"}])
def test_existing_and_fallback_language(example, relation):
    """Permit one existing language or explicit undefined-language creation."""
    example["result"]["language"] = relation
    validate_result(
        example["request"], example["result"], example["request"]["identity"]
    )


@pytest.mark.parametrize("role", ["summary", "audit"])
def test_string_storage_limit(example, role):
    """Reject values which a managed short-string column cannot store."""
    example["request"]["managed_fields"][role]["data_type"] = "string"
    example["result"][role] = "x" * 129 if role == "summary" else {"text": "x" * 129}
    with pytest.raises(InvalidEnrichmentResult):
        validate_result(
            example["request"], example["result"], example["request"]["identity"]
        )


@pytest.mark.parametrize(
    ("kind", "value"),
    [("string", "ok"), ("date", "2026-01-01"), ("integer", 42), ("float", 1.5)],
)
def test_additional_managed_field_types(example, kind, value):
    """Additional fields use retained types and ordinary JSON scalar values."""
    example["request"]["managed_fields"]["reviewed"]["data_type"] = kind
    example["result"]["custom_fields"] = [
        {"field": 13, "data_type": kind, "value": value}
    ]
    validate_result(
        example["request"], example["result"], example["request"]["identity"]
    )


def test_local_document_date_bound_is_separate_from_utc_processing_date(example):
    """Near midnight, local date bounds do not change the UTC processing stamp."""
    example["request"]["document_date_max"] = "2026-11-06"
    example["result"]["document_date"] = "2026-11-06"
    validate_result(
        example["request"], example["result"], example["request"]["identity"]
    )
