"""Validated durable job paths and atomic file storage."""

import os
import shutil
from pathlib import Path
from uuid import UUID

from django.conf import settings

from documents.file_handling import validate_path_in_root
from documents.job_state import RejectedResult
from documents.utils import compute_checksum


def job_directory(job_id: UUID | str) -> Path:
    """Return the private durable directory for a validated job identifier."""
    root = settings.DATA_DIR / "jobs"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root / str(UUID(str(job_id)))


def sync_directory(path: Path) -> None:
    """Persist a rename before recording its completion in the database."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_copy(source: Path, destination: Path) -> None:
    """Atomically publish a copy while keeping the recoverable source."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.job-part")
    with source.open("rb") as src, temporary.open("wb") as dst:
        shutil.copyfileobj(src, dst)
        dst.flush()
        os.fsync(dst.fileno())
    temporary.replace(destination)
    sync_directory(destination.parent)


def file_result(directory: Path, source: Path, name: str) -> dict:
    """Retain a parser output after its temporary context has exited."""
    destination = directory / name
    atomic_copy(source, destination)
    return {"name": name, "checksum": compute_checksum(destination)}


def validate_file(directory: Path, reference: dict, mime_type: str) -> Path:
    """Check containment, integrity, and decodability before publication."""
    import magic

    path = directory / reference["name"]
    validate_path_in_root(path, directory)
    if (
        path.is_symlink()
        or not path.is_file()
        or compute_checksum(path) != reference["checksum"]
    ):
        raise RejectedResult("Staged output is missing or has changed")
    if magic.from_file(path, mime=True) != mime_type:
        raise RejectedResult(f"Parser output is not {mime_type}")
    if mime_type == "application/pdf":
        import pikepdf

        try:
            with pikepdf.open(path) as pdf:
                if not pdf.pages:
                    raise RejectedResult("Parser returned an empty PDF")
        except pikepdf.PdfError as error:
            raise RejectedResult("Parser returned an invalid PDF") from error
    else:
        from PIL import Image

        try:
            with Image.open(path) as image:
                image.verify()
        except (OSError, ValueError) as error:
            raise RejectedResult("Parser returned an invalid thumbnail") from error
    return path
