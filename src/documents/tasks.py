import datetime
import logging
import shutil
import zipfile
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from tempfile import mkstemp

import magic
from celery import Task
from celery import shared_task
from celery.exceptions import Ignore
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import models
from django.utils import timezone

from documents.caching import clear_document_caches
from documents.data_models import ConsumableDocument
from documents.data_models import ConsumeFileDuplicateResult
from documents.data_models import ConsumeFileStoppedResult
from documents.data_models import ConsumeFileSuccessResult
from documents.data_models import DocumentMetadataOverrides
from documents.data_models import StoredBarcode
from documents.exceptions import SearchIndexLockError
from documents.ingestion_jobs import DurableConsumeTask
from documents.matching import prefilter_documents_by_workflowtrigger
from documents.models import CustomFieldInstance
from documents.models import Document
from documents.models import PaperlessTask
from documents.models import ShareLink
from documents.models import ShareLinkBundle
from documents.models import Tag
from documents.models import WorkflowRun
from documents.models import WorkflowTrigger
from documents.plugins.base import ConsumeTaskPlugin
from documents.plugins.base import StopConsumeTaskError
from documents.plugins.helpers import ProgressManager
from documents.plugins.helpers import ProgressStatusOptions
from documents.signals.handlers import cleanup_document_deletion
from documents.signals.handlers import send_websocket_document_updated
from documents.utils import IterWrapper
from documents.utils import identity
from documents.workflows.runner import run_workflows
from documents.workflows.utils import get_workflows_for_trigger
from paperless.config import AIConfig
from paperless.config import BarcodeConfig
from paperless.logging import consume_task_id
from paperless_ai.exceptions import LLMTimeoutError

if settings.AUDIT_LOG_ENABLED:
    from auditlog.models import LogEntry
logger = logging.getLogger("paperless.tasks")


@shared_task
def index_optimize() -> None:
    logger.info(
        "index_optimize is a no-op — Tantivy manages segment merging automatically.",
    )


@shared_task(
    bind=True,
    ignore_result=True,
    autoretry_for=(SearchIndexLockError,),
    max_retries=5,
    retry_backoff=60,
    retry_jitter=True,
)
def index_document(self, document_id: int) -> None:
    """
    Deferred single-document index write.

    Used as a self-healing fallback when add_or_update() exhausts its lock retry
    budget during high-concurrency consumption. Runs via batch_update() directly
    to avoid re-entering the deferred scheduling path in add_or_update().

    If the document was deleted before this task runs, it exits cleanly.
    """
    from documents.search import get_backend

    try:
        document = Document.objects.get(pk=document_id)
    except Document.DoesNotExist:
        logger.info(
            "index_document: document %d no longer exists; skipping",
            document_id,
        )
        return
    with get_backend().batch_update() as batch:
        batch.add_or_update(document)


@shared_task(
    bind=True,
    ignore_result=True,
    autoretry_for=(SearchIndexLockError,),
    max_retries=5,
    retry_backoff=60,
    retry_jitter=True,
)
def remove_document_from_index(self, doc_id: int) -> None:
    """
    Deferred single-document index removal.

    Used as a self-healing fallback when remove() exhausts its lock retry budget.
    Operates only on the Tantivy index; no database lookup required.
    If the document has already been removed, the term-query delete is a no-op.
    """
    from documents.search import get_backend

    with get_backend().batch_update() as batch:
        batch.remove(doc_id)


@shared_task
def train_classifier(
    *,
    status_callback: Callable[[str], None] | None = None,
) -> str:
    """Run the classifier training operation through the Celery adapter."""
    from documents.classifier_training import train_classifier as train

    return train(status_callback=status_callback)


