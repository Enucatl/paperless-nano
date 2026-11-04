"""Required workflow effects survive commit without unsafe automatic replay."""

from __future__ import annotations

import json
from datetime import timedelta
from io import StringIO
from typing import TYPE_CHECKING

import pytest
from django.core.management import call_command
from django.db import transaction
from django.utils import timezone

from documents.ingestion_jobs import NeedsAttention
from documents.job_callbacks import finish_ingestion_followups
from documents.job_callbacks import queue_ingestion_followup
from documents.job_context import active_ingestion_job
from documents.jobs import recover_document_jobs
from documents.models import Document
from documents.models import DocumentJob
from documents.models import Workflow
from documents.models import WorkflowAction
from documents.models import WorkflowActionWebhook
from documents.models import WorkflowTrigger
from documents.tasks import apply_ai_suggestions
from documents.workflows.runner import run_workflows
from documents.workflows.webhooks import send_webhook
from paperless_ai.exceptions import LLMTimeoutError
from paperless_testing.factories import DocumentFactory
from paperless_testing.factories import UserFactory
from paperless_testing.permissions import grant_global

if TYPE_CHECKING:
    from pytest_django.fixtures import Settings
    from pytest_mock import MockerFixture

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("paperless_dirs"),
]


class WorkerStopped(BaseException):
    """Terminate after an external effect without ordinary failure handling."""


@pytest.fixture
def parent() -> DocumentJob:
    """Represent a committed ingestion with workflow completion still pending."""
    return DocumentJob.objects.create(
        kind="consume",
        status=DocumentJob.Status.COMMITTED,
        timeout=60,
        input={"overrides": {}, "root_document_id": None},
        result={"payload": {"document_id": 1}},
    )


def queue(parent: DocumentJob, task=send_webhook, **kwargs) -> str:
    """Record an effect under its owning ingestion and return its stable ID."""
    token = active_ingestion_job.set(str(parent.pk))
    try:
        assert queue_ingestion_followup(task, kwargs=kwargs)
    finally:
        active_ingestion_job.reset(token)
    parent.refresh_from_db()
    return parent.result["workflow_followups"][-1]["id"]


def test_unrelated_workflows_keep_existing_dispatch_policy() -> None:
    """The durable queue is restricted to the active ingestion operation."""
    assert not queue_ingestion_followup(
        send_webhook, kwargs={"url": "https://example.org"}
    )
    assert not DocumentJob.objects.exists()


def test_serialized_attachment_and_duplicate_completion(
    parent: DocumentJob,
    mocker: MockerFixture,
) -> None:
    """Attachment bytes and the completed receipt outlive their original caller."""
    files = {"file": ("invoice.pdf", b"original attachment", "application/pdf")}
    identifier = queue(parent, url="https://example.org", files=files)
    effect = mocker.patch.object(send_webhook, "_orig_run", return_value=None)
    publish = mocker.patch.object(send_webhook, "apply_async")
    json.dumps(parent.result)

    finish_ingestion_followups(parent)
    finish_ingestion_followups(parent)

    effect.assert_called_once_with(
        url="https://example.org", files={"file": list(files["file"])}
    )
    publish.assert_not_called()
    assert DocumentJob.objects.get(pk=identifier).status == DocumentJob.Status.SUCCEEDED


@pytest.mark.parametrize("failure", [WorkerStopped, RuntimeError])
def test_ambiguous_webhook_is_not_repeated(
    parent: DocumentJob,
    mocker: MockerFixture,
    failure: type[BaseException],
) -> None:
    """Unknown HTTP outcomes keep the parent pending and require reconciliation."""
    identifier = queue(parent, url="https://example.org")
    effect = mocker.patch.object(send_webhook, "_orig_run", side_effect=failure)
    with pytest.raises(WorkerStopped if failure is WorkerStopped else NeedsAttention):
        finish_ingestion_followups(parent)
    with pytest.raises(NeedsAttention):
        finish_ingestion_followups(parent)
    effect.assert_called_once()
    assert DocumentJob.objects.get(pk=identifier).status == DocumentJob.Status.ATTENTION


def test_ai_timeout_retries_only_before_mutation(
    parent: DocumentJob,
    mocker: MockerFixture,
) -> None:
    """Safe inference timeouts retry durably without Celery autoretry publication."""
    identifier = queue(parent, apply_ai_suggestions, action_id=1, document_id=1)
    effect = mocker.patch.object(
        apply_ai_suggestions, "_orig_run", side_effect=LLMTimeoutError
    )
    publish = mocker.patch.object(apply_ai_suggestions, "apply_async")
    for _ in range(3):
        with pytest.raises(LLMTimeoutError):
            finish_ingestion_followups(parent)
        DocumentJob.objects.filter(pk=identifier).update(
            updated=timezone.now() - timedelta(minutes=10)
        )
        recover_document_jobs()
        assert (
            DocumentJob.objects.get(pk=identifier).status == DocumentJob.Status.QUEUED
        )
    with pytest.raises(NeedsAttention, match="exhausted"):
        finish_ingestion_followups(parent)
    assert effect.call_count == 4
    publish.assert_not_called()


