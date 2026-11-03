"""Durable reprocessing with staged files and replayable completion.

Celery carries the job UUID. The database is the job journal; DATA_DIR/jobs is
private shared storage, independent of either process's scratch directory.
"""

import dataclasses
import datetime
import logging
import os
import shutil
from pathlib import Path
from uuid import UUID
from uuid import uuid4

from django.conf import settings
from django.contrib.auth.models import User
from django.db import transaction
from django.db.models import F
from django.utils import timezone
from filelock import FileLock
from filelock import Timeout

from documents.caching import clear_document_caches
from documents.file_handling import generate_unique_filename
from documents.file_handling import validate_path_in_root
from documents.models import Document
from documents.models import DocumentBarcode
from documents.models import DocumentJob
from documents.models import PaperlessTask
from documents.permissions import permitted_document_ids
from documents.utils import compute_checksum
from documents.versioning import latest_version
from paperless.config import BarcodeConfig
from paperless.config import OcrConfig
from paperless.config import RemoteOCRConfig
from paperless.parsers import ParserContext
from paperless.parsers.registry import get_parser_registry

logger = logging.getLogger("paperless.jobs")
MAX_ATTEMPTS = 3
REVISION_FIELDS = (
    "content",
    "checksum",
    "archive_checksum",
    "archive_filename",
    "filename",
    "modified",
    "mime_type",
    "root_document_id",
    "content_job",
)


class RejectedResult(Exception):
    """The input, authorization, or output no longer permits application."""


def job_directory(job_id: UUID | str) -> Path:
    """Return the private durable directory for a validated job identifier."""
    root = settings.DATA_DIR / "jobs"
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root / str(UUID(str(job_id)))


