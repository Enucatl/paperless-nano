"""Durable ingestion admission and conservative recovery of external effects."""

import dataclasses
import datetime
import json
import logging
import shutil
from collections.abc import Callable
from pathlib import Path
from uuid import UUID
from uuid import uuid4

from celery import signature
from celery.contrib.django.task import DjangoTask
from celery.exceptions import Ignore
from django.conf import settings
from django.contrib.auth.models import User
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.utils import timezone
from filelock import FileLock
from filelock import Timeout
from kombu.utils.json import dumps as transport_dumps
from kombu.utils.json import loads as transport_loads

from documents.data_models import ConsumableDocument
from documents.data_models import DocumentMetadataOverrides
from documents.data_models import DocumentSource
from documents.file_handling import validate_path_in_root
from documents.job_context import active_ingestion_job
from documents.jobs import RejectedResult
from documents.jobs import _check_permission
from documents.jobs import _configuration
from documents.jobs import _copy
from documents.jobs import _file_result
from documents.jobs import _revision
from documents.jobs import _sync_directory
from documents.jobs import _validate_file
from documents.jobs import job_directory
from documents.models import Document
from documents.models import DocumentJob
from documents.utils import compute_checksum
from documents.utils import copy_basic_file_stats
from documents.versioning import latest_version

logger = logging.getLogger("paperless.jobs")


class NeedsAttention(Exception):
    """An external effect may have happened and must not be repeated automatically."""


def _json(value: object) -> object:
    """Normalize model inputs to JSON primitives, including dates and decimal values."""
    return json.loads(json.dumps(value, cls=DjangoJSONEncoder))


def stage_consumption(
    input_doc: ConsumableDocument,
    overrides: DocumentMetadataOverrides | None = None,
    *,
    job_id: str | None = None,
    dispatch_options: dict | None = None,
) -> DocumentJob:
    """Own a durable input copy before any producer publishes its task."""
    identifier = UUID(job_id) if job_id else uuid4()
    directory = job_directory(identifier)
    with FileLock(directory.with_suffix(".lock"), timeout=0):
        existing = DocumentJob.objects.filter(pk=identifier).first()
        if existing:
            return existing
        metadata = dataclasses.asdict(overrides or DocumentMetadataOverrides())
        source = input_doc.original_file
        stat = source.stat()
        root = (
            Document.objects.filter(pk=input_doc.root_document_id).first()
            if input_doc.root_document_id
            else None
        )
        if input_doc.root_document_id and root is None:
            raise RejectedResult("Version root no longer exists")
        job = DocumentJob.objects.create(
            id=identifier,
            kind="consume",
            status=DocumentJob.Status.STAGING,
            timeout=settings.CELERY_TASK_TIME_LIMIT,
            dispatch_options=json.loads(transport_dumps(dispatch_options or {})),
            input={
                "source": int(input_doc.source),
                "source_path": str(source),
                "source_identity": [
                    stat.st_dev,
                    stat.st_ino,
                    stat.st_size,
                    stat.st_mtime_ns,
                ],
                "filename": source.name,
                "original_path": str(input_doc.original_path)
                if input_doc.original_path
                else None,
                "mailrule_id": input_doc.mailrule_id,
                "root_document_id": input_doc.root_document_id,
                "revision": _revision(root) if root else None,
                "latest_id": latest_version(root).pk if root else None,
                "overrides": _json(metadata),
                "parent_job_id": active_ingestion_job.get(),
            },
        )
        try:
            validate_application(job.pk)
            shadow = source.with_name(f"._{source.name}")
            if shadow.is_file():
                shadow_stat = shadow.stat()
                job.input["shadow"] = {
                    "path": str(shadow),
                    "identity": [
                        shadow_stat.st_dev,
                        shadow_stat.st_ino,
                        shadow_stat.st_size,
                        shadow_stat.st_mtime_ns,
                    ],
                    "checksum": compute_checksum(shadow),
                }
            directory.mkdir(mode=0o700, exist_ok=True)
            _copy(source, directory / "input")
            copy_basic_file_stats(source, directory / "input")
            job.input["checksum"] = compute_checksum(directory / "input")
            current = source.stat()
            if [
                current.st_dev,
                current.st_ino,
                current.st_size,
                current.st_mtime_ns,
            ] != job.input["source_identity"]:
                raise RejectedResult("Input changed during admission")
            job.status = DocumentJob.Status.QUEUED
            job.save(update_fields=["input", "status"])
        except Exception as error:
            job.status = DocumentJob.Status.REJECTED
            job.error = str(error)
            job.save(update_fields=["status", "error"])
            raise
        return job