@shared_task(bind=True, base=DurableConsumeTask)
def consume_file(
    self: Task,
    input_doc: ConsumableDocument | None = None,
    overrides: DocumentMetadataOverrides | None = None,
    *,
    job_id: str | None = None,
):
    """Execute durable ingestion; legacy direct calls enter the same admission."""
    from documents.ingestion_jobs import run_consumption
    from documents.ingestion_jobs import stage_consumption

    if job_id is None:
        job_id = str(stage_consumption(input_doc, overrides, job_id=self.request.id).pk)
    from documents.ingestion_jobs import NeedsAttention
    from documents.ingestion_jobs import has_callbacks
    from documents.models import DocumentJob

    try:
        return run_consumption(job_id, task_id=self.request.id)
    except Ignore:
        raise
    except Exception as error:
        job = DocumentJob.objects.get(pk=job_id)
        # An uncertain replay must not dispatch the native errback a second time.
        if isinstance(error, NeedsAttention) or job.result.get("delivery"):
            PaperlessTask.objects.filter(task_id=self.request.id or job_id).update(
                status=PaperlessTask.Status.FAILURE,
                result_data={
                    "error_type": type(error).__name__,
                    "error_message": str(error),
                },
            )
            logger.error("Ingestion job %s needs attention: %s", job_id, error)
            raise Ignore from error
        if has_callbacks(job):
            job.result["delivery"] = "failure-started"
            job.save(update_fields=["result"])
        raise


def _consume_file_pipeline(
    input_doc: ConsumableDocument,
    overrides: DocumentMetadataOverrides | None = None,
    *,
    task_id: str | None = None,
) -> (
    ConsumeFileSuccessResult
    | ConsumeFileStoppedResult
    | ConsumeFileDuplicateResult
    | None
):
    from documents.barcodes import BarcodePlugin
    from documents.consumer import AsnCheckPlugin
    from documents.consumer import ConsumeFileDuplicateError
    from documents.consumer import ConsumerPlugin
    from documents.consumer import ConsumerPreflightPlugin
    from documents.consumer import WorkflowTriggerPlugin
    from documents.double_sided import CollatePlugin

    token = consume_task_id.set((task_id or "")[:8])
    try:
        if input_doc.mime_type is None:
            input_doc.mime_type = magic.from_file(input_doc.original_file, mime=True)
            receipt = PaperlessTask.objects.filter(task_id=task_id).first()
            if receipt is not None:
                receipt.input_data["mime_type"] = input_doc.mime_type
                receipt.save(update_fields=["input_data"])

        # Default no overrides
        if overrides is None:
            overrides = DocumentMetadataOverrides()

        plugins: list[type[ConsumeTaskPlugin]] = (
            [
                ConsumerPreflightPlugin,
                ConsumerPlugin,
            ]
            if input_doc.root_document_id is not None
            else [
                ConsumerPreflightPlugin,
                AsnCheckPlugin,
                CollatePlugin,
                BarcodePlugin,
                AsnCheckPlugin,  # Re-run ASN check after barcode reading
                WorkflowTriggerPlugin,
                ConsumerPlugin,
            ]
        )

        with (
            ProgressManager(
                overrides.filename or input_doc.original_file.name,
                task_id,
            ) as status_mgr,
            TemporaryDirectory(dir=settings.SCRATCH_DIR) as tmp_dir,
        ):
            tmp_dir = Path(tmp_dir)
            msg = None
            for plugin_class in plugins:
                plugin_name = plugin_class.NAME

                plugin = plugin_class(
                    input_doc,
                    overrides,
                    status_mgr,
                    tmp_dir,
                    task_id,
                )

                if not plugin.able_to_run:
                    logger.debug(f"Skipping plugin {plugin_name}")
                    continue

                if input_doc.job_id:
                    from documents.ingestion_jobs import record_progress

                    record_progress(input_doc.job_id, plugin_class.__name__)
                try:
                    logger.debug(f"Executing plugin {plugin_name}")
                    plugin.setup()

                    msg = plugin.run()

                    if msg is not None:
                        logger.info(f"{plugin_name} completed with: {msg}")
                    else:
                        logger.info(f"{plugin_name} completed with no message")

                    overrides = plugin.metadata

                except StopConsumeTaskError as e:
                    logger.info(f"{plugin_name} requested task exit: {e.message}")
                    return ConsumeFileStoppedResult(reason=e.message)

                except ConsumeFileDuplicateError as e:
                    logger.info(f"{plugin_name} rejected duplicate: {e}")
                    return ConsumeFileDuplicateResult(
                        duplicate_of=e.duplicate_id,
                        duplicate_in_trash=e.in_trash,
                    )

                except Exception as e:
                    logger.exception(f"{plugin_name} failed: {e}")
                    status_mgr.send_progress(
                        ProgressStatusOptions.FAILED,
                        f"{e}",
                        100,
                        100,
                    )
                    raise

                finally:
                    plugin.cleanup()

        return msg
    finally:
        consume_task_id.reset(token)


