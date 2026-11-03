"""Irreversible callback effects are guarded independently of broker delivery."""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING
from unittest.mock import Mock
from uuid import uuid4

import pytest
from celery.exceptions import Ignore

from documents import bulk_edit
from documents.job_callbacks import deletion_revision
from documents.job_callbacks import run_callback
from documents.jobs import RejectedResult
from documents.models import Document
from documents.models import DocumentJob
from paperless_mail.mail import apply_mail_action
from paperless_testing.factories import DocumentFactory
from paperless_testing.factories import TagFactory
from paperless_testing.factories import UserFactory
from paperless_testing.permissions import grant_global

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

    from paperless_testing.dirs import PaperlessDirs

pytestmark = pytest.mark.django_db(transaction=True)


class WorkerStopped(BaseException):
    """Stop without ordinary failure handling after an external effect."""


def test_duplicate_callback_returns_durable_result(
    paperless_dirs: PaperlessDirs,
) -> None:
    """Serializable inputs/results preserve date types and execute only once."""
    identifier = str(uuid4())
    received = datetime.datetime(2024, 1, 1, tzinfo=datetime.UTC)
    effect = Mock(return_value={"received": received})

    first = run_callback(identifier, "mail-action", (received,), {}, effect)
    second = run_callback(identifier, "mail-action", (received,), {}, effect)

    assert first == second == {"received": received}
    effect.assert_called_once()
    job = DocumentJob.objects.get(pk=identifier)
    assert job.kind == "callback"
    assert job.status == DocumentJob.Status.SUCCEEDED
    assert job.cleaned
    assert job.attempts == 1
    assert job.input["task"] == "mail-action"


@pytest.mark.parametrize("failure", [WorkerStopped, RuntimeError])
def test_ambiguous_callback_effect_is_not_repeated(
    paperless_dirs: PaperlessDirs,
    failure: type[BaseException],
) -> None:
    """Both hard termination and an ordinary failure require human reconciliation."""
    identifier = str(uuid4())
    external_effects = []

    def effect() -> None:
        """Represent an effect that happened before the worker lost its outcome."""
        external_effects.append("mail moved")
        raise failure("stopped after moving mail")

    with pytest.raises(failure):
        run_callback(identifier, "mail-action", (), {}, effect)
    with pytest.raises(Ignore):
        run_callback(identifier, "mail-action", (), {}, effect)

    assert external_effects == ["mail moved"]
    job = DocumentJob.objects.get(pk=identifier)
    assert job.status == DocumentJob.Status.ATTENTION
    assert not job.cleaned
    assert job.attempts == 1


@pytest.mark.parametrize(
    ("task", "implementation", "args"),
    [
        (bulk_edit.delete, "documents.bulk_edit._delete", ([12],)),
        (
            bulk_edit.restore_archive_serial_numbers_task,
            "documents.bulk_edit.restore_archive_serial_numbers",
            ({12: 42},),
        ),
        (
            apply_mail_action,
            "paperless_mail.mail._apply_mail_action",
            ([], 12, "mail-uid", "subject", datetime.datetime(2024, 1, 1)),
        ),
    ],
)
def test_concrete_callbacks_use_stable_celery_task_identity(
    paperless_dirs: PaperlessDirs,
    mocker: MockerFixture,
    task,
    implementation: str,
    args: tuple,
) -> None:
    """Every guarded callback reaches the same durable receipt on broker redelivery."""
    effect = mocker.patch(implementation, return_value=None)
    identifier = str(uuid4())
    task.push_request(id=identifier)
    try:
        task.run(*args)
        task.run(*args)
    finally:
        task.pop_request()

    effect.assert_called_once()
    job = DocumentJob.objects.get(pk=identifier)
    assert job.status == DocumentJob.Status.SUCCEEDED
    assert job.input["task"] == task.name


def test_asynchronous_delete_failure_is_not_reported_as_callback_success(
    paperless_dirs: PaperlessDirs,
    mocker: MockerFixture,
) -> None:
    """Retain the direct API policy while surfacing asynchronous deletion failures."""
    mocker.patch(
        "documents.search.get_backend",
        side_effect=RuntimeError("index unavailable"),
    )
    identifier = str(uuid4())
    bulk_edit.delete.push_request(id=identifier)
    try:
        with pytest.raises(RuntimeError, match="index unavailable"):
            bulk_edit.delete.run([])
        with pytest.raises(Ignore):
            bulk_edit.delete.run([])
    finally:
        bulk_edit.delete.pop_request()

    assert DocumentJob.objects.get(pk=identifier).status == DocumentJob.Status.ATTENTION