def validate_application(job_id: str | UUID, *, registry: object = None) -> None:
    """Recheck the original actor, version revision, and deadline before storing."""
    job = DocumentJob.objects.get(pk=job_id)
    from documents.parser_operations import validate_parser_job

    validate_parser_job(job, registry=registry)
    if (
        job.status == DocumentJob.Status.RUNNING
        and job.timeout
        and (timezone.now() - job.updated).total_seconds() > job.timeout
    ):
        raise RejectedResult("Ingestion exceeded its recorded timeout")
    actor_id = job.input["overrides"].get("actor_id")
    root_id = job.input["root_document_id"]
    if root_id:
        qs = (
            Document.objects.select_for_update()
            if transaction.get_connection().in_atomic_block
            else Document.objects
        )
        root = qs.filter(pk=root_id).first()
        if (
            root is None
            or _revision(root) != job.input["revision"]
            or latest_version(root).pk != job.input["latest_id"]
        ):
            raise RejectedResult("Version root changed after admission")
        _check_permission(root, actor_id)
    elif actor_id is not None:
        actor = User.objects.filter(pk=actor_id, is_active=True).first()
        if actor is None or not actor.has_perm("documents.add_document"):
            raise RejectedResult("The requesting user can no longer add documents")


def record_progress(job_id: str, phase: str) -> None:
    """Keep phase progress available after worker or websocket loss."""
    job = DocumentJob.objects.get(pk=job_id)
    job.result["progress"] = phase
    job.save(update_fields=["result"])


def stage_outputs(
    job_id: str, *, original: Path, thumbnail: Path, archive: Path | None, text: str
) -> tuple[Path, Path, Path | None]:
    """Retain validated outputs before temporary parser cleanup or publication."""
    job = DocumentJob.objects.get(pk=job_id)
    directory = job_directory(job.pk)
    if not isinstance(text, str):
        raise RejectedResult("Parser text must be a string")
    if job.configuration["produce_archive"] and archive is None:
        raise RejectedResult("Parser omitted the required archive")
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


def publish_file(job_id: str, source: Path, target: Path) -> None:
    """Journal every permanent write outside database rollback before copying."""
    directory = job_directory(job_id)
    validate_path_in_root(source, directory)
    if not any(
        target.resolve().is_relative_to(root.resolve())
        for root in (
            settings.ORIGINALS_DIR,
            settings.ARCHIVE_DIR,
            settings.THUMBNAIL_DIR,
        )
    ):
        raise RejectedResult("Publication target is outside document storage")
    checksum = compute_checksum(source)
    job = DocumentJob.objects.get(pk=job_id)
    reference = next(
        (
            value
            for value in job.result.values()
            if isinstance(value, dict) and value.get("name") == source.name
        ),
        None,
    )
    if reference is None or reference["checksum"] != checksum:
        raise RejectedResult("Staged publication output changed after validation")
    if target.exists():
        raise RejectedResult("Publication would overwrite an existing file")
    record_publication(
        job_id, {"source": source.name, "target": str(target), "checksum": checksum}
    )
    _copy(source, target)


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


def commit_consumption(job_id: str, document: Document) -> None:
    """Record document ownership and required completion in its database commit."""
    job = DocumentJob.objects.get(pk=job_id)
    document = Document.global_objects.get(pk=document.pk)
    job.result["published"] = {
        "original": {"path": str(document.source_path), "checksum": document.checksum},
        "archive": {
            "path": str(document.archive_path),
            "checksum": document.archive_checksum,
        }
        if document.archive_path
        else None,
        "thumbnail": {
            "path": str(document.thumbnail_path),
            "checksum": compute_checksum(document.thumbnail_path),
        },
    }
    job.result.update(
        {
            "payload": {"document_id": document.pk},
            "remove_source": True,
            "progress": "completion",
            "root_id": document.root_document_id or document.pk,
            "followup": ["post_consume", "index", "cache", "delivery", "cleanup"],
            "post_consume": "pending"
            if job.configuration.get("post_consume_script")
            else "done",
        }
    )
    job.status = DocumentJob.Status.COMMITTED
    job.save(update_fields=["status", "result"])


def run_post_consume(job_id: str, callback: Callable[[], None]) -> None:
    """Never rerun a script whose external effects have an ambiguous outcome."""
    job = DocumentJob.objects.get(pk=job_id)
    state = job.result.get("post_consume", "done")
    if state in {"done", "cancelled"}:
        return
    if state == "started":
        raise NeedsAttention("Post-consume script may already have run")
    if job.configuration.get("post_consume_script") != settings.POST_CONSUME_SCRIPT:
        raise NeedsAttention("Post-consume script configuration changed")
    job.result["post_consume"] = "started"
    job.save(update_fields=["result"])
    callback()
    job.result["post_consume"] = "done"
    job.save(update_fields=["result"])