@shared_task
def sanity_check(*, raise_on_error: bool = True) -> str:
    from documents import sanity_checker
    from documents.sanity_checker import SanityCheckFailedException

    messages = sanity_checker.check_sanity()
    messages.log_messages()

    if not messages.has_error and not messages.has_warning and not messages.has_info:
        return "No issues detected."

    parts: list[str] = []
    if messages.document_error_count:
        parts.append(f"{messages.document_error_count} document(s) with errors")
    if messages.document_warning_count:
        parts.append(f"{messages.document_warning_count} document(s) with warnings")
    if messages.document_info_count:
        parts.append(f"{messages.document_info_count} document(s) with infos")
    if messages.global_warning_count:
        parts.append(f"{messages.global_warning_count} global warning(s)")

    summary = ", ".join(parts) + " found."

    if messages.has_error:
        message = summary + " Check logs for details."
        if raise_on_error:
            raise SanityCheckFailedException(message)
        return message

    return summary


@shared_task
def bulk_update_documents(document_ids) -> None:
    from documents.updates import complete_document_updates

    complete_document_updates(document_ids)


def _read_barcodes_for_reprocess(document: Document) -> list[StoredBarcode] | None:
    """
    Reads the barcodes of the original again, e.g. for documents consumed
    before storing them was enabled. Returns None if they should be left as
    they are: storing is off, the file can't be scanned with the current
    settings, or the scan failed.
    """
    barcode_settings = BarcodeConfig()
    if not barcode_settings.barcode_store_values:
        return None
    try:
        from documents.barcodes import read_barcode_values

        with TemporaryDirectory(dir=settings.SCRATCH_DIR) as tmpdir:
            return read_barcode_values(
                document.source_path,
                document.mime_type,
                barcode_settings,
                Path(tmpdir),
            )
    except Exception as e:
        logger.warning(f"Could not read barcodes of document {document}: {e}")
        return None


@shared_task(bind=True)
def update_document_content_maybe_archive_file(
    self: Task,
    document_id: int,
    *,
    remote_ocr: bool = False,
    job_id: str | None = None,
) -> None:
    """Execute a durable job, admitting legacy CLI/task calls at this boundary."""
    from documents.jobs import run_reprocess
    from documents.jobs import stage_reprocess

    if job_id is None:
        job_id = str(
            stage_reprocess(
                document_id, remote_ocr=remote_ocr, job_id=self.request.id
            ).pk
        )
    if run_reprocess(job_id) is False:
        raise Ignore


@shared_task
def recover_document_jobs() -> None:
    """Redispatch durable jobs whose execution or completion was interrupted."""
    from documents.jobs import recover_document_jobs as recover

    recover()


@shared_task
def empty_trash(doc_ids=None) -> None:
    if doc_ids is None:
        logger.info("Emptying trash of all expired documents")
    documents = (
        Document.deleted_objects.filter(id__in=doc_ids)
        if doc_ids is not None
        else Document.deleted_objects.filter(
            deleted_at__lt=timezone.localtime(timezone.now())
            - datetime.timedelta(
                days=settings.EMPTY_TRASH_DELAY,
            ),
        )
    )

    try:
        deleted_document_ids = list(documents.values_list("id", flat=True))
        # Temporarily connect the cleanup handler
        models.signals.post_delete.connect(cleanup_document_deletion, sender=Document)
        documents.delete()  # this is effectively a hard delete
        logger.info(f"Deleted {len(deleted_document_ids)} documents from trash")

        if settings.AUDIT_LOG_ENABLED:
            # Delete the audit log entries for documents that dont exist anymore
            LogEntry.objects.filter(
                content_type=ContentType.objects.get_for_model(Document),
                object_id__in=deleted_document_ids,
            ).delete()
    except Exception as e:  # pragma: no cover
        logger.exception(f"Error while emptying trash: {e}")
    finally:
        models.signals.post_delete.disconnect(
            cleanup_document_deletion,
            sender=Document,
        )