def _sync_directory(path: Path) -> None:
    """Persist a rename before recording its completion in the database."""
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _copy(source: Path, destination: Path) -> None:
    """Atomically publish a copy while keeping the recoverable source."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.job-part")
    with source.open("rb") as src, temporary.open("wb") as dst:
        shutil.copyfileobj(src, dst)
        dst.flush()
        os.fsync(dst.fileno())
    temporary.replace(destination)
    _sync_directory(destination.parent)


def _revision(document: Document) -> dict:
    """Capture fields whose replacement would invalidate a parsing result."""
    revision = {field: getattr(document, field) for field in REVISION_FIELDS}
    revision["modified"] = document.modified.isoformat()
    revision["content_job"] = (
        str(document.content_job) if document.content_job else None
    )
    return revision


def _check_permission(document: Document, actor_id: int | None) -> None:
    """Recheck the original caller; None explicitly denotes a trusted CLI job."""
    if actor_id is None:
        return
    actor = User.objects.filter(pk=actor_id, is_active=True).first()
    if (
        actor is None
        or not actor.has_perm("documents.change_document")
        or not permitted_document_ids(actor, perm="change_document")
        .filter(pk=document.pk)
        .exists()
    ):
        raise RejectedResult("The requesting user can no longer change this document")


def _check_target(job: DocumentJob) -> Document:
    """Reject deleted, superseded, edited, or newly forbidden input."""
    document = Document.objects.filter(pk=job.input["document_id"]).first()
    if document is None or _revision(document) != job.input["revision"]:
        raise RejectedResult("Document changed or was deleted after job admission")
    root = Document.objects.filter(pk=job.input["root_id"]).first()
    if root is None or latest_version(root).pk != job.input["latest_id"]:
        raise RejectedResult(
            "The document's current version changed after job admission"
        )
    _check_permission(document, job.input["actor_id"])
    return document


def stage_reprocess(
    document_id: int,
    *,
    actor_id: int | None = None,
    remote_ocr: bool = False,
    trigger_source: str = PaperlessTask.TriggerSource.MANUAL,
    job_id: str | None = None,
) -> DocumentJob:
    """Snapshot a concrete document and its input before queue publication."""
    job_id = UUID(job_id) if job_id is not None else uuid4()
    # Legacy deliveries can race during admission as well as execution.
    with FileLock(job_directory(job_id).with_suffix(".lock"), timeout=0):
        existing = DocumentJob.objects.filter(pk=job_id).first()
        if existing is not None:
            return existing
        document = Document.objects.get(pk=document_id)
        _check_permission(document, actor_id)
        root = document.root_document or document
        latest = latest_version(root)
        job = DocumentJob(
            id=job_id,
            status=DocumentJob.Status.STAGING,
            timeout=settings.CELERY_TASK_TIME_LIMIT,
            input={
                "document_id": document.pk,
                "root_id": root.pk,
                "latest_id": latest.pk,
                "actor_id": actor_id,
                "remote_ocr": remote_ocr,
                "trigger_source": trigger_source,
                "revision": _revision(document),
                "filename": document.original_filename or document.source_path.name,
                "staged_name": "input" + document.source_path.suffix,
                "source_path": str(document.source_path),
            },
        )
        job.save()
        try:
            with FileLock(settings.MEDIA_LOCK):
                _check_target(job)
                directory = job_directory(job.pk)
                directory.mkdir(mode=0o700, exist_ok=True)
                _copy(document.source_path, directory / job.input["staged_name"])
                job.input["checksum"] = compute_checksum(
                    directory / job.input["staged_name"]
                )
                # A writer need not take MEDIA_LOCK (e.g. a queryset content update).
                document.refresh_from_db()
                if _revision(document) != job.input["revision"]:
                    raise RejectedResult("Document changed while staging its input")
            job.status = DocumentJob.Status.QUEUED
            job.save(update_fields=["status", "input"])
        except Exception as error:
            job.status = DocumentJob.Status.REJECTED
            job.error = str(error)
            job.save(update_fields=["status", "error"])
            raise
        return job


def _dispatch(job: DocumentJob) -> None:
    """Send only serializable identifiers; a lost send remains recoverable."""
    if job.kind == "consume":
        from documents.ingestion_jobs import dispatch_consumption

        dispatch_consumption(job)
        return
    from documents.tasks import update_document_content_maybe_archive_file

    update_document_content_maybe_archive_file.apply_async(
        kwargs={"document_id": job.input["document_id"], "job_id": str(job.pk)},
        task_id=str(job.pk),
        time_limit=job.timeout,
        headers={"trigger_source": job.input["trigger_source"]},
    )


def submit_reprocess(
    document_id: int,
    *,
    actor_id: int | None = None,
    remote_ocr: bool = False,
    trigger_source: str = PaperlessTask.TriggerSource.MANUAL,
) -> DocumentJob:
    """Durably admit work before best-effort Celery dispatch."""
    job = stage_reprocess(
        document_id,
        actor_id=actor_id,
        remote_ocr=remote_ocr,
        trigger_source=trigger_source,
    )

    def dispatch() -> None:
        """Leave a failed broker publication queued for the recovery sweep."""
        try:
            _dispatch(job)
        except Exception:
            logger.exception("Job %s is durable but its dispatch failed", job.pk)

    transaction.on_commit(dispatch)
    return job


def _configuration(parser: object = None, *, mailrule_id: int | None = None) -> dict:
    """Record effective built-in settings without persisting remote credentials."""
    remote = dataclasses.asdict(RemoteOCRConfig())
    remote.pop("remote_ocr_api_key")
    ocr = getattr(parser, "settings", None)
    if not isinstance(ocr, OcrConfig):
        ocr = OcrConfig()
    configuration = {
        "ocr": dataclasses.asdict(ocr),
        "barcodes": dataclasses.asdict(BarcodeConfig()),
        "remote": remote,
        "execution": {
            name: getattr(settings, name)
            for name in (
                "THREADS_PER_WORKER",
                "THUMBNAIL_FONT_NAME",
                "TIKA_ENDPOINT",
                "TIKA_GOTENBERG_ENDPOINT",
                "REMOTE_OCR_ALLOW_INTERNAL_ENDPOINTS",
                "EMAIL_PARSE_DEFAULT_LAYOUT",
                "OCR_OUTPUT_TYPE",
            )
        },
    }
    if mailrule_id:
        from paperless_mail.models import MailRule

        configuration["mail_layout"] = (
            MailRule.objects.filter(pk=mailrule_id)
            .values_list("pdf_layout", flat=True)
            .first()
        )
    return configuration


def _file_result(directory: Path, source: Path, name: str) -> dict:
    """Retain a parser output after its temporary context has exited."""
    destination = directory / name
    _copy(source, destination)
    return {"name": name, "checksum": compute_checksum(destination)}


def _validate_file(directory: Path, reference: dict, mime_type: str) -> Path:
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

        with pikepdf.open(path) as pdf:
            if not pdf.pages:
                raise RejectedResult("Parser returned an empty PDF")
    else:
        from PIL import Image

        with Image.open(path) as image:
            image.verify()
    return path


def _prepare(job: DocumentJob) -> None:
    """Run the current parser and persist validated, serializable output."""
    from documents.consumer import should_produce_archive
    from documents.tasks import _read_barcodes_for_reprocess

    document = _check_target(job)
    directory = job_directory(job.pk)
    source = directory / job.input["staged_name"]
    job.attempts += 1
    job.status = DocumentJob.Status.RUNNING
    job.updated = timezone.now()
    job.configuration = _configuration()
    job.result["progress"] = "parsing"
    job.error = ""
    job.save()
    started = job.updated
    if compute_checksum(source) != job.input["checksum"]:
        raise RejectedResult("Staged input has changed")
    parser_class = get_parser_registry().get_parser_for_file(
        document.mime_type,
        job.input["filename"],
        source,
        allow_remote=job.input["remote_ocr"] or RemoteOCRConfig().remote_ocr_by_default,
    )
    if parser_class is None:
        raise RejectedResult(f"No parser for {document.mime_type}")
    job.provider = {
        "class": f"{parser_class.__module__}.{parser_class.__qualname__}",
        "name": parser_class.name,
        "version": parser_class.version,
    }
    job.save(update_fields=["provider"])
    with parser_class() as parser:
        parser.configure(ParserContext())
        produce_archive = should_produce_archive(parser, document.mime_type, source)
        job.configuration = _configuration(parser)
        job.configuration["produce_archive"] = produce_archive
        job.save(update_fields=["configuration"])
        parser.parse(source, document.mime_type, produce_archive=produce_archive)
        text = parser.get_text()
        if not isinstance(text, str):
            raise RejectedResult("Parser text must be a string")
        result = {
            "content": text,
            "thumbnail": _file_result(
                directory,
                Path(parser.get_thumbnail(source, document.mime_type)),
                "thumbnail.webp",
            ),
            "archive": None,
        }
        archive = parser.get_archive_path()
        if archive:
            result["archive"] = _file_result(directory, Path(archive), "archive.pdf")
        elif produce_archive:
            raise RejectedResult("Parser omitted the required PDF rendition")
        # Barcode reading uses the durable input too, without changing the ORM row.
        document.filename = str(source)
        result["barcodes"] = _read_barcodes_for_reprocess(document)
        _validate_file(directory, result["thumbnail"], "image/webp")
        if result["archive"]:
            _validate_file(directory, result["archive"], "application/pdf")
    if job.timeout and (timezone.now() - started).total_seconds() > job.timeout:
        raise TimeoutError("Reprocessing exceeded its recorded timeout")
    configuration = dict(job.configuration)
    configuration.pop("produce_archive")
    if _configuration() != configuration:
        raise RejectedResult("Processing configuration changed during execution")
    job.result = result
    job.result["progress"] = "publication"
    job.status = DocumentJob.Status.PREPARED
    job.updated = timezone.now()
    job.save()


def _apply(job: DocumentJob) -> None:
    """Publish a new archive generation and atomically commit result ownership."""
    directory = job_directory(job.pk)
    _validate_file(directory, job.result["thumbnail"], "image/webp")
    archive = job.result["archive"]
    archive_source = (
        _validate_file(directory, archive, "application/pdf") if archive else None
    )
    with FileLock(settings.MEDIA_LOCK, timeout=0):
        document = _check_target(job)
        if archive and "archive_filename" not in job.result:
            # Never overwrite the prior archive before the new DB state commits.
            document.archive_filename = None
            filename = generate_unique_filename(document, archive_filename=True)
            if (
                len(str(filename))
                > Document._meta.get_field("archive_filename").max_length
            ):
                raise RejectedResult("Archive filename exceeds the database limit")
            validate_path_in_root(settings.ARCHIVE_DIR / filename, settings.ARCHIVE_DIR)
            job.result["archive_filename"] = str(filename)
    job.save(update_fields=["result"])
    media_lock = FileLock(settings.MEDIA_LOCK, timeout=0)
    try:
        with transaction.atomic():
            Document.objects.select_for_update().get(pk=job.input["root_id"])
            # Conditional UPDATE is also a write lock on SQLite, where FOR UPDATE
            # alone cannot prevent another writer from changing the input revision.
            if not Document.objects.filter(
                pk=document.pk, **job.input["revision"]
            ).update(modified=F("modified")):
                raise RejectedResult("Document changed before result application")
            document = _check_target(job)
            media_lock.acquire()
            changes = {"content": job.result["content"], "content_job": job.pk}
            if archive:
                destination = settings.ARCHIVE_DIR / job.result["archive_filename"]
                validate_path_in_root(destination, settings.ARCHIVE_DIR)
                if Document.global_objects.filter(
                    archive_filename=job.result["archive_filename"]
                ).exists():
                    raise RejectedResult(
                        "Archive destination is already owned by a document"
                    )
                if destination.exists():
                    if compute_checksum(destination) != archive["checksum"]:
                        raise RejectedResult(
                            "Archive destination contains another file"
                        )
                else:
                    _copy(archive_source, destination)
                changes.update(
                    archive_filename=job.result["archive_filename"],
                    archive_checksum=archive["checksum"],
                )
            if job.result["barcodes"] is not None:
                document.barcodes.all().delete()
                DocumentBarcode.objects.bulk_create(
                    DocumentBarcode(document=document, **barcode)
                    for barcode in job.result["barcodes"]
                )
            changes["modified"] = timezone.now()
            Document.objects.filter(pk=document.pk).update(**changes)
            if settings.AUDIT_LOG_ENABLED:
                from auditlog.models import LogEntry

                LogEntry.objects.log_create(
                    instance=document,
                    changes={
                        key: [getattr(document, key), value]
                        for key, value in changes.items()
                        if key not in {"modified", "content_job"}
                    },
                    additional_data={"reason": "Update document content"},
                    action=LogEntry.Action.UPDATE,
                )
            document.refresh_from_db()
            job.result["applied_revision"] = _revision(document)
            job.result["followup"] = ["thumbnail", "index", "cache"]
            job.result["progress"] = "completion"
            job.status = DocumentJob.Status.COMMITTED
            job.updated = timezone.now()
            job.save()
    finally:
        media_lock.release()


def _finish(job: DocumentJob) -> None:
    """Resume idempotent completion recorded in the document's commit."""
    from paperless.config import AIConfig

    with transaction.atomic():
        document = Document.objects.filter(pk=job.input["document_id"]).first()
        if document is not None:
            Document.objects.filter(pk=document.pk).update(modified=F("modified"))
            document.refresh_from_db()
            if (
                document.content_job == job.pk
                and document.checksum == job.input["revision"]["checksum"]
            ):
                source = _validate_file(
                    job_directory(job.pk), job.result["thumbnail"], "image/webp"
                )
                with FileLock(settings.MEDIA_LOCK, timeout=0):
                    validate_path_in_root(
                        document.thumbnail_path, settings.THUMBNAIL_DIR
                    )
                    _copy(source, document.thumbnail_path)
    from documents.search import get_backend

    # Read after acquiring the writer so a delayed completion cannot reindex a
    # snapshot fetched before a newer operation finished indexing.
    with get_backend().batch_update() as batch:
        document = Document.objects.filter(pk=job.input["root_id"]).first()
        if document is None:
            batch.remove(job.input["root_id"])
        else:
            batch.add_or_update(document)
    if document is not None and AIConfig().llm_index_enabled:
        from paperless_ai.indexing import llm_index_add_or_update_document

        if llm_index_add_or_update_document(document) is False:
            raise RuntimeError("AI indexing is waiting for an index migration")
    clear_document_caches(job.input["document_id"])
    if job.input["root_id"] != job.input["document_id"]:
        clear_document_caches(job.input["root_id"])
    job.result["followup"] = []
    job.status = DocumentJob.Status.SUCCEEDED
    job.error = ""
    job.updated = timezone.now()
    job.save()