def _input(job: DocumentJob) -> tuple[ConsumableDocument, DocumentMetadataOverrides]:
    """Reconstitute legacy parser input only inside the execution adapter."""
    directory = job_directory(job.pk)
    working = directory / "work" / job.input["filename"]
    if not working.exists() and (directory / "input").exists():
        _copy(directory / "input", working)
        copy_basic_file_stats(directory / "input", working)
    metadata = dict(job.input["overrides"])
    if metadata.get("created"):
        metadata["created"] = datetime.date.fromisoformat(metadata["created"])
    input_doc = ConsumableDocument(
        source=DocumentSource(job.input["source"]),
        original_file=working,
        original_path=Path(job.input["original_path"])
        if job.input["original_path"]
        else None,
        source_path=Path(job.input["source_path"]),
        root_document_id=job.input["root_document_id"],
        mailrule_id=job.input["mailrule_id"],
        job_id=str(job.pk),
        defer_mime_type=True,
    )
    return input_doc, DocumentMetadataOverrides(**metadata)


def finish_consumption(job: DocumentJob) -> None:
    """Resume required safe completion without replaying consumption workflows."""
    from documents.caching import clear_document_caches
    from documents.consumer import ConsumerPlugin
    from documents.job_callbacks import finish_ingestion_followups
    from documents.plugins.helpers import ProgressManager
    from documents.search import get_backend
    from paperless.config import AIConfig

    payload = job.result["payload"]
    if isinstance(payload, dict) and "document_id" in payload:
        document = Document.objects.filter(pk=payload["document_id"]).first()
        if document and job.result.get("post_consume") != "done":
            input_doc, overrides = _input(job)
            with ProgressManager(
                overrides.filename or job.input["filename"], str(job.pk)
            ) as progress:
                plugin = ConsumerPlugin(
                    input_doc, overrides, progress, job_directory(job.pk), str(job.pk)
                )
                run_post_consume(
                    str(job.pk), lambda: plugin.run_post_consume_script(document)
                )
        elif document is None and job.result.get("post_consume") == "pending":
            job.result["post_consume"] = "cancelled"
            job.result["post_consume_reason"] = "Document deleted before execution"
            job.save(update_fields=["result"])
        elif document is None and job.result.get("post_consume") == "started":
            raise NeedsAttention("Post-consume script may already have run")
    finish_ingestion_followups(job)
    job.refresh_from_db()
    if isinstance(payload, dict) and "document_id" in payload:
        with get_backend().batch_update() as batch:
            root = Document.objects.filter(pk=job.result["root_id"]).first()
            if root:
                batch.add_or_update(root)
            else:
                batch.remove(job.result["root_id"])
        if root and AIConfig().llm_index_enabled:
            from paperless_ai.indexing import llm_index_add_or_update_document

            if llm_index_add_or_update_document(root) is False:
                raise RuntimeError("AI indexing is waiting for migration")
        clear_document_caches(payload["document_id"])
        clear_document_caches(job.result["root_id"])
    job.refresh_from_db()
    job.status = DocumentJob.Status.SUCCEEDED
    job.result["followup"] = ["delivery", "cleanup"]
    job.result["progress"] = "delivery"
    job.error = ""
    job.save(update_fields=["status", "result", "error"])


def cleanup_consumption(job: DocumentJob) -> None:
    """Delete only the admitted source identity, after durable success."""
    payload = job.result.get("payload")
    if isinstance(payload, dict) and payload.get("document_id"):
        document = Document.global_objects.filter(pk=payload["document_id"]).first()
        if document is not None:
            if (
                not document.source_path.is_file()
                or compute_checksum(document.source_path) != document.checksum
            ):
                raise OSError(
                    "Published original is missing or changed; recovery input retained"
                )
            if document.archive_path and (
                not document.archive_path.is_file()
                or compute_checksum(document.archive_path) != document.archive_checksum
            ):
                raise OSError(
                    "Published archive is missing or changed; recovery output retained"
                )
    source = Path(job.input["source_path"])
    if not job.result.get("remove_source", True) and not source.exists():
        raise OSError("Stopped ingestion has no source file; staged input retained")
    if job.result.get("remove_source", True) and source.is_file():
        stat = source.stat()
        if [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns] == job.input[
            "source_identity"
        ] and compute_checksum(source) == job.input["checksum"]:
            source.unlink()
            _sync_directory(source.parent)
    if (
        isinstance(payload, dict)
        and payload.get("document_id")
        and job.input.get("shadow")
    ):
        reference = job.input["shadow"]
        shadow = Path(reference["path"])
        if shadow.is_file():
            stat = shadow.stat()
            if [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns] == reference[
                "identity"
            ] and compute_checksum(shadow) == reference["checksum"]:
                shadow.unlink()
                _sync_directory(shadow.parent)
    directory = job_directory(job.pk)
    if directory.exists():
        shutil.rmtree(directory)
        _sync_directory(directory.parent)
    job.cleaned = True
    job.result["followup"] = []
    job.result["progress"] = "done"
    job.save(update_fields=["cleaned", "result"])


