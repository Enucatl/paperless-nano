"""Durable reprocessing with staged files and replayable completion.

Celery carries the job UUID. The database is the job journal; DATA_DIR/jobs is
private shared storage, independent of either process's scratch directory.
"""

import datetime
import logging
import shutil
from uuid import UUID
from uuid import uuid4
from uuid import uuid5

from django.conf import settings
from django.db import transaction
from django.db.models import CharField
from django.db.models import F
from django.db.models import Value
from django.db.models.functions import Cast
from django.db.models.functions import Replace
from django.utils import timezone
from filelock import FileLock
from filelock import Timeout

from documents.caching import clear_document_caches
from documents.file_handling import generate_unique_filename
from documents.file_handling import validate_path_in_root
from documents.job_state import RejectedResult
from documents.job_state import check_permission as _check_permission
from documents.job_state import document_revision as _revision
from documents.job_state import sync_terminal_task
from documents.job_state import track_terminal_task
from documents.job_storage import atomic_copy as _copy
from documents.job_storage import file_result as _file_result
from documents.job_storage import job_directory
from documents.job_storage import sync_directory as _sync_directory
from documents.job_storage import validate_file as _validate_file
from documents.language import detect_document_language
from documents.models import Document
from documents.models import DocumentBarcode
from documents.models import DocumentJob
from documents.models import PaperlessTask
from documents.parser_operations import effective_configuration as _configuration
from documents.parser_operations import parser_class as configured_parser_class
from documents.parser_operations import run_parser
from documents.parser_operations import validate_parser_job
from documents.processor_notifications import defer_notifications
from documents.utils import compute_checksum
from documents.versioning import latest_version
from paperless.config import RemoteOCRConfig
from paperless.parsers.contract import resolve_reference
from paperless.parsers.providers import configured_parser

