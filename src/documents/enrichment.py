"""Versioned rich metadata proposals; authorization and persistence stay in core."""

from __future__ import annotations

import json
import re
from datetime import date
from typing import TYPE_CHECKING
from typing import NotRequired
from typing import Protocol
from typing import TypedDict

if TYPE_CHECKING:
    from documents.classification import Candidate


class EnrichmentIdentity(TypedDict):
    """Immutable implementation, model and prompt revisions, without credentials."""

    provider: str
    model: str
    prompt: str


class ManagedField(TypedDict):
    """An existing named custom field and its retained storage type."""

    id: int
    data_type: str


class EnrichmentRequest(TypedDict):
    """Retained content, permissions, effective settings and existing metadata."""

    version: int
    revision: str
    content: str
    metadata: dict
    candidates: dict[str, list[Candidate]]
    managed_fields: dict[str, ManagedField]
    configuration: dict
    identity: EnrichmentIdentity
    date: str
    document_date_max: str


class RelationProposal(TypedDict, total=False):
    """Exactly one permitted existing ID or an explicit creation name."""

    id: int
    create: str


class CustomFieldProposal(TypedDict):
    """Replace an existing field using its retained ID and storage type."""

    field: int
    data_type: str
    value: str | int | float | bool


class EnrichmentResult(TypedDict):
    """Optional replacements; omitted fields preserve their existing values."""

    version: int
    revision: str
    identity: EnrichmentIdentity
    title: NotRequired[str]
    document_date: NotRequired[str]
    summary: NotRequired[str]
    processing_date: NotRequired[str]
    audit: NotRequired[dict]
    correspondent: NotRequired[RelationProposal]
    language: NotRequired[RelationProposal]
    custom_fields: NotRequired[list[CustomFieldProposal]]


class EnrichmentProvider(Protocol):
    """Compute rich metadata without database or document-write authority."""

    identity: EnrichmentIdentity
    configuration: dict

    def enrich(self, request: EnrichmentRequest) -> EnrichmentResult:
        """Return a proposal for the exact retained request."""
        ...


class InvalidEnrichmentResult(ValueError):
    """A malformed, incompatible or unauthorized proposal cannot be applied."""


def _reject(message: str) -> None:
    """Raise the public boundary error."""
    raise InvalidEnrichmentResult(message)


def _date(value: object) -> date:
    """Require the canonical ISO date representation."""
    if not isinstance(value, str):
        _reject("Date must be a YYYY-MM-DD string")
    try:
        parsed = date.fromisoformat(value)
    except ValueError as error:
        raise InvalidEnrichmentResult("Invalid date") from error
    if parsed.isoformat() != value:
        _reject("Date must be a YYYY-MM-DD string")
    return parsed


def validate_result(
    request: EnrichmentRequest,
    result: EnrichmentResult,
    identity: EnrichmentIdentity,
) -> None:
    """Check the wire contract; core must recheck current access before commit.

    Args:
        request: Core-owned retained admission snapshot.
        result: Untrusted provider proposal.
        identity: Currently selected immutable provider/model/prompt identity.

    Raises:
        InvalidEnrichmentResult: The result cannot safely be applied.
    """
    if (
        type(request.get("version")) is not int
        or request["version"] != 1
        or not isinstance(result, dict)
        or type(result.get("version")) is not int
        or result.get("version") != 1
        or result.get("revision") != request["revision"]
        or result.get("identity") != request["identity"]
        or result.get("identity") != identity
        or not isinstance(identity, dict)
        or set(identity) != {"provider", "model", "prompt"}
        or any(not isinstance(value, str) or not value for value in identity.values())
        or not {"version", "revision", "identity"} <= result.keys()
        or result.keys() - EnrichmentResult.__annotations__.keys()
    ):
        _reject("Enrichment contract, revision or identity changed")
    try:
        json.dumps(result, allow_nan=False)
    except (TypeError, ValueError) as error:
        raise InvalidEnrichmentResult(
            "Enrichment result must be finite JSON"
        ) from error
    if not request["content"].strip() and result.keys() - {
        "version",
        "revision",
        "identity",
    }:
        _reject("Empty content cannot produce metadata changes")
    if "title" in result and (
        not isinstance(result["title"], str) or not 1 <= len(result["title"]) <= 128
    ):
        _reject("Title must contain 1 to 128 characters")
    if "document_date" in result and not (
        date(1900, 1, 1)
        <= _date(result["document_date"])
        <= _date(request["document_date_max"])
    ):
        _reject("Document date is outside the retained date bounds")
    for role, expected_types in {
        "summary": {"string", "longtext"},
        "processing_date": {"date"},
        "audit": {"string", "longtext"},
    }.items():
        if role not in result:
            continue
        field = request["managed_fields"].get(role)
        if not field:
            continue
        if field["data_type"] not in expected_types:
            _reject("Incompatible managed field")
        value = (
            json.dumps(result[role], ensure_ascii=False)
            if role == "audit"
            else result[role]
        )
        if (
            field["data_type"] == "string"
            and isinstance(value, str)
            and len(value) > 128
        ):
            _reject("Managed string exceeds its storage limit")
    if "summary" in result and not isinstance(result["summary"], str):
        _reject("Summary must be a string; blank means clear")
    if "processing_date" in result:
        _date(result["processing_date"])
        if result["processing_date"] != request["date"]:
            _reject("Processing date must match the retained UTC date")
    if "audit" in result and not isinstance(result["audit"], dict):
        _reject("Metadata audit must be a JSON object")
    for key, candidates in (("correspondent", "correspondents"), ("language", "tags")):
        if key not in result:
            continue
        proposal = result[key]
        if not isinstance(proposal, dict) or set(proposal) not in ({"id"}, {"create"}):
            _reject("Relation must propose exactly one ID or creation")
        if "id" in proposal:
            permitted = {
                item["id"]: item["name"] for item in request["candidates"][candidates]
            }
            value = proposal["id"]
            if type(value) is not int or value not in permitted:
                _reject("Relation ID is not a permitted candidate")
            name = permitted[value]
        else:
            name = proposal["create"]
            if not isinstance(name, str) or not name.strip() or len(name) > 128:
                _reject("Creation name must contain 1 to 128 characters")
        if key == "language" and not re.fullmatch(r"language:[a-z]{2,3}", name):
            _reject("Language must name one language tag")
    fields = result.get("custom_fields", [])
    if not isinstance(fields, list):
        _reject("Custom fields must be a list")
    allowed = {
        item["id"]: item["data_type"]
        for role, item in request["managed_fields"].items()
        if role not in {"summary", "processing_date", "audit"}
    }
    seen = set()
    for field in fields:
        if (
            not isinstance(field, dict)
            or set(field) != {"field", "data_type", "value"}
            or type(field["field"]) is not int
            or field["field"] not in allowed
            or field["field"] in seen
            or field["data_type"] != allowed[field["field"]]
        ):
            _reject("Custom field is unknown, duplicated or has changed type")
        seen.add(field["field"])
        kind, value = field["data_type"], field["value"]
        if kind == "string" and isinstance(value, str) and len(value) > 128:
            _reject("Custom string exceeds its storage limit")
        if kind == "date":
            _date(value)
        elif not (
            (kind in {"string", "longtext"} and isinstance(value, str))
            or (kind == "boolean" and type(value) is bool)
            or (kind == "integer" and type(value) is int)
            or (kind == "float" and type(value) in {int, float})
        ):
            _reject("Unsupported custom field type or malformed value")