def run_consumption(job_id: str, task_id: str | None = None) -> dict | None:
    """Execute at most one ingestion attempt and recover committed completion."""
    directory = job_directory(job_id)
    lock = FileLock(directory.with_suffix(".lock"), timeout=0)
    try:
        lock.acquire()
    except Timeout:
        raise Ignore from None
    token = active_ingestion_job.set(str(job_id))
    try:
        job = DocumentJob.objects.get(pk=job_id)
        if job.status in {
            DocumentJob.Status.RUNNING,
            DocumentJob.Status.STAGING,
            DocumentJob.Status.ATTENTION,
        }:
            raise NeedsAttention(
                "Interrupted ingestion may have external effects; inspect its journal before recovery"
            )
        if job.status in {DocumentJob.Status.FAILED, DocumentJob.Status.REJECTED}:
            raise RejectedResult(job.error)
        if job.result.get("delivery") in {"started", "failure-started", "failure-done"}:
            raise NeedsAttention("Completion callbacks may already have been delivered")
        if job.status == DocumentJob.Status.QUEUED:
            validate_application(job.pk)
            if compute_checksum(directory / "input") != job.input["checksum"]:
                raise RejectedResult("Staged input has changed")
            job.status = DocumentJob.Status.RUNNING
            job.updated = timezone.now()
            job.attempts += 1
            job.configuration = {"post_consume_script": settings.POST_CONSUME_SCRIPT}
            job.save()
            from documents.tasks import _consume_file_pipeline

            input_doc, overrides = _input(job)
            payload = _consume_file_pipeline(
                input_doc, overrides, task_id=task_id or str(job.pk)
            )
            job.refresh_from_db()
            if job.status == DocumentJob.Status.RUNNING:
                job.result["payload"] = payload
                job.result["remove_source"] = not input_doc.original_file.exists()
                job.result["followup"] = ["delivery", "cleanup"]
                job.status = DocumentJob.Status.COMMITTED
                job.save(update_fields=["result", "status"])
        if job.status == DocumentJob.Status.COMMITTED:
            # Completion indexing is deliberately outside the deferral scope.
            active_ingestion_job.reset(token)
            token = active_ingestion_job.set(None)
            finish_consumption(job)
        if job.status == DocumentJob.Status.SUCCEEDED:
            if has_callbacks(job):
                if job.result.get("delivery") == "done":
                    if not callbacks_complete(job):
                        raise Ignore
                    if not job.cleaned:
                        cleanup_consumption(job)
                    raise Ignore
                job.result["delivery"] = "started"
                job.save(update_fields=["result"])
            elif not job.cleaned:
                cleanup_consumption(job)
            return job.result.get("payload")
    except Ignore:
        raise
    except Exception as error:
        job = DocumentJob.objects.get(pk=job_id)
        job.error = str(error)
        if isinstance(error, NeedsAttention) or job.status in {
            DocumentJob.Status.RUNNING,
            DocumentJob.Status.STAGING,
        }:
            job.status = DocumentJob.Status.ATTENTION
        elif (
            isinstance(error, RejectedResult)
            and job.status == DocumentJob.Status.QUEUED
        ):
            job.status = DocumentJob.Status.REJECTED
        job.updated = timezone.now()
        job.save(update_fields=["status", "error", "updated"])
        raise
    finally:
        active_ingestion_job.reset(token)
        lock.release()


def has_callbacks(job: DocumentJob) -> bool:
    """Tell whether Celery has externally observable completion work to deliver."""
    return any(
        job.dispatch_options.get(name)
        for name in ("link", "link_error", "chord", "chain")
    )


