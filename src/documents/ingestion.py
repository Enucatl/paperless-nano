"""Explicit preparation and publication operations for the ingestion coordinator."""

import dataclasses
import datetime
from contextlib import nullcontext
from pathlib import Path

import magic
from django.conf import settings
from django.db import transaction
from django.utils import timezone

from documents.data_models import DocumentMetadataOverrides
from documents.ingestion_state import transition
from documents.job_storage import atomic_copy as _copy
from documents.job_storage import job_directory
from documents.models import DocumentJob
from documents.models import PaperlessTask
from documents.operation_journal import invoke
from documents.plugins.helpers import ProgressManager
from documents.plugins.pipeline import consumption_plugins
from documents.utils import copy_basic_file_stats
from paperless.parsers.contract import file_reference
from paperless.parsers.contract import resolve_reference


def metadata_from_json(value: dict) -> DocumentMetadataOverrides:
    """Restore typed metadata at the existing plugin boundary."""
    value = value.copy()
    if value.get("created"):
        value["created"] = datetime.date.fromisoformat(value["created"])
    if value.get("custom_fields"):
        value["custom_fields"] = {
            int(key): item for key, item in value["custom_fields"].items()
        }
    return DocumentMetadataOverrides(**value)


def prepare(job: DocumentJob, task_id: str) -> None:
    """Retain each plugin outcome before advancing the durable Prepared boundary."""
    from documents.consumer import AsnCheckPlugin
    from documents.consumer import ConsumeFileDuplicateError
    from documents.consumer import ConsumerPlugin
    from documents.consumer import ConsumerPreflightPlugin
    from documents.ingestion_state import restore_input
    from documents.operation_journal import NeedsAttention
    from documents.operation_journal import json_value
    from documents.plugins.base import NoCleanupPluginMixin
    from documents.plugins.base import StopConsumeTaskError

    started = timezone.now()
    directory = job_directory(job.pk)
    input_doc, metadata = restore_input(job)
    input_doc.mime_type = magic.from_file(input_doc.original_file, mime=True)
    if job.result.get("scripts", {}).get("pre") != settings.PRE_CONSUME_SCRIPT:
        raise NeedsAttention("Pre-consume script configuration changed")
    receipt = PaperlessTask.objects.filter(task_id=task_id).first()
    if receipt is not None:
        receipt.input_data["mime_type"] = input_doc.mime_type
        receipt.save(update_fields=["input_data"])
    plugins = consumption_plugins(input_doc)
    with ProgressManager(
        metadata.filename or job.input["filename"], task_id
    ) as progress:
        for index, cls in enumerate(plugins):
            key = f"prepare:{index}"
            identity = (
                f"{cls.__module__}.{cls.__qualname__}:{getattr(cls, 'version', '1')}"
            )
            temporary = directory / f"plugin-{index}"
            temporary.mkdir(exist_ok=True)
            plugin = cls(input_doc, metadata, progress, temporary, task_id)
            inputs = {
                "metadata": json_value(dataclasses.asdict(metadata)),
                "mime_type": input_doc.mime_type,
                "source": file_reference(directory, input_doc.original_file),
            }

            def execute(identifier: str) -> dict:
                """Validate and retain public outcomes before plugin cleanup."""
                plugin.invocation_id = identifier
                retained_input = resolve_reference(directory, inputs["source"])
                working_input = temporary / retained_input.name
                _copy(retained_input, working_input)
                copy_basic_file_stats(retained_input, working_input)
                input_doc.original_file = working_input
                if not plugin.able_to_run:
                    return {
                        "metadata": inputs["metadata"],
                        "source": inputs["source"],
                        "mime_type": input_doc.mime_type,
                        "skipped": True,
                    }
                stopped = None
                prepared = None
                try:
                    plugin.setup()
                    if cls is ConsumerPlugin:

                        def hook(name, action, safe):
                            """Supply each hook its own stable invocation identity."""

                            def execute_hook(identifier):
                                previous = plugin.invocation_id
                                plugin.invocation_id = identifier
                                try:
                                    action()
                                finally:
                                    plugin.invocation_id = previous

                            return invoke(
                                job,
                                f"{key}:{name}",
                                name,
                                {
                                    "script": job.result.get("scripts", {}).get("pre")
                                    if name == "pre_consume"
                                    else None
                                },
                                execute_hook,
                                replay_safe=safe,
                            )

                        plugin.invoke = hook
                        prepared = plugin.prepare()
                        message = None
                    else:
                        message = plugin.run()
                except StopConsumeTaskError as error:
                    stopped = {"reason": error.message}
                    message = None
                except ConsumeFileDuplicateError as error:
                    stopped = {
                        "duplicate_of": error.duplicate_id,
                        "duplicate_in_trash": error.in_trash,
                    }
                    message = None
                if message is not None and not isinstance(message, (str, dict)):
                    raise ValueError("Plugin returned an invalid message")
                source = None
                if input_doc.original_file.exists():
                    retained = (
                        directory / f"input-{index}" / input_doc.original_file.name
                    )
                    _copy(input_doc.original_file, retained)
                    copy_basic_file_stats(input_doc.original_file, retained)
                    source = file_reference(directory, retained)
                elif stopped is None:
                    raise ValueError(
                        "Plugin removed its input without stopping ingestion"
                    )
                result = {
                    "metadata": json_value(dataclasses.asdict(plugin.metadata)),
                    "source": source,
                    "mime_type": input_doc.mime_type,
                    "stopped": stopped,
                    "message": message,
                }
                if prepared is not None:
                    result["prepared"] = {
                        **prepared,
                        **{
                            name: file_reference(directory, Path(prepared[name]))
                            if prepared[name]
                            else None
                            for name in ("original", "thumbnail", "archive")
                        },
                    }
                return result

            # Only built-ins without opaque effects may repeat by default.
            safe = (
                cls is AsnCheckPlugin
                or (
                    cls is ConsumerPreflightPlugin
                    and not settings.CONSUMER_DELETE_DUPLICATES
                )
                or cls.__dict__.get("replay_safe", False) is True
            )
            if cls is ConsumerPlugin:
                # Hooks and parser calls retain their own finite invocation outcomes.
                safe = True

            def validate_outcome(outcome: dict) -> None:
                """Validate recovered outcomes before recording invocation success."""
                if not isinstance(outcome, dict) or not isinstance(
                    outcome.get("metadata"), dict
                ):
                    raise ValueError("Invalid plugin metadata outcome")
                returned_metadata = json_value(
                    dataclasses.asdict(metadata_from_json(outcome["metadata"]))
                )
                if "policy" in job.result.get("invocations", {}).get(key, {}):
                    for name, value in inputs["metadata"].items():
                        if (
                            name not in {"title", "created", "filename", "barcodes"}
                            and returned_metadata.get(name) != value
                        ):
                            raise ValueError(
                                f"Remote plugin changed protected metadata: {name}"
                            )
                if not isinstance(outcome.get("mime_type"), str):
                    raise ValueError("Invalid plugin MIME type")
                if outcome.get("source"):
                    resolve_reference(directory, outcome["source"])
                elif not outcome.get("stopped"):
                    raise ValueError("Plugin outcome has no retained source")
                if "prepared" in outcome:
                    prepared_result = outcome["prepared"]
                    if not isinstance(prepared_result.get("text"), str):
                        raise ValueError("Prepared text must be a string")
                    for name in ("original", "thumbnail", "archive"):
                        if name != "archive" or prepared_result[name]:
                            resolve_reference(directory, prepared_result[name])

            result = invoke(
                job,
                key,
                identity,
                inputs,
                execute,
                replay_safe=safe,
                reconcile=plugin.reconcile,
                validate=validate_outcome,
                allow_deferred=cls is not ConsumerPlugin,
            )
            if (
                not isinstance(result, dict)
                or "metadata" not in result
                or "source" not in result
            ):
                raise NeedsAttention(
                    f"Plugin {identity} returned an invalid recovery result"
                )
            metadata = metadata_from_json(result["metadata"])
            if result["source"]:
                input_doc.original_file = resolve_reference(directory, result["source"])
                input_doc.mime_type = result["mime_type"]
            plugin.metadata = metadata
            # Cleanup has its own outcome; opaque cleanup is never blindly repeated.
            import shutil

            from documents.barcodes import BarcodePlugin

            def cleanup(_identifier: str) -> None:
                """Clean only the built-in barcode workspace without recreating runtime state."""
                if result.get("skipped"):
                    return
                if cls is BarcodePlugin:
                    shutil.rmtree(temporary, ignore_errors=False)
                else:
                    plugin.cleanup()

            invoke(
                job,
                key + ":cleanup",
                identity,
                {},
                cleanup,
                replay_safe=cls is BarcodePlugin
                or type(plugin).cleanup is NoCleanupPluginMixin.cleanup
                or getattr(plugin, "cleanup_replay_safe", False) is True,
            )
            job.refresh_from_db()
            if result.get("stopped") is not None:
                job.result.update(
                    payload=result["stopped"], remove_source=result["source"] is None
                )
                job.save(update_fields=["result"])
                break
            if "prepared" in result:
                job.result["prepared"] = result["prepared"]
                job.result["metadata"] = result["metadata"]
                job.save(update_fields=["result"])
        if job.timeout and (timezone.now() - started).total_seconds() > job.timeout:
            raise TimeoutError("Ingestion exceeded its recorded timeout")
        transition(job, "prepared")