@shared_task
def check_scheduled_workflows() -> None:
    """
    Check and run all enabled scheduled workflows.

    Scheduled triggers are evaluated based on a target date field (e.g. added, created, modified, or a custom date field),
    combined with a day offset:
        - Positive offsets mean the workflow should trigger AFTER the specified date (e.g., offset = +7 → trigger 7 days after)
        - Negative offsets mean the workflow should trigger BEFORE the specified date (e.g., offset = -7 → trigger 7 days before)

    Once a document satisfies this condition, and recurring/non-recurring constraints are met, the workflow is run.
    """
    scheduled_workflows = get_workflows_for_trigger(
        WorkflowTrigger.WorkflowTriggerType.SCHEDULED,
    )
    if scheduled_workflows.count() > 0:
        logger.debug(f"Checking {len(scheduled_workflows)} scheduled workflows")
        now = timezone.now()
        for workflow in scheduled_workflows:
            schedule_triggers = workflow.triggers.filter(
                type=WorkflowTrigger.WorkflowTriggerType.SCHEDULED,
            )
            trigger: WorkflowTrigger
            for trigger in schedule_triggers:
                documents = Document.objects.none()
                offset_td = datetime.timedelta(days=trigger.schedule_offset_days)
                threshold = now - offset_td
                logger.debug(
                    f"Trigger {trigger.id}: checking if (date + {offset_td}) <= now ({now})",
                )

                match trigger.schedule_date_field:
                    case WorkflowTrigger.ScheduleDateField.ADDED:
                        documents = Document.objects.filter(
                            root_document__isnull=True,
                            added__lte=threshold,
                        )

                    case WorkflowTrigger.ScheduleDateField.CREATED:
                        documents = Document.objects.filter(
                            root_document__isnull=True,
                            created__lte=threshold,
                        )

                    case WorkflowTrigger.ScheduleDateField.MODIFIED:
                        documents = Document.objects.filter(
                            root_document__isnull=True,
                            modified__lte=threshold,
                        )

                    case WorkflowTrigger.ScheduleDateField.CUSTOM_FIELD:
                        # cap earliest date to avoid massive scans
                        earliest_date = now - datetime.timedelta(days=365)
                        if offset_td.days < -365:
                            logger.warning(
                                f"Trigger {trigger.id} has large negative offset ({offset_td.days}), "
                                f"limiting earliest scan date to {earliest_date}",
                            )

                        cf_filter_kwargs = {
                            "field": trigger.schedule_date_custom_field,
                            "value_date__isnull": False,
                            "value_date__lte": threshold,
                            "value_date__gte": earliest_date,
                        }

                        recent_cf_instances = CustomFieldInstance.objects.filter(
                            **cf_filter_kwargs,
                        )

                        matched_ids = [
                            cfi.document_id
                            for cfi in recent_cf_instances
                            if cfi.value_date
                            and (
                                timezone.make_aware(
                                    datetime.datetime.combine(
                                        cfi.value_date,
                                        datetime.time.min,
                                    ),
                                )
                                + offset_td
                                <= now
                            )
                        ]

                        documents = Document.objects.filter(
                            root_document__isnull=True,
                            id__in=matched_ids,
                        )

                if documents.exists():
                    documents = prefilter_documents_by_workflowtrigger(
                        documents,
                        trigger,
                    )

                if documents.exists():
                    logger.debug(
                        f"Found {documents.count()} documents for trigger {trigger}",
                    )
                    for document in documents:
                        workflow_runs = WorkflowRun.objects.filter(
                            document=document,
                            type=WorkflowTrigger.WorkflowTriggerType.SCHEDULED,
                            workflow=workflow,
                        ).order_by("-run_at")
                        if not trigger.schedule_is_recurring and workflow_runs.exists():
                            logger.debug(
                                f"Skipping document {document} for non-recurring workflow {workflow} as it has already been run",
                            )
                            continue

                        if (
                            trigger.schedule_is_recurring
                            and workflow_runs.exists()
                            and (
                                workflow_runs.first().run_at
                                > now
                                - datetime.timedelta(
                                    days=trigger.schedule_recurring_interval_days,
                                )
                            )
                        ):
                            # schedule is recurring but the last run was within the number of recurring interval days
                            logger.debug(
                                f"Skipping document {document} for recurring workflow {workflow} as the last run was within the recurring interval",
                            )
                            continue
                        run_workflows(
                            trigger_type=WorkflowTrigger.WorkflowTriggerType.SCHEDULED,
                            workflow_to_run=workflow,
                            document=document,
                        )
                        # Scheduled workflows dont send document_updated signal, so send a websocket update here to ensure clients are updated
                        send_websocket_document_updated(
                            sender=None,
                            document=document,
                        )