def _cleanup(job: DocumentJob) -> None:
    """Delete staging only after durable success; interruptions are retryable."""
    with FileLock(settings.MEDIA_LOCK):
        document = Document.objects.filter(pk=job.input["document_id"]).first()
        if document is not None:
            if not document.source_path.is_file():
                raise OSError("Original file missing; staged input retained")
            if (
                document.checksum == job.input["revision"]["checksum"]
                and compute_checksum(document.source_path) != job.input["checksum"]
            ):
                raise OSError(
                    "Original file changed unexpectedly; staged input retained"
                )
        old_name = job.input["revision"]["archive_filename"]
        if job.result["archive"] and old_name:
            old_path = (settings.ARCHIVE_DIR / old_name).resolve()
            if (
                old_path.is_relative_to(settings.ARCHIVE_DIR.resolve())
                and not Document.global_objects.filter(
                    archive_filename=old_name
                ).exists()
                and old_path.is_file()
                and compute_checksum(old_path)
                == job.input["revision"]["archive_checksum"]
            ):
                old_path.unlink()
                _sync_directory(old_path.parent)
    directory = job_directory(job.pk)
    if directory.exists():
        shutil.rmtree(directory)
        _sync_directory(directory.parent)
    job.cleaned = True
    job.error = ""
    job.result["progress"] = "done"
    job.save(update_fields=["cleaned", "error", "result"])