def publish(job: DocumentJob, task_id: str) -> None:
    """Commit document, permanent references and completion intents atomically."""
    from documents.consumer import ConsumerPlugin
    from documents.ingestion_state import commit_consumption
    from documents.ingestion_state import restore_input
    from documents.ingestion_state import validate_application
    from documents.ingestion_storage import reconcile_publication

    reconcile_publication(job)
    job.refresh_from_db()
    if "prepared" not in job.result:
        transition(job, "committed")
        return
    directory = job_directory(job.pk)
    prepared = job.result["prepared"]
    prepared = {
        **prepared,
        **{
            name: str(resolve_reference(directory, prepared[name]))
            if prepared[name]
            else None
            for name in ("original", "thumbnail", "archive")
        },
    }
    input_doc, _ = restore_input(job)
    metadata = metadata_from_json(job.result["metadata"])
    with ProgressManager(
        metadata.filename or job.input["filename"], task_id
    ) as progress:
        plugin = ConsumerPlugin(input_doc, metadata, progress, directory, task_id)
        with transaction.atomic():
            # The process lock serializes execution; row locking protects the commit checkpoint.
            job = DocumentJob.objects.select_for_update().get(pk=job.pk)
            if job.status != DocumentJob.Status.PREPARED:
                raise ValueError("Publication requires a Prepared job")
            validate_application(job.pk)
            document = plugin.publish(prepared)
            commit_consumption(str(job.pk), document)