def update_document_parent_tags(tag: Tag, new_parent: Tag) -> None:
    """
    When a tag's parent changes, ensure all documents containing the tag also have
    the parent tag (and its ancestors) applied.
    """
    doc_tag_relationship = Document.tags.through

    doc_ids: list[int] = list(
        Document.objects.filter(tags=tag).values_list("pk", flat=True),
    )

    if not doc_ids:
        return

    parent_ids = [new_parent.id, *new_parent.get_ancestors_pks()]

    parent_ids = list(dict.fromkeys(parent_ids))

    existing_pairs = set(
        doc_tag_relationship.objects.filter(
            document_id__in=doc_ids,
            tag_id__in=parent_ids,
        ).values_list("document_id", "tag_id"),
    )

    to_create: list = []
    affected: set[int] = set()

    for doc_id in doc_ids:
        for parent_id in parent_ids:
            if (doc_id, parent_id) in existing_pairs:
                continue

            to_create.append(
                doc_tag_relationship(document_id=doc_id, tag_id=parent_id),
            )
            affected.add(doc_id)

    if to_create:
        doc_tag_relationship.objects.bulk_create(
            to_create,
            ignore_conflicts=True,
        )

    if affected:
        bulk_update_documents.apply_async(
            kwargs={"document_ids": list(affected)},
            headers={"trigger_source": PaperlessTask.TriggerSource.SYSTEM},
        )


@shared_task
def llmindex_index(
    *,
    iter_wrapper: IterWrapper[Document] = identity,
    rebuild: bool = False,
) -> str | None:
    ai_config = AIConfig()
    if not ai_config.llm_index_enabled:  # pragma: no cover
        logger.info("LLM index is disabled, skipping update.")
        return None

    from paperless_ai.indexing import update_llm_index

    return update_llm_index(
        iter_wrapper=iter_wrapper,
        rebuild=rebuild,
    )


@shared_task(
    bind=True,
    autoretry_for=(LLMTimeoutError,),
    max_retries=3,
    retry_backoff=60,
    retry_backoff_max=600,
    retry_jitter=True,
)
def apply_ai_suggestions(self, action_id: int, document_id: int) -> None:
    """
    Deferred "apply AI suggestions" workflow action.
    """
    from documents.models import WorkflowAction
    from documents.workflows.ai import apply_ai_suggestions_to_document

    try:
        action = WorkflowAction.objects.get(pk=action_id)
        document = Document.objects.select_related("owner").get(pk=document_id)
    except (WorkflowAction.DoesNotExist, Document.DoesNotExist):
        logger.warning(
            "Workflow action %s or document %s no longer exists, "
            "not applying AI suggestions",
            action_id,
            document_id,
        )
        return

    if not apply_ai_suggestions_to_document(action, document):
        return

    # Do not run update workflows again for their own AI result.
    clear_document_caches(document.pk)
    from documents.job_context import active_ingestion_job

    if active_ingestion_job.get() is not None:
        return
    index_document.delay(document.pk)

    ai_config = AIConfig()
    if ai_config.llm_index_enabled:
        update_document_in_llm_index.apply_async_on_commit(
            kwargs={"document_id": document.pk},
        )


