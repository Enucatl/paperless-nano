"""Plain AI inference and stale-result protection across core callers."""

import json
from copy import deepcopy
from types import SimpleNamespace

import pytest
from django.test import override_settings

from documents.classification import InvalidClassificationResult
from documents.classification import validate_result
from documents.models import Document
from documents.models import WorkflowAction
from documents.workflows.ai import apply_ai_suggestions_to_document
from paperless_ai.ai_classifier import LLMClassificationProvider
from paperless_ai.ai_classifier import get_ai_document_classification
from paperless_ai.exceptions import LLMTimeoutError
from paperless_ai.taxonomy import empty_taxonomy_candidates
from paperless_testing.factories import DocumentFactory
from paperless_testing.factories import TagFactory

SUGGESTIONS = {
    "title": "Suggested title",
    "tags": {"existing_ids": [], "new_names": ["Invoice"]},
    "correspondents": {"existing_ids": [], "new_names": []},
    "document_types": {"existing_ids": [], "new_names": []},
    "storage_paths": {"existing_ids": [], "new_names": []},
    "dates": [],
}


def test_provider_accepts_serializable_snapshot_without_database() -> None:
    """Inference and the common validator require no ORM or request context."""
    suggestions = deepcopy(SUGGESTIONS)
    suggestions["tags"]["existing_ids"] = [3]
    calls = []

    def query(prompt, **kwargs):
        """Record the supplied candidate constraints."""
        calls.append((prompt, kwargs))
        return suggestions

    provider = LLMClassificationProvider(
        SimpleNamespace(run_llm_query=query),
        ("test-ai", "model-1"),
    )
    request = {
        "version": 1,
        "revision": "content-1",
        "content": "An invoice",
        "metadata": {"prompt": "Classify an invoice", "output_language_name": None},
        "candidates": {
            "tags": [{"id": 3, "name": "Invoice"}],
            "correspondents": [],
            "document_types": [],
            "storage_paths": [],
        },
    }
    result = provider.classify(json.loads(json.dumps(request)))
    validate_result(request, json.loads(json.dumps(result)), provider.identity)
    assert result["assignments"]["tags"] == [3]
    assert result["suggestions"] == suggestions
    assert calls[0][1]["allowed_candidate_ids"]["tags"] == {3}


@pytest.fixture
def ai_inference(mocker):
    """Keep integration tests focused on the provider and application boundary."""
    mocker.patch(
        "paperless_ai.ai_classifier.get_taxonomy_context",
        return_value=(empty_taxonomy_candidates(), ""),
    )
    query = mocker.Mock(return_value=deepcopy(SUGGESTIONS))
    mocker.patch(
        "paperless_ai.ai_classifier.AIClient",
        return_value=SimpleNamespace(run_llm_query=query),
    )
    return query


@pytest.mark.django_db
@pytest.mark.parametrize("change", ["title", "tags", "version"])
def test_ai_rejects_document_changes_during_inference(ai_inference, change) -> None:
    """A slow provider cannot return a successful result for an edited source."""
    document = DocumentFactory(content="An invoice")

    def query(*args, **kwargs):
        """Simulate changes that do not necessarily update the source timestamp."""
        if change == "title":
            Document.objects.filter(pk=document.pk).update(title="User edit")
        elif change == "tags":
            document.tags.add(TagFactory(name="Manual"))
        else:
            DocumentFactory(root_document=document, version_index=1, content="New text")
        return deepcopy(SUGGESTIONS)

    ai_inference.side_effect = query
    with pytest.raises(InvalidClassificationResult):
        get_ai_document_classification(document)


@pytest.mark.django_db
def test_ai_rejects_forbidden_candidate_ids(ai_inference) -> None:
    """Backend validation cannot be the only defense against forbidden IDs."""
    document = DocumentFactory(content="An invoice")
    ai_inference.return_value["tags"]["existing_ids"] = [999]
    with pytest.raises(InvalidClassificationResult):
        get_ai_document_classification(document)
    assert not document.tags.exists()


@pytest.mark.django_db
def test_ai_rejects_model_change_during_inference(ai_inference, settings) -> None:
    """Results from an obsolete configured model are not accepted."""
    document = DocumentFactory(content="An invoice")
    settings.LLM_MODEL = "before"

    def query(*args, **kwargs):
        """Activate a different model while the old provider is running."""
        settings.LLM_MODEL = "after"
        return deepcopy(SUGGESTIONS)

    ai_inference.side_effect = query
    with pytest.raises(InvalidClassificationResult, match="model changed"):
        get_ai_document_classification(document)


