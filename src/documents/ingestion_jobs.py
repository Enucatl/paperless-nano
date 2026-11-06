"""Durable ingestion admission and conservative recovery of external effects."""

import dataclasses
import datetime
import json
import logging
import shutil
from pathlib import Path
from uuid import UUID
from uuid import uuid4

from celery import signature
from celery.contrib.django.task import DjangoTask
from celery.exceptions import Ignore
from django.conf import settings
from django.db import transaction
from django.utils import timezone
from filelock import FileLock
from filelock import Timeout
from kombu.utils.json import dumps as transport_dumps
from kombu.utils.json import loads as transport_loads

from documents.data_models import ConsumableDocument
from documents.data_models import DocumentMetadataOverrides
from documents.ingestion_state import restore_input
from documents.ingestion_state import run_post_consume
from documents.ingestion_state import validate_application
from documents.job_context import active_ingestion_job
from documents.job_state import PendingOperation
from documents.job_state import RejectedResult
from documents.job_state import document_revision as _revision
from documents.job_state import track_terminal_task
from documents.job_storage import atomic_copy as _copy
from documents.job_storage import job_directory
from documents.job_storage import sync_directory as _sync_directory
from documents.models import Document
from documents.models import DocumentJob
from documents.operation_journal import NeedsAttention
from documents.operation_journal import json_value
from documents.operation_journal import operation_failure_status
from documents.processor_notifications import defer_notifications
from documents.utils import compute_checksum
from documents.utils import copy_basic_file_stats
from documents.versioning import latest_version

logger = logging.getLogger("paperless.jobs")


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
    with FileLock(directory.with_suffix(".lock"), timeout=0, is_singleton=True):
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
            result={
                "lifecycle": 1,
                "scripts": {
                    "pre": settings.PRE_CONSUME_SCRIPT,
                    "post": settings.POST_CONSUME_SCRIPT,
                },
            },
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
                "overrides": json_value(metadata),
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
            job.result["phase"] = "accepted"
            job.save(update_fields=["input", "status", "result"])
        except Exception as error:
            job.status = DocumentJob.Status.REJECTED
            job.error = str(error)
            job.save(update_fields=["status", "error"])
            raise
        return job


def finish_consumption(job: DocumentJob) -> None:
    """Resume required safe completion without replaying consumption workflows."""
    from documents.caching import clear_document_caches
    from documents.consumer import ConsumerPlugin
    from documents.job_callbacks import finish_ingestion_followups
    from documents.plugins.helpers import ProgressManager
    from documents.search import get_backend
    from paperless.config import AIConfig

    modern = bool(job.result.get("lifecycle"))
    if modern:
        from documents.ingestion import complete_document
        from documents.ingestion_state import transition

        transition(job, "completing")
        token = active_ingestion_job.set(str(job.pk))
        try:
            complete_document(job)
        finally:
            active_ingestion_job.reset(token)
        job.refresh_from_db()
    payload = job.result["payload"]
    if isinstance(payload, dict) and "document_id" in payload:
        document = Document.objects.filter(pk=payload["document_id"]).first()
        if document:
            input_doc, overrides = restore_input(job)
            with ProgressManager(
                overrides.filename or job.input["filename"], str(job.pk)
            ) as progress:
                plugin = ConsumerPlugin(
                    input_doc, overrides, progress, job_directory(job.pk), str(job.pk)
                )
                from uuid import uuid5

                plugin.invocation_id = str(uuid5(job.pk, "complete:post_consume"))
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

    def index_and_cache(_identifier: str) -> None:
        """Repeat only derived-state writes from the current committed document."""
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

    if isinstance(payload, dict) and "document_id" in payload:
        if modern:
            from documents.operation_journal import invoke

            invoke(
                job,
                "complete:index_cache",
                "paperless.index_cache",
                {"root_id": job.result["root_id"]},
                index_and_cache,
                replay_safe=True,
            )
        else:
            index_and_cache(str(job.pk))
    if modern and isinstance(payload, dict) and "document_id" in payload:
        from documents.consumer import ConsumerStatusShortMessage
        from documents.ingestion import metadata_from_json
        from documents.plugins.helpers import ProgressStatusOptions

        def notify(_identifier: str) -> None:
            """Report completion after required effects and derived-state updates."""
            input_doc, _ = restore_input(job)
            overrides = metadata_from_json(job.result["metadata"])
            with ProgressManager(
                overrides.filename or job.input["filename"], str(job.pk)
            ) as progress:
                consumer = ConsumerPlugin(
                    input_doc, overrides, progress, job_directory(job.pk), str(job.pk)
                )
                consumer._send_progress(
                    100,
                    100,
                    ProgressStatusOptions.SUCCESS,
                    ConsumerStatusShortMessage.FINISHED,
                    payload["document_id"],
                )

        invoke(
            job,
            "complete:progress",
            "paperless.progress",
            {"document_id": payload["document_id"]},
            notify,
            replay_safe=True,
        )
    job.refresh_from_db()
    job.status = DocumentJob.Status.SUCCEEDED
    job.result["followup"] = ["delivery", "cleanup"]
    job.result["progress"] = "delivery"
    job.error = ""
    job.save(update_fields=["status", "result", "error"])
    if modern and not has_callbacks(job):
        transition(job, "complete")