def complete_document(job: DocumentJob) -> None:
    """Execute explicitly owned post-commit metadata, workflow and extension steps."""
    from documents.consumer import ConsumerPlugin
    from documents.consumer import load_classifier
    from documents.models import Document
    from documents.models import WorkflowTrigger
    from documents.signals import document_consumption_finished
    from documents.signals import document_updated
    from documents.signals import handlers
    from documents.workflows.runner import run_workflows

    identifier = job.result.get("payload", {}).get("document_id")
    if identifier is None:
        return
    from documents.classification_operations import configured_provider
    from documents.classification_operations import provider_configuration
    from documents.job_state import execution_policy
    from documents.operation_journal import json_value

    if "enrichment" not in job.result:
        deferred = None
        if (
            settings.CLASSIFIER_ENABLED
            and execution_policy("metadata.classify", settings.CLASSIFICATION_PROVIDER)
            is not None
        ):
            provider = configured_provider()
            if provider.identity is not None:
                deferred = {
                    "identity": list(provider.identity),
                    "configuration": json_value(provider_configuration()),
                }
        job.result["enrichment"] = deferred
        job.save(update_fields=["result"])
    deferred = job.result["enrichment"]
    classification = {}

    def classifier():
        """Load the selected classifier once, only when completion needs it."""
        if deferred is not None:
            return None
        if "provider" not in classification:
            classification["provider"] = load_classifier()
        return classification["provider"]

    steps = (
        "inbox",
        "correspondent",
        "type",
        "tags",
        "storage",
        "workflows",
        "version",
        "signal",
        "enrichment",
    )
    for name in steps:
        document = Document.objects.filter(pk=identifier).first()
        if document is None:
            continue

        def execute(invocation_id: str) -> None:
            """Use current committed state for a named completion operation."""
            if name in {"inbox", "correspondent", "type", "tags", "storage"}:
                function = {
                    "inbox": handlers.add_inbox_tags,
                    "correspondent": handlers.set_correspondent,
                    "type": handlers.set_document_type,
                    "tags": handlers.set_tags,
                    "storage": handlers.set_storage_path,
                }[name]
                function(
                    sender=ConsumerPlugin,
                    document=document,
                    classifier=classifier() if name != "inbox" else None,
                )
            elif name == "workflows":
                run_workflows(
                    WorkflowTrigger.WorkflowTriggerType.DOCUMENT_ADDED,
                    document,
                )
            elif name == "version" and document.root_document_id:
                root = document.root_document
                run_workflows(
                    WorkflowTrigger.WorkflowTriggerType.DOCUMENT_UPDATED, root
                )
                document_updated.send(
                    sender=ConsumerPlugin, document=root, skip_ai_index=True
                )
            elif name == "enrichment" and deferred is not None:
                from uuid import uuid5

                from documents.enrichment_jobs import submit_enrichment

                with transaction.atomic():
                    submit_enrichment(
                        document.pk,
                        actor_id=job.input["overrides"].get("actor_id"),
                        job_id=uuid5(job.pk, "metadata"),
                        expected_provider=tuple(deferred["identity"]),
                        expected_configuration=deferred["configuration"],
                    )
            elif name == "signal":
                document_consumption_finished.send(
                    sender=ConsumerPlugin,
                    document=document,
                    classifier=classifier(),
                    original_file=document.source_path,
                    coordinated=True,
                    invocation_id=invocation_id,
                )

        def validate_completion(result) -> None:
            """Require a receipt rather than remote core metadata mutations."""
            if result is not None:
                raise ValueError("Completion effects must return null")

        with transaction.atomic() if name == "enrichment" else nullcontext():
            invoke(
                job,
                "complete:" + name,
                "paperless." + name,
                {"document_id": identifier},
                execute,
                allow_deferred=name in {"workflows", "signal"},
                validate=validate_completion,
                replay_safe=name in {"inbox", "enrichment"}
                or (
                    name in {"correspondent", "type", "tags", "storage"}
                    and not settings.CLASSIFICATION_PROVIDER
                ),
            )