logger = logging.getLogger("paperless.jobs")
MAX_ATTEMPTS = 3


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
    metadata: bool = False,
    thumbnail_only: bool = False,
) -> DocumentJob:
    """Snapshot a concrete document and its input before queue publication."""
    job_id = UUID(job_id) if job_id is not None else uuid4()
    # Legacy deliveries can race during admission as well as execution.
    with FileLock(
        job_directory(job_id).with_suffix(".lock"), timeout=0, is_singleton=True
    ):
        existing = DocumentJob.objects.filter(pk=job_id).first()
        if existing is not None:
            if (
                existing.kind != "reprocess"
                or existing.input.get("document_id") != document_id
                or existing.input.get("actor_id") != actor_id
                or existing.input.get("metadata", False) != metadata
                or existing.input.get("remote_ocr", False) != remote_ocr
                or existing.input.get("thumbnail_only", False) != thumbnail_only
            ):
                raise RejectedResult("Submission identifier belongs to another request")
            return existing
        document = Document.objects.get(pk=document_id)
        _check_permission(document, actor_id)
        root = document.root_document or document
        latest = latest_version(root)
        job = DocumentJob(
            id=job_id,
            status=DocumentJob.Status.STAGING,
            result={"invocation_contract": 1},
            timeout=settings.CELERY_TASK_TIME_LIMIT,
            configuration=_configuration(thumbnail_only=thumbnail_only),
            input={
                "document_id": document.pk,
                "root_id": root.pk,
                "latest_id": latest.pk,
                "actor_id": actor_id,
                "remote_ocr": remote_ocr,
                "metadata": metadata,
                "thumbnail_only": thumbnail_only,
                "trigger_source": trigger_source,
                "revision": _revision(document),
                "filename": document.original_filename or document.source_path.name,
                "staged_name": "input" + document.source_path.suffix,
                "source_path": str(document.source_path),
            },
        )
        job.save()
        try:
            if metadata:
                from documents.enrichment_operations import configured_provider
                from documents.enrichment_operations import provider_configuration
                from documents.operation_journal import json_value

                identity = configured_provider().identity
                if identity is None:
                    raise RejectedResult("No active metadata provider")
                job.input["metadata_provider"] = json_value(identity)
                job.input["metadata_configuration"] = json_value(
                    provider_configuration()
                )
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
    if job.kind == "enrich":
        from documents.enrichment_jobs import execute_enrichment

        execute_enrichment.apply_async(
            args=[str(job.pk)], task_id=str(job.pk), time_limit=job.timeout
        )
        return
    if job.kind == "search":
        from documents.search_operations import run_search

        run_search.apply_async(
            args=[str(job.pk)], task_id=str(job.pk), time_limit=job.timeout
        )
        return
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
    thumbnail_only: bool = False,
    trigger_source: str = PaperlessTask.TriggerSource.MANUAL,
) -> DocumentJob:
    """Durably admit work before best-effort Celery dispatch."""
    job = stage_reprocess(
        document_id,
        actor_id=actor_id,
        remote_ocr=remote_ocr,
        thumbnail_only=thumbnail_only,
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


def _prepare(job: DocumentJob) -> None:
    """Run the current parser and persist validated, serializable output."""
    from documents.barcodes import read_barcodes_for_reprocess

    document = _check_target(job)
    directory = job_directory(job.pk)
    source = directory / job.input["staged_name"]
    job.attempts += 1
    job.status = DocumentJob.Status.RUNNING
    job.updated = timezone.now()
    thumbnail_only = job.input.get("thumbnail_only", False)
    configuration = _configuration(thumbnail_only=thumbnail_only)
    if job.configuration and job.configuration != configuration:
        raise RejectedResult(
            "Processing configuration or provider changed before retry"
        )
    if not job.configuration:
        job.configuration = configuration
    job.result.setdefault("invocation_contract", 1)
    job.result["progress"] = "parsing"
    job.error = ""
    job.save()
    started = job.updated
    if compute_checksum(source) != job.input["checksum"]:
        raise RejectedResult("Staged input has changed")
    from paperless.parsers.providers import LocalThumbnailProvider

    parser_class = (
        LocalThumbnailProvider if thumbnail_only else configured_parser_class()
    )
    with configured_parser(parser_class, configuration) as parser:
        from documents.operation_journal import invoke

        def execute(identifier: str) -> dict:
            """Retain validated parser output before reprocessing publication."""
            return run_parser(
                parser,
                source,
                document.mime_type,
                job.input["filename"],
                configuration,
                directory,
                job_id=str(job.pk),
                operation="thumbnail" if thumbnail_only else "reprocess",
                invocation_id=identifier,
                allow_remote=not thumbnail_only
                and (
                    job.input["remote_ocr"] or RemoteOCRConfig().remote_ocr_by_default
                ),
            )

        parsed = invoke(
            job,
            "parser",
            configuration["parser"],
            {"configuration": configuration, "checksum": job.input["checksum"]},
            execute,
            replay_safe=True,
            allow_deferred=False,
        )
        job.refresh_from_db()
        result = {
            "invocation_contract": 1,
            "content": document.content if thumbnail_only else parsed["text"],
            "page_count": parsed["page_count"],
            "parser_request": job.result["parser_request"],
            "parser_result": parsed,
            "invocations": job.result["invocations"],
            "thumbnail": _file_result(
                directory,
                resolve_reference(directory, parsed["thumbnail"]),
                "thumbnail.webp",
            ),
            "archive": None,
        }
        if parsed["archive"] and not thumbnail_only:
            result["archive"] = _file_result(
                directory,
                resolve_reference(directory, parsed["archive"]),
                "archive.pdf",
            )
        # Barcode reading uses the durable input too, without changing the ORM row.
        document.filename = str(source)
        result["barcodes"] = (
            None if thumbnail_only else read_barcodes_for_reprocess(document)
        )
        _validate_file(directory, result["thumbnail"], "image/webp")
        if result["archive"]:
            _validate_file(directory, result["archive"], "application/pdf")
    if job.timeout and (timezone.now() - started).total_seconds() > job.timeout:
        raise TimeoutError("Reprocessing exceeded its recorded timeout")
    configuration = dict(job.configuration)
    if _configuration(thumbnail_only=thumbnail_only) != configuration:
        raise RejectedResult("Processing configuration changed during execution")
    job.result = result
    job.result["progress"] = "publication"
    job.status = DocumentJob.Status.PREPARED
    job.updated = timezone.now()
    job.save()


def _apply(job: DocumentJob) -> None:
    """Publish a new archive generation and atomically commit result ownership."""
    validate_parser_job(job)
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
            root_document = Document.objects.select_for_update().get(
                pk=job.input["root_id"]
            )
            # Conditional UPDATE is also a write lock on SQLite, where FOR UPDATE
            # alone cannot prevent another writer from changing the input revision.
            if not Document.objects.filter(
                pk=document.pk, **job.input["revision"]
            ).update(modified=F("modified")):
                raise RejectedResult("Document changed before result application")
            document = _check_target(job)
            media_lock.acquire()
            changes = {"content_job": job.pk}
            if not job.input.get("thumbnail_only"):
                changes["content"] = job.result["content"]
                if not document.language:
                    changes["language"] = (
                        root_document.language
                        or detect_document_language(job.result["content"])
                    )
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
            if not job.input.get("thumbnail_only"):
                changes["modified"] = timezone.now()
            Document.objects.filter(pk=document.pk).update(**changes)
            if document.root_document_id and not job.input.get("thumbnail_only"):
                Document.objects.filter(pk=root_document.pk, language="").update(
                    language=changes.get("language", document.language),
                    modified=timezone.now(),
                )
            if settings.AUDIT_LOG_ENABLED and not job.input.get("thumbnail_only"):
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
            from documents.ocr_provenance import publish_provenance

            publish_provenance(job, document)
            job.result["applied_revision"] = _revision(document)
            if job.input.get("metadata"):
                from documents.enrichment_operations import snapshot

                job.result["metadata_revision"] = snapshot(document)
            job.result["followup"] = (
                ["thumbnail", "cache"]
                if job.input.get("thumbnail_only")
                else ["thumbnail", "index", "cache"]
            )
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
    if not job.input.get("thumbnail_only"):
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
    if job.input.get("metadata") and "metadata_job" not in job.result:
        from documents.enrichment_jobs import submit_enrichment
        from documents.enrichment_operations import snapshot

        try:
            with transaction.atomic():
                document = Document.objects.select_for_update().get(
                    pk=job.input["document_id"]
                )
                child_id = uuid5(job.pk, "metadata")
                if not DocumentJob.objects.filter(pk=child_id).exists() and (
                    snapshot(document) != job.result["metadata_revision"]
                ):
                    raise RejectedResult("Document changed before metadata admission")
                child = submit_enrichment(
                    document.pk,
                    actor_id=job.input["actor_id"],
                    job_id=child_id,
                    expected_provider=job.input["metadata_provider"],
                    expected_configuration=job.input["metadata_configuration"],
                )
                job.result["metadata_job"] = str(child.pk)
                job.save(update_fields=["result"])
        except Exception as error:
            raise RejectedResult(
                f"OCR committed; metadata admission failed: {error}"
            ) from error
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


@defer_notifications()
@track_terminal_task
def run_reprocess(job_id: UUID | str) -> bool:
    """Execute or resume a job, independently of Celery lifecycle callbacks."""
    from documents.job_state import PendingOperation

    directory = job_directory(job_id)
    # Keep the lock outside staging; deleting a live lock creates two owners.
    lock = FileLock(directory.with_suffix(".lock"), timeout=0, is_singleton=True)
    try:
        lock.acquire()
    except Timeout:
        logger.info("Job %s is already executing", job_id)
        return False
    try:
        job = DocumentJob.objects.get(pk=job_id)
        if job.status in {DocumentJob.Status.WAITING, DocumentJob.Status.CANCELLED}:
            return False
        if job.status in {
            DocumentJob.Status.FAILED,
            DocumentJob.Status.REJECTED,
            DocumentJob.Status.ATTENTION,
        }:
            raise RejectedResult(job.error)
        if job.status == DocumentJob.Status.RUNNING and not job.result.get(
            "invocation_contract"
        ):
            job.status = DocumentJob.Status.ATTENTION
            job.error = "Legacy parser execution was interrupted; reconcile external effects before recovery"
            job.save(update_fields=["status", "error"])
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
        except PendingOperation:
            job.refresh_from_db()
            job.attempts = max(0, job.attempts - 1)
            job.save(update_fields=["attempts"])
            return False
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
            elif any(
                entry["status"] in {"unknown", "running"} and not entry["replay_safe"]
                for entry in job.result.get("invocations", {}).values()
            ):
                job.status = DocumentJob.Status.ATTENTION
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


def recover_document_jobs(*, include_recent: bool = False) -> None:
    """Republish unfinished durable work; workers serialize duplicate delivery."""
    from documents.deferred_operations import recover_waiting

    recover_waiting()
    terminal_jobs = DocumentJob.objects.alias(
        task_id=Replace(Cast("pk", CharField()), Value("-"), Value("")),
    ).filter(
        task_id__in=PaperlessTask.objects.filter(
            status__in=[PaperlessTask.Status.PENDING, PaperlessTask.Status.STARTED],
        )
        .annotate(
            job_id=Replace("task_id", Value("-"), Value("")),
        )
        .values("job_id"),
        status__in=[
            DocumentJob.Status.FAILED,
            DocumentJob.Status.REJECTED,
            DocumentJob.Status.ATTENTION,
        ],
    )
    for job in list(terminal_jobs):
        sync_terminal_task(job)
    cutoff = timezone.now() - datetime.timedelta(minutes=5)
    jobs = DocumentJob.objects.filter(cleaned=False).exclude(
        status__in=[
            DocumentJob.Status.FAILED,
            DocumentJob.Status.REJECTED,
            DocumentJob.Status.ATTENTION,
            DocumentJob.Status.WAITING,
            DocumentJob.Status.CANCELLED,
        ],
    )
    if include_recent:
        # Startup repairs fresh dispatch gaps without interrupting admission.
        jobs = jobs.exclude(status=DocumentJob.Status.STAGING, updated__gte=cutoff)
    else:
        jobs = jobs.filter(updated__lt=cutoff)
    # Publishing a task can close the connection and invalidate a server cursor.
    for identifier in list(jobs.values_list("pk", flat=True)):
        job = jobs.filter(pk=identifier).first()
        if job is None:
            continue
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