def cleanup_consumption(job: DocumentJob) -> None:
    """Delete only the admitted source identity, after durable success."""
    job.result["cleanup"] = "running"
    job.save(update_fields=["result"])
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
    from documents.ingestion_storage import cleanup_publication

    cleanup_publication(job)
    directory = job_directory(job.pk)
    if directory.exists():
        shutil.rmtree(directory)
        _sync_directory(directory.parent)
    if job.result.get("lifecycle"):
        from documents.ingestion_state import transition

        transition(job, "complete")
    job.cleaned = True
    job.result["cleanup"] = "succeeded"
    job.result["followup"] = []
    job.result["progress"] = "done"
    job.save(update_fields=["cleaned", "result"])


@defer_notifications()
@track_terminal_task
def run_consumption(job_id: str, task_id: str | None = None) -> dict | None:
    """Execute at most one ingestion attempt and recover committed completion."""
    directory = job_directory(job_id)
    lock = FileLock(directory.with_suffix(".lock"), timeout=0, is_singleton=True)
    try:
        lock.acquire()
    except Timeout:
        raise Ignore from None
    token = active_ingestion_job.set(str(job_id))
    try:
        job = DocumentJob.objects.get(pk=job_id)
        if job.status in {DocumentJob.Status.WAITING, DocumentJob.Status.CANCELLED}:
            raise Ignore
        modern = job.result.get("lifecycle") == 1
        if job.result.get("lifecycle") not in {None, 1} or (
            job.status == DocumentJob.Status.PREPARED and not modern
        ):
            raise NeedsAttention(
                "Unknown or legacy preparation contract; inspect retained artifacts"
            )
        if (
            modern
            and job.status == DocumentJob.Status.ATTENTION
            and job.result.get("phase") == "preparing"
        ):
            job.status = DocumentJob.Status.RUNNING
            job.save(update_fields=["status"])
        if job.status in {DocumentJob.Status.STAGING, DocumentJob.Status.ATTENTION} or (
            job.status == DocumentJob.Status.RUNNING and not modern
        ):
            raise NeedsAttention(
                "Interrupted legacy ingestion or unresolved effects require inspection"
            )
        if job.status in {DocumentJob.Status.FAILED, DocumentJob.Status.REJECTED}:
            raise RejectedResult(job.error)
        if job.result.get("delivery") in {"started", "failure-started", "failure-done"}:
            raise NeedsAttention("Completion callbacks may already have been delivered")
        if job.status == DocumentJob.Status.QUEUED:
            validate_application(job.pk)
            if compute_checksum(directory / "input") != job.input["checksum"]:
                raise RejectedResult("Staged input has changed")
            # Queued legacy jobs have never invoked plugins and can adopt the contract.
            job.result.setdefault("lifecycle", 1)
            job.result.setdefault("phase", "accepted")
            job.result.setdefault(
                "scripts",
                {
                    "pre": settings.PRE_CONSUME_SCRIPT,
                    "post": settings.POST_CONSUME_SCRIPT,
                },
            )
            job.attempts += 1
            job.save(update_fields=["result", "attempts"])
            from documents.ingestion_state import transition

            transition(job, "preparing")
        if job.status == DocumentJob.Status.RUNNING:
            from documents.ingestion import prepare

            prepare(job, task_id or str(job.pk))
        if job.status == DocumentJob.Status.PREPARED:
            from documents.ingestion import publish

            publish(job, task_id or str(job.pk))
            job.refresh_from_db()
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
    except PendingOperation:
        raise Ignore from None
    except Ignore:
        raise
    except Exception as error:
        job = DocumentJob.objects.get(pk=job_id)
        job.error = str(error)
        failure_status = operation_failure_status(job)
        if failure_status in {DocumentJob.Status.REJECTED, DocumentJob.Status.FAILED}:
            job.status = failure_status
        elif (
            isinstance(error, NeedsAttention)
            or job.status == DocumentJob.Status.STAGING
            or (
                any(
                    entry["status"] == "unknown"
                    for entry in job.result.get("invocations", {}).values()
                )
            )
        ):
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