@pytest.mark.django_db
def test_ai_timeout_can_retry_without_applying_assignments(ai_inference) -> None:
    """A failed inference leaves source state intact for a fresh invocation."""
    document = DocumentFactory(title="Original", content="An invoice")
    ai_inference.side_effect = [LLMTimeoutError(), deepcopy(SUGGESTIONS)]
    with pytest.raises(LLMTimeoutError):
        get_ai_document_classification(document)
    assert get_ai_document_classification(document) == SUGGESTIONS
    document.refresh_from_db()
    assert document.title == "Original"
    assert not document.tags.exists()


@pytest.mark.django_db
@override_settings(AI_ENABLED=True)
def test_workflow_rechecks_revision_before_applying(mocker) -> None:
    """Update and scheduled workflows reject edits even outside ingestion jobs."""
    document = DocumentFactory(title="Original", content="An invoice")
    action = WorkflowAction.objects.create(
        type=WorkflowAction.WorkflowActionType.APPLY_AI_SUGGESTIONS,
        ai_suggestion_fields=[WorkflowAction.AISuggestionField.TITLE],
        ai_overwrite_existing=True,
    )

    def classify(*args, **kwargs):
        """Simulate a caller returning after a user saved newer metadata."""
        Document.objects.filter(pk=document.pk).update(title="User edit")
        return deepcopy(SUGGESTIONS)

    mocker.patch(
        "paperless_ai.ai_classifier.get_ai_document_classification",
        side_effect=classify,
    )
    with pytest.raises(InvalidClassificationResult):
        apply_ai_suggestions_to_document(action, document)
    document.refresh_from_db()
    assert document.title == "User edit"


@pytest.mark.django_db
def test_ai_rejects_candidate_permission_changes(ai_inference, mocker) -> None:
    """An ID allowed when inference started must still be permitted on return."""
    from paperless_testing.factories import UserFactory

    document = DocumentFactory(content="An invoice")
    tag = TagFactory(name="Invoice")
    private_owner = UserFactory()
    candidates = empty_taxonomy_candidates()
    candidates["tags"] = [{"id": tag.pk, "name": tag.name, "weight": 1.0}]
    mocker.patch(
        "paperless_ai.ai_classifier.get_taxonomy_context",
        return_value=(candidates, ""),
    )

    def query(*args, **kwargs):
        """Remove visibility without changing the classified document."""
        type(tag).objects.filter(pk=tag.pk).update(owner=private_owner)
        suggestions = deepcopy(SUGGESTIONS)
        suggestions["tags"]["existing_ids"] = [tag.pk]
        return suggestions

    ai_inference.side_effect = query
    with pytest.raises(InvalidClassificationResult, match="candidates changed"):
        get_ai_document_classification(document)


@pytest.mark.django_db
def test_ai_rejects_suggestions_that_bypass_validated_assignments(
    ai_inference,
    mocker,
) -> None:
    """The optional suggestions payload cannot carry separate unchecked IDs."""
    document = DocumentFactory(content="An invoice")
    original_classify = LLMClassificationProvider.classify

    def classify(provider, request):
        """Keep validated assignments empty while substituting a payload ID."""
        result = original_classify(provider, request)
        result["suggestions"] = deepcopy(result["suggestions"])
        result["suggestions"]["tags"]["existing_ids"] = [999]
        return result

    mocker.patch.object(LLMClassificationProvider, "classify", classify)
    with pytest.raises(InvalidClassificationResult, match="disagree"):
        get_ai_document_classification(document)


@pytest.mark.django_db
@override_settings(AI_ENABLED=True, LLM_BACKEND="mock_backend")
def test_api_resolves_new_names_with_current_privileges(
    ai_inference,
    paperless_dirs,
    rest_api_client,
) -> None:
    """Losing superuser access during inference cannot expose private taxonomy IDs."""
    from paperless_testing.factories import UserFactory

    user = UserFactory(superuser=True)
    document = DocumentFactory(content="An invoice", owner=user)
    private_tag = TagFactory(name="Invoice", owner=UserFactory())
    rest_api_client.force_authenticate(user)

    def query(*args, **kwargs):
        """Keep document ownership while revoking access to unrelated objects."""
        type(user).objects.filter(pk=user.pk).update(is_superuser=False)
        return deepcopy(SUGGESTIONS)

    ai_inference.side_effect = query
    response = rest_api_client.get(
        f"/api/documents/{document.pk}/ai_suggestions/",
    )
    assert response.status_code == 200
    assert private_tag.pk not in response.data["tags"]
    assert response.data["tags"] == []
    assert response.data["suggested_tags"] == ["Invoice"]