def test_acknowledgment_skips_uncertain_effect_but_runs_pending_effect(
    parent: DocumentJob,
    mocker: MockerFixture,
) -> None:
    """Operator acknowledgment never discards an effect that has not started."""
    uncertain = queue(parent, url="https://example.org/uncertain")
    effect = mocker.patch.object(
        send_webhook, "_orig_run", side_effect=RuntimeError, return_value=None
    )
    with pytest.raises(NeedsAttention):
        finish_ingestion_followups(parent)
    queue(parent, url="https://example.org/pending")
    parent.status = DocumentJob.Status.ATTENTION
    parent.save(update_fields=["status"])
    mocker.patch("documents.ingestion_jobs.dispatch_consumption")
    stdout = StringIO()

    call_command(
        "document_jobs",
        job=str(parent.pk),
        resume=True,
        acknowledge_effects=True,
        stdout=stdout,
    )
    parent.refresh_from_db()
    effect.reset_mock(side_effect=True)
    finish_ingestion_followups(parent)

    effect.assert_called_once_with(url="https://example.org/pending")
    assert DocumentJob.objects.get(pk=uncertain).result["acknowledged"]
    assert len(json.loads(stdout.getvalue())["workflow_followups"]) == 2


def test_workflow_intents_commit_with_document_state(
    parent: DocumentJob,
    mocker: MockerFixture,
) -> None:
    """Both existing workflow scheduling sites write intents before dispatch."""
    document = DocumentFactory()
    trigger = WorkflowTrigger.objects.create(
        type=WorkflowTrigger.WorkflowTriggerType.DOCUMENT_ADDED
    )
    webhook = WorkflowActionWebhook.objects.create(
        url="https://example.org", body="body", use_params=False
    )
    ai = WorkflowAction.objects.create(
        type=WorkflowAction.WorkflowActionType.APPLY_AI_SUGGESTIONS
    )
    hook = WorkflowAction.objects.create(
        type=WorkflowAction.WorkflowActionType.WEBHOOK, webhook=webhook
    )
    workflow = Workflow.objects.create(name="Required completion")
    workflow.triggers.add(trigger)
    workflow.actions.add(ai, hook)
    ai_publish = mocker.patch.object(apply_ai_suggestions, "delay_on_commit")
    hook_publish = mocker.patch.object(send_webhook, "apply_async")
    token = active_ingestion_job.set(str(parent.pk))
    try:
        with pytest.raises(RuntimeError, match="rollback"):
            with transaction.atomic():
                run_workflows(trigger.type, document)
                parent.refresh_from_db()
                assert len(parent.result["workflow_followups"]) == 2
                raise RuntimeError("rollback")
    finally:
        active_ingestion_job.reset(token)
    parent.refresh_from_db()
    assert "workflow_followups" not in parent.result
    ai_publish.assert_not_called()
    hook_publish.assert_not_called()


@pytest.mark.parametrize("change", ["edit", "revoke"])
def test_ai_rechecks_revision_and_actor_after_inference(
    parent: DocumentJob,
    mocker: MockerFixture,
    settings: Settings,
    change: str,
) -> None:
    """An inference response cannot overwrite a newer edit or revoked admission."""
    settings.AI_ENABLED = True
    actor = UserFactory()
    grant_global(actor, "documents.add_document")
    document = DocumentFactory(title="original", content="source text", owner=actor)
    parent.input["overrides"] = {"actor_id": actor.pk}
    parent.save(update_fields=["input"])
    action = WorkflowAction.objects.create(
        type=WorkflowAction.WorkflowActionType.APPLY_AI_SUGGESTIONS,
        ai_suggestion_fields=[WorkflowAction.AISuggestionField.TITLE],
        ai_overwrite_existing=True,
    )

    def inference(*args, **kwargs):
        """Change trusted state while the provider is processing the snapshot."""
        if change == "edit":
            Document.objects.filter(pk=document.pk).update(title="newer user edit")
        else:
            actor.is_active = False
            actor.save(update_fields=["is_active"])
        return {"title": "stale AI title"}

    mocker.patch(
        "paperless_ai.ai_classifier.get_ai_document_classification",
        side_effect=inference,
    )
    mocker.patch(
        "paperless_ai.ai_classifier.get_llm_output_language", return_value="en"
    )
    index = mocker.patch("documents.tasks.index_document.delay")
    queue(parent, apply_ai_suggestions, action_id=action.pk, document_id=document.pk)
    with pytest.raises(NeedsAttention):
        finish_ingestion_followups(parent)
    document.refresh_from_db()
    assert document.title == ("newer user edit" if change == "edit" else "original")
    index.assert_not_called()


def test_ai_completion_defers_indexes_to_parent(
    parent: DocumentJob,
    mocker: MockerFixture,
) -> None:
    """AI completion cannot introduce another unjournaled index dispatch."""
    action = WorkflowAction.objects.create(
        type=WorkflowAction.WorkflowActionType.APPLY_AI_SUGGESTIONS
    )
    document = DocumentFactory()
    mocker.patch(
        "documents.workflows.ai.apply_ai_suggestions_to_document",
        return_value=["title"],
    )
    index = mocker.patch("documents.tasks.index_document.delay")
    ai_index = mocker.patch(
        "documents.tasks.update_document_in_llm_index.apply_async_on_commit"
    )
    queue(parent, apply_ai_suggestions, action_id=action.pk, document_id=document.pk)
    finish_ingestion_followups(parent)
    index.assert_not_called()
    ai_index.assert_not_called()
