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
    execution: NotRequired[str]
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

    def configuration_for(self, context: dict) -> dict:
        """Snapshot configuration for core-supplied document context."""
        return self.configuration_snapshot()

    def selection(self, request: ParserRequest, root: Path) -> dict:
        """Declare effective execution identity and conservative replay policy."""
        return {
            "identity": self.identity,
            "replay_safe": getattr(self, "replay_safe", False),
            "uses_remote_service": self.uses_remote_service,
        }

    def process(self, request: ParserRequest, root: Path) -> ParserResult:
        """Return output references within this operation's private directory."""
        ...


class InvalidParserResult(ValueError):
    """An incompatible, stale, missing or unsafe result cannot be published."""


def configuration_snapshot(provider: ParserProvider, context: dict) -> dict:
    """Validate and detach an implementation's effective configuration."""
    method = getattr(provider, "configuration_for", None)
    value = method(context) if method else provider.configuration_snapshot()
    if not isinstance(value, dict):
        raise InvalidParserResult("Parser configuration must be an object")
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise InvalidParserResult("Parser configuration must be JSON") from error


def execution_snapshot(
    provider: ParserProvider, request: dict, root: Path, *, retained: dict | None = None
) -> dict:
    """Validate declarations equally for direct providers and internal engines."""
    identity = getattr(provider, "identity", None)
    method = getattr(provider, "selection", None)
    value = (
        method(json.loads(json.dumps(request)), root)
        if method
        else {
            "identity": identity,
            "replay_safe": getattr(provider, "replay_safe", False),
            "uses_remote_service": getattr(provider, "uses_remote_service", True),
        }
    )
    if (
        not isinstance(identity, str)
        or not identity
        or not isinstance(value, dict)
        or not isinstance(value.get("identity"), str)
        or not value["identity"]
        or type(value.get("replay_safe")) is not bool
        or type(value.get("uses_remote_service")) is not bool
    ):
        raise InvalidParserResult("Invalid parser execution declaration")
    mode = value.get("execution_mode", getattr(provider, "execution_mode", None))
    if mode is not None:
        if mode not in {"local", "external"}:
            raise InvalidParserResult("Parser execution_mode must be local or external")
        value = {**value, "execution_mode": mode}
    # Pre-migration selections retain their original routing outside this snapshot.
    if retained is not None and "execution_mode" not in retained:
        value.pop("execution_mode", None)
        mode = None
    if (value["uses_remote_service"] or mode == "external") and not request[
        "allow_remote"
    ]:
        raise InvalidParserResult(
            "Remote processing is not permitted for this document"
        )
    try:
        return json.loads(json.dumps(value, allow_nan=False))
    except (TypeError, ValueError) as error:
        raise InvalidParserResult(
            "Parser execution declaration must be JSON"
        ) from error


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
