"""Serializable parser inputs and results, independent of Django and execution."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from typing import NotRequired
from typing import Protocol
from typing import TypedDict


class FileReference(TypedDict):
    """A content-checked file relative to the core's private operation directory."""

    path: str
    sha256: str
    size: int


class ParserRequest(TypedDict):
    """An immutable-by-convention source and effective configuration snapshot."""

    version: int
    revision: str
    source: FileReference
    mime_type: str
    filename: str
    settings: dict
    operation: str
    archive_policy: str
    output_type: str
    output_dir: str
    allow_remote: bool
    invocation_id: NotRequired[str]


class ParserResult(TypedDict):
    """Proposed content and private renditions; the core owns publication."""

    version: int
    revision: str
    provider: str
    text: str
    page_count: int | None
    date: str | None
    archive: FileReference | None
    thumbnail: FileReference | None
    archive_type: str | None


class ParserProvider(Protocol):
    """Processing interface without database objects or permanent file paths."""

    identity: str
    uses_remote_service: bool

    def configuration_snapshot(self) -> dict:
        """Return JSON settings without secrets, checked on retry and publication."""
        return {}

    def process(self, request: ParserRequest, root: Path) -> ParserResult:
        """Return output references within this operation's private directory."""
        ...


class InvalidParserResult(ValueError):
    """An incompatible, stale, missing or unsafe result cannot be published."""


def relative_path(root: Path, name: str) -> Path:
    """Resolve a contained relative path, rejecting links and traversal."""
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise InvalidParserResult("Parser file reference must be relative")
    relative = Path(name)
    if ".." in relative.parts:
        raise InvalidParserResult("Parser file reference escapes its operation")
    path = root.resolve()
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise InvalidParserResult("Parser file references cannot contain symlinks")
    if not path.resolve().is_relative_to(root.resolve()):
        raise InvalidParserResult("Parser file reference escapes its operation")
    return path


def file_reference(root: Path, path: Path) -> FileReference:
    """Describe an existing private file without depending on worker scratch."""
    try:
        name = str(path.absolute().relative_to(root.resolve()))
    except ValueError as error:
        raise InvalidParserResult("Parser file is outside its operation") from error
    path = relative_path(root, name)
    if not path.is_file():
        raise InvalidParserResult("Parser file is missing or not a regular file")
    with path.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": name, "sha256": checksum, "size": path.stat().st_size}


def resolve_reference(root: Path, reference: FileReference) -> Path:
    """Check file identity before consuming or publishing provider output."""
    if (
        not isinstance(reference, dict)
        or set(reference) != {"path", "sha256", "size"}
        or type(reference.get("size")) is not int
        or reference["size"] < 0
        or not isinstance(reference.get("sha256"), str)
    ):
        raise InvalidParserResult("Invalid parser file reference")
    path = relative_path(root, reference["path"])
    if file_reference(root, path) != reference:
        raise InvalidParserResult("Parser file changed after it was referenced")
    return path


def validate_result(
    request: ParserRequest,
    result: ParserResult,
    root: Path,
    identity: str,
) -> None:
    """Reject malformed results and changed or unsafe files."""
    if (
        request["version"] != 1
        or not isinstance(result, dict)
        or type(result.get("version")) is not int
        or result.get("version") != 1
        or result.get("revision") != request["revision"]
        or result.get("provider") != identity
        or not isinstance(result.get("text"), str)
        or "page_count" not in result
        or (
            result["page_count"] is not None
            and (type(result["page_count"]) is not int or result["page_count"] < 1)
        )
        or not {"date", "archive", "thumbnail", "archive_type"} <= result.keys()
    ):
        raise InvalidParserResult("Invalid parser result or provider identity")
    try:
        json.dumps(result, allow_nan=False)
        if result["date"] is not None:
            datetime.fromisoformat(result["date"])
    except (TypeError, ValueError) as error:
        raise InvalidParserResult("Parser result is not serializable") from error

    source = resolve_reference(root, request["source"])
    output_dir = relative_path(root, request["output_dir"])
    for key in ("archive", "thumbnail"):
        reference = result[key]
        if reference is None:
            continue
        path = resolve_reference(root, reference)
        if not path.is_relative_to(output_dir) or path == source:
            raise InvalidParserResult("Parser output is outside its output directory")
        if path.stat().st_size == 0 or path.samefile(source):
            raise InvalidParserResult("Parser output is empty or aliases its source")

    archive_type = result["archive_type"]
    if (result["archive"] is None and archive_type is not None) or (
        result["archive"] is not None and archive_type not in {"searchable_pdf", "pdfa"}
    ):
        raise InvalidParserResult("Parser archive type is invalid")