def test_delete_callback_preserves_newer_source_edits(
    paperless_dirs: PaperlessDirs,
) -> None:
    """Replacement completion cannot delete a source changed during processing."""
    document = DocumentFactory(content="admitted content")
    revision = deletion_revision(document)
    Document.objects.filter(pk=document.pk).update(content="newer user edit")
    identifier = str(uuid4())
    bulk_edit.delete.push_request(id=identifier)
    try:
        with pytest.raises(RejectedResult, match="changed after admission"):
            bulk_edit.delete.run(
                [document.pk],
                expected_revisions={str(document.pk): revision},
            )
    finally:
        bulk_edit.delete.pop_request()

    assert Document.objects.get(pk=document.pk).content == "newer user edit"
    assert DocumentJob.objects.get(pk=identifier).status == DocumentJob.Status.ATTENTION


@pytest.mark.parametrize("change", ["title", "tag"])
def test_delete_callback_protects_metadata_without_modified_bump(
    paperless_dirs: PaperlessDirs,
    change: str,
) -> None:
    """Queryset metadata and relation writes are revisions even without auto_now."""
    document = DocumentFactory()
    revision = deletion_revision(document)
    if change == "title":
        Document.objects.filter(pk=document.pk).update(title="new title")
    else:
        Document.tags.through.objects.create(
            document_id=document.pk,
            tag_id=TagFactory().pk,
        )
    identifier = str(uuid4())
    bulk_edit.delete.push_request(id=identifier)
    try:
        with pytest.raises(RejectedResult, match="changed after admission"):
            bulk_edit.delete.run(
                [document.pk],
                expected_revisions={str(document.pk): revision},
            )
    finally:
        bulk_edit.delete.pop_request()

    document.refresh_from_db()
    assert document.modified.isoformat() == revision["fields"]["modified"]
    assert Document.objects.filter(pk=document.pk).exists()


def test_delete_callback_rechecks_permission_before_deletion(
    paperless_dirs: PaperlessDirs,
) -> None:
    """Revoking the initiating user's delete permission protects the original."""
    user = UserFactory()
    grant_global(user, "delete_document")
    document = DocumentFactory(owner=user)
    revision = deletion_revision(document)
    user.user_permissions.clear()
    identifier = str(uuid4())
    bulk_edit.delete.push_request(id=identifier)
    try:
        with pytest.raises(RejectedResult, match="can no longer delete"):
            bulk_edit.delete.run(
                [document.pk],
                expected_revisions={str(document.pk): revision},
                actor_id=user.pk,
            )
    finally:
        bulk_edit.delete.pop_request()

    assert Document.objects.filter(pk=document.pk).exists()
    assert DocumentJob.objects.get(pk=identifier).status == DocumentJob.Status.ATTENTION


def test_restoring_released_asn_preserves_newer_assignment(
    paperless_dirs: PaperlessDirs,
) -> None:
    """An error callback may restore an empty ASN but cannot replace a user's edit."""
    edited = DocumentFactory(archive_serial_number=99)
    released = DocumentFactory(archive_serial_number=None)

    bulk_edit.restore_archive_serial_numbers({edited.pk: 12, released.pk: 13})

    edited.refresh_from_db()
    released.refresh_from_db()
    assert edited.archive_serial_number == 99
    assert released.archive_serial_number == 13


def test_immutable_asn_errback_ignores_prepended_failure_id(
    paperless_dirs: PaperlessDirs,
) -> None:
    """Celery's old-style errback task ID must not replace the ASN backup argument."""
    document = DocumentFactory(archive_serial_number=None)

    result = bulk_edit.restore_archive_serial_numbers_task.si(
        {document.pk: 42},
    ).apply(args=("failed-task-id",), throw=True)

    document.refresh_from_db()
    assert result.successful()
    assert document.archive_serial_number == 42


def test_asn_handoff_preserves_edit_during_pdf_transformation(
    paperless_dirs: PaperlessDirs,
) -> None:
    """Replacement preparation may release only the originally captured ASN."""
    document = DocumentFactory(archive_serial_number=99)

    released = bulk_edit.release_archive_serial_numbers(
        [document.pk],
        expected_asns={document.pk: 12},
    )

    document.refresh_from_db()
    assert released == {}
    assert document.archive_serial_number == 99


def test_split_captures_deletion_revision_before_transforming_pdf(
    sample_doc: Document,
    mocker: MockerFixture,
) -> None:
    """An edit during PDF preparation must not become the deletion's accepted revision."""
    original_content = sample_doc.content

    def transform(*args, **kwargs):
        """Represent a user edit while the PDF tool is preparing replacement files."""
        Document.objects.filter(pk=sample_doc.pk).update(content="edit during split")
        return [sample_doc.source_path]

    mocker.patch("documents.pdf_ops.build_pdfs", side_effect=transform)
    mocker.patch("documents.tasks.consume_file.s")
    chord = mocker.patch("documents.bulk_edit.chord")

    bulk_edit.split([sample_doc.pk], [[1]], delete_originals=True)

    callback = chord.call_args.kwargs["body"]
    assert (
        callback.kwargs["expected_revisions"][str(sample_doc.pk)]["fields"]["content"]
        == original_content
    )
    assert Document.objects.get(pk=sample_doc.pk).content == "edit during split"
