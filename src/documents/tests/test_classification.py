"""Contract, persistence and cache tests for replaceable classification."""

import json
from copy import deepcopy

import pytest
from django.core.cache import cache
from django.utils.functional import SimpleLazyObject

from documents.classification import ClassificationProviderError
from documents.classification import InvalidClassificationResult
from documents.classification import validate_result
from documents.classification_operations import load_classification_provider
from documents.classification_operations import proposed_assignments
from documents.classification_providers import NameTaggingProvider
from documents.models import Document
from documents.models import Tag
from documents.signals.handlers import set_tags
from paperless_testing.factories import DocumentFactory
from paperless_testing.factories import TagFactory
from paperless_testing.factories import UserFactory

PROVIDER = "documents.classification_providers.NameTaggingProvider"


@pytest.fixture
def provider(settings):
    """Select a real second provider without changing assignment code."""
    settings.CLASSIFICATION_PROVIDER = PROVIDER
    return load_classification_provider()


def test_plain_contract() -> None:
    """A provider can operate using JSON values without database access."""
    request = {
        "version": 1,
        "revision": "source-1",
        "content": "An Invoice",
        "metadata": {"title": "Example"},
        "candidates": {"tags": [{"id": 3, "name": "invoice"}]},
    }
    provider = NameTaggingProvider()
    result = provider.classify(json.loads(json.dumps(request)))
    validate_result(request, json.loads(json.dumps(result)), provider.identity)
    assert result["assignments"] == {"tags": [3]}
    for key, value in [
        ("version", 2),
        ("version", True),
        ("revision", "old"),
        ("model", "other"),
    ]:
        invalid = deepcopy(result)
        invalid[key] = value
        with pytest.raises(InvalidClassificationResult):
            validate_result(request, invalid, provider.identity)


@pytest.mark.django_db
def test_lazy_authenticated_user(provider, paperless_dirs):
    """Session authentication supplies a lazy user, not necessarily a User instance."""
    user = UserFactory()
    document = DocumentFactory(content="Invoice", owner=user)
    tag = TagFactory(name="Invoice", owner=user, matching_algorithm=Tag.MATCH_AUTO)
    _, assignments = proposed_assignments(
        document,
        "tags",
        provider,
        SimpleLazyObject(lambda: user),
    )
    assert assignments == [tag.pk]


@pytest.mark.django_db
def test_second_provider_preserves_rules_and_manual_tags(provider, paperless_dirs):
    """Provider replacement uses the existing persistence and override policy."""
    doc = DocumentFactory(content="invoice electricity")
    auto = TagFactory(name="Invoice", matching_algorithm=Tag.MATCH_AUTO)
    rule = TagFactory(
        name="Utility", match="electricity", matching_algorithm=Tag.MATCH_ANY
    )
    manual = TagFactory(name="Manual", matching_algorithm=Tag.MATCH_NONE)
    old = TagFactory(name="Old", matching_algorithm=Tag.MATCH_AUTO)
    doc.tags.add(manual, old)
    set_tags(None, doc, classifier=provider, replace=True)
    assert set(doc.tags.all()) == {manual, auto, rule}
    set_tags(None, doc, classifier=provider)
    assert set(doc.tags.all()) == {manual, auto, rule}


@pytest.mark.django_db
@pytest.mark.parametrize("failure", [TimeoutError, RuntimeError])
def test_failed_replace_can_retry_without_losing_tags(
    provider, paperless_dirs, mocker, failure
):
    """Neither transport failure nor timeout deletes the previous assignments."""
    doc = DocumentFactory(content="Invoice")
    old = TagFactory(name="Old", matching_algorithm=Tag.MATCH_AUTO)
    new = TagFactory(name="Invoice", matching_algorithm=Tag.MATCH_AUTO)
    doc.tags.add(old)
    classify = provider.classify
    mocker.patch.object(provider, "classify", side_effect=failure("failed"))
    with pytest.raises(failure):
        set_tags(None, doc, classifier=provider, replace=True)
    assert set(doc.tags.all()) == {old}
    provider.classify = classify
    set_tags(None, doc, classifier=provider, replace=True)
    assert set(doc.tags.all()) == {new}


@pytest.mark.django_db
@pytest.mark.parametrize(
    "change", ["title", "tags", "version", "provider", "permission", "model"]
)
def test_reject_changed_inputs(provider, paperless_dirs, mocker, settings, change):
    """Edits, permission revocation and provider changes invalidate inference."""
    owner = UserFactory()
    doc = DocumentFactory(content="Invoice", owner=owner)
    tag = TagFactory(name="Invoice", matching_algorithm=Tag.MATCH_AUTO, owner=owner)
    classify = provider.classify

    def changed(request):
        """Change core state after the provider received its allowed snapshot."""
        result = classify(request)
        if change == "title":
            Document.objects.filter(pk=doc.pk).update(title="User edit")
        elif change == "tags":
            doc.tags.add(tag)
        elif change == "version":
            DocumentFactory(root_document=doc, content="New content", version_index=1)
        elif change == "provider":
            settings.CLASSIFICATION_PROVIDER = ""
        elif change == "permission":
            Tag.objects.filter(pk=tag.pk).update(owner=UserFactory())
        else:
            provider.identity = ("name-tags:1", "new-model")
        return result

    mocker.patch.object(provider, "classify", side_effect=changed)
    with pytest.raises(InvalidClassificationResult):
        proposed_assignments(doc, "tags", provider)


