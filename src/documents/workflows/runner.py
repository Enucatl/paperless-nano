"""Explicit workflow matching and ordered action execution."""

from __future__ import annotations

import logging
from contextlib import nullcontext
from typing import TYPE_CHECKING

from documents import matching
from documents.models import Document
from documents.models import Workflow
from documents.models import WorkflowAction
from documents.models import WorkflowRun
from documents.models import WorkflowTrigger
from documents.updates import document_metadata_update
from documents.workflows.actions import build_workflow_action_context
from documents.workflows.actions import execute_email_action
from documents.workflows.actions import execute_move_to_trash_action
from documents.workflows.actions import execute_password_removal_action
from documents.workflows.actions import execute_webhook_action
from documents.workflows.mutations import apply_assignment_to_document
from documents.workflows.mutations import apply_assignment_to_overrides
from documents.workflows.mutations import apply_removal_to_document
from documents.workflows.mutations import apply_removal_to_overrides
from documents.workflows.utils import get_workflows_for_trigger

if TYPE_CHECKING:
    import uuid
    from pathlib import Path

    from documents.data_models import ConsumableDocument
    from documents.data_models import DocumentMetadataOverrides

# Preserve the existing logger so workflow log filtering remains compatible.
logger = logging.getLogger("paperless.handlers")


def run_workflows(
    trigger_type: WorkflowTrigger.WorkflowTriggerType,
    document: Document | ConsumableDocument,
    workflow_to_run: Workflow | None = None,
    logging_group: uuid.UUID | None = None,
    overrides: DocumentMetadataOverrides | None = None,
    original_file: Path | None = None,
) -> tuple[DocumentMetadataOverrides, str] | None:
    """
    Execute workflows matching a document for the given trigger. When `overrides` is provided
    (consumption flow), actions mutate that object and the function returns `(overrides, messages)`.
    Otherwise actions mutate the actual document and return nothing.

    Attachments for email/webhook actions use `original_file` when given, otherwise fall back to
    `document.source_path` (Document) or `document.original_file` (ConsumableDocument).

    Passing `workflow_to_run` skips the workflow query (currently only used by scheduled runs).
    """

    use_overrides = overrides is not None

    if isinstance(document, Document) and document.root_document_id is not None:
        logger.debug(
            "Skipping workflow execution for version document %s",
            document.pk,
        )
        return None

    # Track whether the caller supplied original_file. When set explicitly (e.g. by
    # run_workflows_added during consumption), it points at the staged file that has
    # not yet been moved into its final storage location. This matters for password
    # removal, which must read from the staged path rather than document.source_path.
    caller_supplied_original_file = original_file is not None
    if original_file is None:
        original_file = (
            document.source_path if not use_overrides else document.original_file
        )
    messages = []

    workflows = get_workflows_for_trigger(trigger_type, workflow_to_run)

    for workflow in workflows:
        if not use_overrides:
            if TYPE_CHECKING:
                assert isinstance(document, Document)
            try:
                # This can be called from bulk_update_documents, which may be running multiple times
                # Refresh this so the matching data is fresh and instance fields are re-freshed
                # Otherwise, this instance might be behind and overwrite the work another process did
                document.refresh_from_db()
            except Document.DoesNotExist:
                # Document was hard deleted by a previous workflow or another process
                logger.info(
                    "Document no longer exists, skipping remaining workflows",
                    extra={"group": logging_group},
                )
                break

            if not caller_supplied_original_file:
                original_file = document.source_path

            # Check if document was soft deleted (moved to trash)
            if document.is_deleted:
                logger.info(
                    "Document was moved to trash, skipping remaining workflows",
                    extra={"group": logging_group},
                )
                break

        if matching.document_matches_workflow(document, workflow, trigger_type):
            with (
                document_metadata_update(document)
                if not use_overrides
                else nullcontext()
            ):
                action: WorkflowAction
                has_move_to_trash_action = False
                for action in workflow.actions.order_by("order", "pk"):
                    message = f"Applying {action} from {workflow}"
                    if not use_overrides:
                        logger.info(message, extra={"group": logging_group})
                    else:
                        messages.append(message)

                    if action.type == WorkflowAction.WorkflowActionType.ASSIGNMENT:
                        if use_overrides and overrides:
                            apply_assignment_to_overrides(action, overrides)
                        else:
                            apply_assignment_to_document(
                                action,
                                document,
                                logging_group,
                            )
                    elif action.type == WorkflowAction.WorkflowActionType.REMOVAL:
                        if use_overrides and overrides:
                            apply_removal_to_overrides(action, overrides)
                        else:
                            apply_removal_to_document(action, document)
                    elif action.type == WorkflowAction.WorkflowActionType.EMAIL:
                        context = build_workflow_action_context(document, overrides)
                        execute_email_action(
                            action,
                            document,
                            context,
                            logging_group,
                            original_file,
                            trigger_type,
                        )
                    elif action.type == WorkflowAction.WorkflowActionType.WEBHOOK:
                        context = build_workflow_action_context(document, overrides)
                        execute_webhook_action(
                            action,
                            document,
                            context,
                            logging_group,
                            original_file,
                        )
                    elif (
                        action.type
                        == WorkflowAction.WorkflowActionType.PASSWORD_REMOVAL
                    ):
                        execute_password_removal_action(
                            action,
                            document,
                            logging_group,
                            source_file=(
                                original_file if caller_supplied_original_file else None
                            ),
                        )
                    elif action.type == WorkflowAction.WorkflowActionType.MOVE_TO_TRASH:
                        has_move_to_trash_action = True
                    elif action.type == WorkflowAction.WorkflowActionType.REMOTE_OCR:
                        if use_overrides and overrides:
                            overrides.remote_ocr = True
                        else:
                            # If a workflow has a consumption trigger *and* another type,
                            # the document has already been parsed by the time the other one fires
                            logger.debug(
                                "Remote OCR action only applies to consumption "
                                "triggers, ignoring",
                                extra={"group": logging_group},
                            )
                    elif (
                        action.type
                        == WorkflowAction.WorkflowActionType.APPLY_AI_SUGGESTIONS
                    ):
                        if use_overrides:
                            # The document has not been parsed yet, so there is no
                            # content for the LLM to make suggestions from
                            logger.debug(
                                "Apply AI suggestions action does not apply to "
                                "consumption triggers, ignoring",
                                extra={"group": logging_group},
                            )
                        else:
                            # Queued rather than run sync
                            from documents.job_callbacks import queue_ingestion_followup
                            from documents.tasks import apply_ai_suggestions

                            # kwargs so the PaperlessTask record can note the
                            # document, see _extract_input_data
                            arguments = {
                                "action_id": action.pk,
                                "document_id": document.pk,
                            }
                            if not queue_ingestion_followup(
                                apply_ai_suggestions, kwargs=arguments
                            ):
                                apply_ai_suggestions.delay_on_commit(**arguments)

                if not use_overrides:
                    # limit title to 128 characters
                    document.title = document.title[:128]
                    # Never overwrite filenames from a stale workflow instance.
                    document.save(
                        update_fields=[
                            "title",
                            "correspondent",
                            "document_type",
                            "storage_path",
                            "owner",
                            "modified",
                        ],
                    )

            WorkflowRun.objects.create(
                workflow=workflow,
                type=trigger_type,
                document=document if not use_overrides else None,
            )

            if has_move_to_trash_action:
                execute_move_to_trash_action(action, document, logging_group)

    if use_overrides:
        if TYPE_CHECKING:
            assert overrides is not None
        return overrides, "\n".join(messages)
