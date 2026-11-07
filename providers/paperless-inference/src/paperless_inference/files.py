"""Verify private file references in the external worker protocol."""

import hashlib
from pathlib import Path


def relative_path(root: Path, name: str) -> Path:
    """Resolve a contained relative path, rejecting links and traversal."""
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise ValueError("Parser file reference must be relative")
    relative = Path(name)
    if ".." in relative.parts:
        raise ValueError("Parser file reference escapes its operation")
    path = root.resolve()
    for part in relative.parts:
        path = path / part
        if path.is_symlink():
            raise ValueError("Parser file references cannot contain symlinks")
    if not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("Parser file reference escapes its operation")
    return path


def file_reference(root: Path, path: Path) -> dict:
    """Describe an existing private file without depending on worker scratch."""
    try:
        name = str(path.absolute().relative_to(root.resolve()))
    except ValueError as error:
        raise ValueError("Parser file is outside its operation") from error
    path = relative_path(root, name)
    if not path.is_file():
        raise ValueError("Parser file is missing or not a regular file")
    with path.open("rb") as stream:
        checksum = hashlib.file_digest(stream, "sha256").hexdigest()
    return {"path": name, "sha256": checksum, "size": path.stat().st_size}


def resolve_reference(root: Path, reference: dict) -> Path:
    """Check file identity before consuming or publishing provider output."""
    if (
        not isinstance(reference, dict)
        or set(reference) != {"path", "sha256", "size"}
        or type(reference.get("size")) is not int
        or reference["size"] < 0
        or not isinstance(reference.get("sha256"), str)
    ):
        raise ValueError("Invalid parser file reference")
    path = relative_path(root, reference["path"])
    if file_reference(root, path) != reference:
        raise ValueError("Parser file changed after it was referenced")
    return path