def callbacks_complete(job: DocumentJob) -> bool:
    """Keep input until concrete callback operations report durable completion."""
    if job.result.get("callbacks_acknowledged"):
        return True
    identifiers = []
    for name in ("link", "chord", "chain"):
        value = job.dispatch_options.get(name)
        for callback in (
            (value if isinstance(value, list) else [value]) if value else []
        ):
            identifier = callback.get("options", {}).get("task_id")
            if identifier:
                identifiers.append(identifier)
    completed = DocumentJob.objects.filter(
        pk__in=identifiers, kind="callback", status=DocumentJob.Status.SUCCEEDED
    ).count()
    if completed == len(set(identifiers)):
        return True
    if job.updated + datetime.timedelta(seconds=job.timeout or 1800) < timezone.now():
        job.status = DocumentJob.Status.ATTENTION
        job.error = "Completion callback is missing, failed, or interrupted; inspect its durable operation"
        job.save(update_fields=["status", "error"])
    return False


class DurableConsumeTask(DjangoTask):
    """Stage all existing call/signature forms at the Celery admission boundary."""

    def apply_async(self, args=None, kwargs=None, **options):
        """Replace legacy dataclass messages with a durable UUID before publication."""
        args = tuple(args or ())
        kwargs = dict(kwargs or {})
        if "job_id" not in kwargs:
            input_doc = kwargs.get("input_doc", args[0] if args else None)
            overrides = kwargs.get("overrides", args[1] if len(args) > 1 else None)
            for name in ("link", "link_error", "chord", "chain"):
                if options.get(name):
                    items = (
                        options[name]
                        if isinstance(options[name], (tuple, list))
                        else [options[name]]
                    )
                    frozen = []
                    for item in items:
                        callback = signature(item, app=self.app).clone()
                        if callback.task == "documents.bulk_edit.delete":
                            doc_ids = (
                                callback.args[0]
                                if callback.args
                                else callback.kwargs.get("doc_ids", [])
                            )
                            from documents.job_callbacks import deletion_revision

                            callback.kwargs.setdefault(
                                "expected_revisions",
                                {
                                    str(document.pk): deletion_revision(document)
                                    for document in Document.objects.filter(
                                        pk__in=doc_ids
                                    )
                                },
                            )
                            callback.kwargs.setdefault(
                                "actor_id", overrides.actor_id if overrides else None
                            )
                        callback.freeze()
                        frozen.append(callback)
                    options[name] = frozen[0] if name == "chord" else frozen
            retained = {
                key: value
                for key, value in options.items()
                if key
                in {
                    "link",
                    "link_error",
                    "chord",
                    "chain",
                    "group_id",
                    "group_index",
                    "root_id",
                    "parent_id",
                    "headers",
                    "queue",
                    "routing_key",
                    "priority",
                }
            }
            job = stage_consumption(
                input_doc,
                overrides,
                job_id=options.get("task_id"),
                dispatch_options=retained,
            )
            kwargs = {"job_id": str(job.pk)}
            options["task_id"] = str(job.pk)
            args = ()
        options.setdefault("task_id", kwargs["job_id"])
        if transaction.get_connection().in_atomic_block:
            transaction.on_commit(
                lambda: super(DurableConsumeTask, self).apply_async(
                    args=args, kwargs=kwargs, **options
                )
            )
            return self.AsyncResult(options["task_id"])
        return super().apply_async(args=args, kwargs=kwargs, **options)

    def after_return(self, status, retval, task_id, args, kwargs, einfo):
        """Acknowledge native callback dispatch; ambiguous interruption stays visible."""
        identifier = kwargs.get("job_id", task_id)
        job = DocumentJob.objects.filter(pk=identifier, kind="consume").first()
        if job and has_callbacks(job):
            with FileLock(job_directory(job.pk).with_suffix(".lock")):
                job.refresh_from_db()
                if status == "SUCCESS" and job.result.get("delivery") == "started":
                    job.result["delivery"] = "done"
                    job.save(update_fields=["result"])
                    if callbacks_complete(job):
                        cleanup_consumption(job)
                elif (
                    status == "FAILURE"
                    and job.result.get("delivery") == "failure-started"
                ):
                    job.result["delivery"] = "failure-done"
                    job.save(update_fields=["result"])
        return super().after_return(status, retval, task_id, args, kwargs, einfo)


def dispatch_consumption(job: DocumentJob) -> None:
    """Replay durable admission with the original Celery completion metadata."""
    from documents.tasks import consume_file

    options = transport_loads(json.dumps(job.dispatch_options))
    consume_file.apply_async(
        kwargs={"job_id": str(job.pk)},
        task_id=str(job.pk),
        time_limit=job.timeout,
        **options,
    )