def run_reprocess(job_id: UUID | str) -> bool:
    """Execute or resume a job, independently of Celery lifecycle callbacks."""
    directory = job_directory(job_id)
    # Keep the lock outside staging; deleting a live lock creates two owners.
    lock = FileLock(directory.with_suffix(".lock"), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        logger.info("Job %s is already executing", job_id)
        return False
    try:
        job = DocumentJob.objects.get(pk=job_id)
        if job.status in {DocumentJob.Status.FAILED, DocumentJob.Status.REJECTED}:
            raise RejectedResult(job.error)
        try:
            if job.status in {
                DocumentJob.Status.QUEUED,
                DocumentJob.Status.RUNNING,
            }:
                if job.attempts >= MAX_ATTEMPTS:
                    job.status = DocumentJob.Status.FAILED
                    job.error = "Processing exhausted three attempts; input retained"
                    job.save()
                    raise RejectedResult(job.error)
                _prepare(job)
            if job.status == DocumentJob.Status.PREPARED:
                _apply(job)
            if job.status == DocumentJob.Status.COMMITTED:
                _finish(job)
            if job.status == DocumentJob.Status.SUCCEEDED and not job.cleaned:
                _cleanup(job)
            return True
        except Exception as error:
            # A commit can succeed even if its acknowledgment is lost.
            job.refresh_from_db()
            job.error = str(error)
            if isinstance(error, RejectedResult) and job.status not in {
                DocumentJob.Status.COMMITTED,
                DocumentJob.Status.SUCCEEDED,
                DocumentJob.Status.FAILED,
            }:
                job.status = DocumentJob.Status.REJECTED
            elif job.status == DocumentJob.Status.RUNNING:
                job.status = (
                    DocumentJob.Status.FAILED
                    if job.attempts >= MAX_ATTEMPTS
                    else DocumentJob.Status.QUEUED
                )
                if job.status == DocumentJob.Status.FAILED:
                    job.error = (
                        f"Processing exhausted three attempts: {error}; input retained"
                    )
            job.updated = timezone.now()
            job.save(update_fields=["status", "error", "updated"])
            raise
    finally:
        lock.release()


def recover_document_jobs() -> None:
    """Republish unfinished durable work; workers serialize duplicate delivery."""
    cutoff = timezone.now() - datetime.timedelta(minutes=5)
    jobs = DocumentJob.objects.filter(updated__lt=cutoff, cleaned=False).exclude(
        status__in=[
            DocumentJob.Status.FAILED,
            DocumentJob.Status.REJECTED,
            DocumentJob.Status.ATTENTION,
        ],
    )
    for job in jobs.iterator():
        if job.status == DocumentJob.Status.STAGING:
            job.status = DocumentJob.Status.ATTENTION
            job.error = (
                "Admission interrupted; inspect staged input and source before recovery"
            )
            job.save(update_fields=["status", "error"])
            continue
        if (
            job.status == DocumentJob.Status.RUNNING
            and job.updated + datetime.timedelta(seconds=job.timeout) > timezone.now()
        ):
            continue
        if job.kind == "callback":
            if job.status == DocumentJob.Status.QUEUED:
                # The parent owns dispatch, including safe AI timeout retries.
                continue
            job.status = DocumentJob.Status.ATTENTION
            job.error = "Callback execution was interrupted; reconcile its effects before recovery"
            job.save(update_fields=["status", "error"])
            continue
        try:
            _dispatch(job)
        except Exception:
            logger.exception("Could not dispatch durable job %s", job.pk)