@pytest.mark.django_db
def test_provider_cannot_widen_allowlist(provider, paperless_dirs, mocker):
    """Treat even an in-process provider's request and result as untrusted."""
    doc = DocumentFactory(content="Invoice", owner=UserFactory())
    forbidden = TagFactory(
        name="Invoice", owner=UserFactory(), matching_algorithm=Tag.MATCH_AUTO
    )
    classify = provider.classify

    def invalid(request):
        """Try to add a forbidden candidate before producing the result."""
        request["candidates"]["tags"].append(
            {"id": forbidden.pk, "name": forbidden.name}
        )
        return classify(request)

    mocker.patch.object(provider, "classify", side_effect=invalid)
    with pytest.raises(InvalidClassificationResult):
        set_tags(None, doc, classifier=provider)
    assert not doc.tags.exists()


@pytest.mark.django_db
def test_suggestions_cache_permissions_and_conditionals(
    provider, paperless_dirs, rest_api_client, mocker
):
    """Cached and conditional responses respect current users, rules and edits."""
    from guardian.shortcuts import assign_perm

    owner = UserFactory()
    assign_perm("documents.view_document", owner)
    assign_perm("documents.change_document", owner)
    other = UserFactory()
    doc = DocumentFactory(content="Invoice", owner=owner)
    tag = TagFactory(name="Invoice", owner=owner, matching_algorithm=Tag.MATCH_AUTO)
    assign_perm("documents.view_document", other, doc)
    assign_perm("documents.change_document", other, doc)
    assign_perm("documents.view_document", other)
    assign_perm("documents.change_document", other)
    rest_api_client.force_authenticate(owner)
    url = f"/api/documents/{doc.pk}/suggestions/"
    first = rest_api_client.get(url)
    assert first.status_code == 200
    assert first.data["tags"] == [tag.pk]
    mocker.patch(
        "documents.views.load_classification_provider",
        side_effect=AssertionError("cache miss"),
    )
    assert rest_api_client.get(url).data == first.data
    assert rest_api_client.get(url, HTTP_IF_NONE_MATCH=first["ETag"]).status_code == 304
    mocker.stopall()
    rest_api_client.force_authenticate(other)
    second = rest_api_client.get(url, HTTP_IF_NONE_MATCH=first["ETag"])
    assert second.status_code == 200
    assert second.data["tags"] == []
    rest_api_client.force_authenticate(owner)
    Document.objects.filter(pk=doc.pk).update(content="Changed")
    changed = rest_api_client.get(url, HTTP_IF_NONE_MATCH=first["ETag"])
    assert changed.status_code == 200
    assert changed.data["tags"] == []
    cache.clear()


@pytest.mark.django_db
@pytest.mark.parametrize(
    ("failure", "status"),
    [
        (TimeoutError, 503),
        (ClassificationProviderError, 502),
        (InvalidClassificationResult, 409),
    ],
)
@pytest.mark.parametrize("target", ["classify", "__init__"])
def test_api_failure_is_not_cached(
    provider,
    paperless_dirs,
    rest_api_client,
    mocker,
    failure,
    status,
    target,
):
    """Failed inference returns a defined error and permits a fresh retry."""
    user = UserFactory(superuser=True)
    doc = DocumentFactory(content="Invoice", owner=user)
    tag = TagFactory(name="Invoice", matching_algorithm=Tag.MATCH_AUTO, owner=user)
    rest_api_client.force_authenticate(user)
    patch = mocker.patch.object(
        NameTaggingProvider, target, side_effect=failure("failed")
    )
    url = f"/api/documents/{doc.pk}/suggestions/"
    assert rest_api_client.get(url).status_code == status
    mocker.stop(patch)
    response = rest_api_client.get(url)
    assert response.status_code == 200
    assert response.data["tags"] == [tag.pk]
    assert not doc.tags.exists()


@pytest.mark.django_db
def test_provider_configuration_change_rejects_result(provider, paperless_dirs, mocker):
    """Independent providers declare settings with the same freshness checks."""
    document = DocumentFactory(content="Invoice")
    TagFactory(name="Invoice", matching_algorithm=Tag.MATCH_AUTO)
    mocker.patch.object(
        NameTaggingProvider, "configuration", {"model": "first"}, create=True
    )
    classify = provider.classify

    def changed(request):
        result = classify(request)
        NameTaggingProvider.configuration["model"] = "second"
        return result

    mocker.patch.object(provider, "classify", side_effect=changed)
    with pytest.raises(InvalidClassificationResult):
        proposed_assignments(document, "tags", provider)


@pytest.mark.django_db
def test_builtin_activation_rejects_old_model(paperless_dirs, mocker):
    """Legacy construction uses the public contract for activation validation."""
    from documents.classifier import DocumentClassifier

    document = DocumentFactory(content="Invoice")
    tag = TagFactory(name="Invoice", matching_algorithm=Tag.MATCH_AUTO)
    classifier = DocumentClassifier()
    classifier.model_identity = "old"
    mocker.patch.object(classifier, "predict_tags", return_value=[tag.pk])
    mocker.patch("documents.classifier.active_model_identity", return_value="new")
    with pytest.raises(InvalidClassificationResult):
        proposed_assignments(document, "tags", classifier)
