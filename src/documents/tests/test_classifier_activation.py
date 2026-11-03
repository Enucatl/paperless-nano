from concurrent.futures import ThreadPoolExecutor
from datetime import UTC
from datetime import datetime
from pathlib import Path
from threading import Event

import pytest
from django.core.cache import cache

from documents.caching import CLASSIFIER_HASH_KEY
from documents.classifier import ClassifierModelCorruptError
from documents.classifier import DocumentClassifier
from documents.classifier import _SignedFileWriter
from documents.classifier import active_model_identity
from documents.classifier import load_classifier
from documents.models import Document
from documents.models import Tag


@pytest.fixture
def model_file(settings, tmp_path: Path) -> Path:
    """Keep activation files in the disposable test directory."""
    settings.MODEL_FILE = tmp_path / "model.pickle"
    return settings.MODEL_FILE


def fitted_classifier(label: bytes = b"old") -> DocumentClassifier:
    """Build a small serializable model with distinguishable training metadata."""
    classifier = DocumentClassifier()
    classifier.last_auto_type_hash = label
    classifier.last_doc_change_time = datetime(2026, 1, 1, tzinfo=UTC)
    return classifier


def test_activation_identity_tracks_verified_bytes(model_file: Path) -> None:
    """A reader keeps its verified identity across later model activation."""
    old = fitted_classifier()
    old.save()
    reader = load_classifier(raise_exception=True)
    assert reader.model_identity == old.model_identity == active_model_identity()

    replacement = fitted_classifier(b"replacement")
    replacement.save()
    assert reader.model_identity != active_model_identity()
    assert replacement.model_identity == active_model_identity()
    assert (
        load_classifier(raise_exception=True).model_identity
        == replacement.model_identity
    )


def test_failed_activation_preserves_model_and_cache(model_file: Path, mocker) -> None:
    """An interrupted write cannot activate its model or advertise new metadata."""
    old = fitted_classifier()
    old.save()
    original = model_file.read_bytes()
    replacement = fitted_classifier(b"replacement")
    mocker.patch("documents.classifier.pickle.dump", side_effect=OSError("disk full"))

    with pytest.raises(OSError, match="disk full"):
        replacement.save()

    assert model_file.read_bytes() == original
    assert cache.get(CLASSIFIER_HASH_KEY) == old.last_auto_type_hash.hex()
    assert replacement.model_identity is None
    assert list(model_file.parent.glob("*.part")) == []


def test_overlapping_writers_keep_independent_staging(model_file: Path) -> None:
    """Each writer publishes only its own complete payload and signature."""
    with _SignedFileWriter(model_file, DocumentClassifier._new_hmac()) as first:
        first.write(b"first")
        with _SignedFileWriter(model_file, DocumentClassifier._new_hmac()) as second:
            second.write(b"second")
        assert (
            model_file.read_bytes()
            == DocumentClassifier._compute_hmac(b"second") + b"second"
        )
        first.write(b" end")
    assert (
        model_file.read_bytes()
        == DocumentClassifier._compute_hmac(b"first end") + b"first end"
    )
    assert list(model_file.parent.glob("*.part")) == []


def test_corrupt_load_cannot_delete_concurrent_activation(
    model_file: Path, mocker
) -> None:
    """A corrupt reader cannot remove a model activated after its snapshot."""
    model_file.write_bytes(b"corrupt")
    loading = Event()
    release_load = Event()
    saving = Event()

    def fail_load() -> None:
        """Hold the reader while the other thread starts activation."""
        loading.set()
        assert release_load.wait(5)
        raise ClassifierModelCorruptError

    def activate() -> None:
        """Publish a replacement after the corrupt reader has its snapshot."""
        assert loading.wait(5)
        saving.set()
        fitted_classifier(b"replacement").save()

    mocker.patch.object(DocumentClassifier, "load", side_effect=fail_load)
    with ThreadPoolExecutor(max_workers=2) as executor:
        reader = executor.submit(load_classifier)
        writer = executor.submit(activate)
        try:
            assert saving.wait(5)
            writer.result(timeout=5)
        finally:
            release_load.set()
        assert reader.result(timeout=5) is None
        writer.result(timeout=5)
    mocker.stopall()
    assert load_classifier(raise_exception=True).last_auto_type_hash == b"replacement"


@pytest.mark.django_db
def test_training_does_not_publish_before_activation(model_file: Path) -> None:
    """Training remains private until the signed model has been activated."""
    old = fitted_classifier()
    old.save()
    tag = Tag.objects.create(name="auto", matching_algorithm=Tag.MATCH_AUTO)
    document = Document.objects.create(title="Invoice", content="invoice total")
    document.tags.add(tag)
    replacement = load_classifier(raise_exception=True)

    assert replacement.train()
    assert replacement.model_identity is None
    assert active_model_identity() == old.model_identity
    assert cache.get(CLASSIFIER_HASH_KEY) == old.last_auto_type_hash.hex()
    replacement.save()
    assert active_model_identity() == replacement.model_identity
    assert cache.get(CLASSIFIER_HASH_KEY) == replacement.last_auto_type_hash.hex()


@pytest.mark.django_db
def test_unchanged_old_model_cannot_republish_cache(model_file: Path) -> None:
    """A training reader cannot overwrite cache metadata for a newer activation."""
    tag = Tag.objects.create(name="auto", matching_algorithm=Tag.MATCH_AUTO)
    document = Document.objects.create(title="Invoice", content="invoice total")
    document.tags.add(tag)
    reader = DocumentClassifier()
    assert reader.train()
    reader.save()
    replacement = fitted_classifier(b"replacement")
    replacement.save()

    assert not reader.train()
    assert active_model_identity() == replacement.model_identity
    assert cache.get(CLASSIFIER_HASH_KEY) == replacement.last_auto_type_hash.hex()


def test_valid_model_loading_needs_no_write_lock(model_file: Path, mocker) -> None:
    """Inference still loads a readable model from read-only model storage."""
    old = fitted_classifier()
    old.save()
    mocker.patch(
        "documents.classifier.classifier_model_lock",
        side_effect=PermissionError("read-only model storage"),
    )
    assert load_classifier(raise_exception=True).model_identity == old.model_identity


def test_unreadable_activation_token_is_unavailable(model_file: Path, mocker) -> None:
    """Unavailable model metadata does not prevent rule-based suggestions."""
    mocker.patch.object(Path, "open", side_effect=PermissionError("no access"))
    assert active_model_identity() is None
