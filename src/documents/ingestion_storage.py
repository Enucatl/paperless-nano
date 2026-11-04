"""Ownership proofs for publication outside the SQL transaction."""

import json
import os
from pathlib import Path

from django.conf import settings
from filelock import FileLock

from documents.job_state import RejectedResult
from documents.job_storage import atomic_copy as _copy
from documents.job_storage import file_result as _file_result
from documents.job_storage import job_directory
from documents.job_storage import sync_directory as _sync_directory
from documents.job_storage import validate_file as _validate_file
from documents.models import DocumentJob
from documents.parser_operations import effective_configuration as _configuration
from documents.utils import compute_checksum
from documents.utils import copy_basic_file_stats


def reconcile_publication(job: DocumentJob) -> None:
    """Remove only proven links owned by a rolled-back publication attempt."""
    from documents.operation_journal import NeedsAttention

    directory = job_directory(job.pk)
    manifest_path = directory / "publication.json"
    if not manifest_path.exists():
        return
    manifest = json.loads(manifest_path.read_text())
    with FileLock(settings.MEDIA_LOCK):
        for entry in manifest:
            if "owned" not in entry:
                raise NeedsAttention(
                    "Legacy publication lacks an ownership proof; inspect retained files"
                )
            target, owned = Path(entry["target"]), Path(entry["owned"])
            validate_target(target)
            if owned != target.with_name(f".paperless-{job.pk}-{target.name}"):
                raise RejectedResult("Invalid publication ownership path")
            if target.exists() or target.is_symlink():
                if (
                    target.is_symlink()
                    or not owned.exists()
                    or not target.samefile(owned)
                ):
                    raise NeedsAttention(
                        f"Publication target {target} belongs to another file"
                    )
                target.unlink()
                _sync_directory(target.parent)
            owned.unlink(missing_ok=True)
            owned.with_name(f".{owned.name}.job-part").unlink(missing_ok=True)
        manifest_path.unlink()
        _sync_directory(directory)


def validate_target(target: Path) -> None:
    """Require a publication path contained in configured document storage."""
    if target.is_symlink() or not any(
        target.resolve().is_relative_to(root.resolve())
        for root in (
            settings.ORIGINALS_DIR,
            settings.ARCHIVE_DIR,
            settings.THUMBNAIL_DIR,
        )
    ):
        raise RejectedResult("Publication target is outside document storage")


def publish_file(job_id: str, source: Path, target: Path) -> None:
    """Link a retained same-filesystem copy without replacing an existing target."""
    from documents.file_handling import validate_path_in_root

    directory = job_directory(job_id)
    validate_path_in_root(source, directory)
    validate_target(target)
    job = DocumentJob.objects.get(pk=job_id)
    reference = next(
        (
            value
            for value in job.result.values()
            if isinstance(value, dict) and value.get("name") == source.name
        ),
        None,
    )
    if reference is None or reference["checksum"] != compute_checksum(source):
        raise RejectedResult("Staged publication output changed after validation")
    owned = target.with_name(f".paperless-{job_id}-{target.name}")
    if target.exists() or owned.exists() or owned.is_symlink():
        raise RejectedResult("Publication would overwrite an existing file")
    record_publication(
        job_id,
        {
            "source": source.name,
            "target": str(target),
            "owned": str(owned),
            "checksum": reference["checksum"],
        },
    )
    _copy(source, owned)
    os.link(owned, target)
    _sync_directory(target.parent)


def cleanup_publication(job: DocumentJob) -> None:
    """Release storage's retained ownership links after a successful commit."""
    manifest_path = job_directory(job.pk) / "publication.json"
    if not manifest_path.exists():
        return
    for entry in json.loads(manifest_path.read_text()):
        if "owned" in entry:
            target, owned = Path(entry["target"]), Path(entry["owned"])
            validate_target(target)
            if owned != target.with_name(f".paperless-{job.pk}-{target.name}"):
                raise RejectedResult("Invalid publication ownership path")
            owned.unlink(missing_ok=True)
            owned.with_name(f".{owned.name}.job-part").unlink(missing_ok=True)
            _sync_directory(owned.parent)


def stage_outputs(
    job_id: str, *, original: Path, thumbnail: Path, archive: Path | None, text: str
) -> tuple[Path, Path, Path | None]:
    """Retain validated outputs before temporary parser cleanup or publication."""
    job = DocumentJob.objects.get(pk=job_id)
    directory = job_directory(job.pk)
    if not isinstance(text, str):
        raise RejectedResult("Parser text must be a string")
    result = {
        "original": _file_result(directory, original, "original"),
        "thumbnail": _file_result(directory, thumbnail, "thumbnail.webp"),
        "archive": _file_result(directory, archive, "archive.pdf") if archive else None,
        "content": text,
    }
    copy_basic_file_stats(original, directory / "original")
    _validate_file(directory, result["thumbnail"], "image/webp")
    if archive:
        _validate_file(directory, result["archive"], "application/pdf")
    for name, value in _configuration(mailrule_id=job.input["mailrule_id"]).items():
        if value != job.configuration[name]:
            raise RejectedResult("Processing configuration changed during ingestion")
    job.result.update(result)
    job.result["progress"] = "publication"
    job.save(update_fields=["result"])
    return (
        directory / "original",
        directory / "thumbnail.webp",
        directory / "archive.pdf" if archive else None,
    )


def record_publication(job_id: str, entry: dict) -> None:
    """Persist file publication or relocation intent independently of DB rollback."""
    import os

    directory = job_directory(job_id)
    manifest_path = directory / "publication.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else []
    manifest.append(entry)
    temporary = directory / "publication.pending"
    with temporary.open("w") as output:
        json.dump(manifest, output)
        output.flush()
        os.fsync(output.fileno())
    temporary.replace(manifest_path)
    _sync_directory(directory)