@shared_task
def update_document_in_llm_index(
    document: Document | None = None,
    *,
    document_id: int | None = None,
) -> None:
    """Load newly queued document IDs in the child, accepting legacy model payloads."""
    if not AIConfig().llm_index_enabled:
        return

    if document is None:
        document = Document.objects.filter(pk=document_id).first()
        if document is None:
            return

    from paperless_ai.indexing import llm_index_add_or_update_document

    llm_index_add_or_update_document(document)


@shared_task
def remove_document_from_llm_index(
    document: Document | None = None,
    *,
    document_id: int | None = None,
) -> None:
    """Remove a deleted document by ID without fetching its former database row."""
    if not AIConfig().llm_index_enabled:
        return

    from paperless_ai.indexing import llm_index_remove_document

    llm_index_remove_document(
        document_id=document_id if document_id is not None else document.pk,
    )


@shared_task
def build_share_link_bundle(bundle_id: int) -> None:
    try:
        bundle = (
            ShareLinkBundle.objects.filter(pk=bundle_id)
            .prefetch_related("documents")
            .get()
        )
    except ShareLinkBundle.DoesNotExist:
        logger.warning("Share link bundle %s no longer exists.", bundle_id)
        return

    bundle.remove_file()
    bundle.status = ShareLinkBundle.Status.PROCESSING
    bundle.last_error = None
    bundle.size_bytes = None
    bundle.built_at = None
    bundle.file_path = ""
    bundle.save(
        update_fields=[
            "status",
            "last_error",
            "size_bytes",
            "built_at",
            "file_path",
        ],
    )

    documents = list(bundle.documents.all().order_by("pk"))

    _, temp_zip_path_str = mkstemp(suffix=".zip", dir=settings.SCRATCH_DIR)
    temp_zip_path = Path(temp_zip_path_str)

    try:
        from documents.bulk_download import ArchiveOnlyStrategy
        from documents.bulk_download import OriginalsOnlyStrategy

        strategy_class = (
            ArchiveOnlyStrategy
            if bundle.file_version == ShareLink.FileVersion.ARCHIVE
            else OriginalsOnlyStrategy
        )
        with zipfile.ZipFile(temp_zip_path, "w", zipfile.ZIP_DEFLATED) as zipf:
            strategy = strategy_class(zipf)
            for document in documents:
                strategy.add_document(document)

        output_dir = settings.SHARE_LINK_BUNDLE_DIR
        output_dir.mkdir(parents=True, exist_ok=True)
        final_path = (output_dir / f"{bundle.slug}.zip").resolve()
        if final_path.exists():
            final_path.unlink()
        shutil.move(temp_zip_path, final_path)

        bundle.file_path = f"{bundle.slug}.zip"
        bundle.size_bytes = final_path.stat().st_size
        bundle.status = ShareLinkBundle.Status.READY
        bundle.built_at = timezone.now()
        bundle.last_error = None
        bundle.save(
            update_fields=[
                "file_path",
                "size_bytes",
                "status",
                "built_at",
                "last_error",
            ],
        )
        logger.info("Built share link bundle %s", bundle.pk)
    except Exception as exc:
        logger.exception(
            "Failed to build share link bundle %s: %s",
            bundle_id,
            exc,
        )
        bundle.status = ShareLinkBundle.Status.FAILED
        bundle.last_error = {
            "bundle_id": bundle_id,
            "exception_type": exc.__class__.__name__,
            "message": str(exc),
            "timestamp": timezone.now().isoformat(),
        }
        bundle.save(update_fields=["status", "last_error"])
        try:
            temp_zip_path.unlink()
        except OSError:
            pass
        raise
    finally:
        try:
            temp_zip_path.unlink(missing_ok=True)
        except OSError:
            pass


@shared_task
def cleanup_expired_share_link_bundles() -> None:
    now = timezone.now()
    expired_qs = ShareLinkBundle.objects.filter(
        expiration__isnull=False,
        expiration__lt=now,
    )
    count = 0
    for bundle in expired_qs.iterator():
        count += 1
        try:
            bundle.delete()
        except Exception as exc:
            logger.warning(
                "Failed to delete expired share link bundle %s: %s",
                bundle.pk,
                exc,
            )
    if count:
        logger.info("Deleted %s expired share link bundle(s)", count)
