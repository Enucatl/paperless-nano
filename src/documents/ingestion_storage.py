"""Ownership proofs for publication outside the SQL transaction."""

import json
import os
from pathlib import Path

from django.conf import settings
from filelock import FileLock

from documents.jobs import RejectedResult
from documents.jobs import _copy
from documents.jobs import _sync_directory
from documents.jobs import job_directory
from documents.models import DocumentJob
from documents.utils import compute_checksum


def reconcile_publication(job: DocumentJob) -> None:
    """Remove only proven links owned by a rolled-back publication attempt."""
    from documents.ingestion_jobs import NeedsAttention

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
    from documents.ingestion_jobs import record_publication

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
