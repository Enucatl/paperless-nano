"""Shared remote lifecycle survives offline hosts and rejects stale application."""

import shutil
from datetime import timedelta

import pytest
from django.utils import timezone
from django.core.exceptions import PermissionDenied

from documents import deferred_operations as operations
from documents import jobs
from documents.enrichment_jobs import run_enrichment
from documents.ingestion_state import invoke
from documents.models import Document
from documents.models import DocumentJob
from paperless.parsers.contract import file_reference
from paperless_testing.factories import TagFactory
from paperless_testing.factories import UserFactory

pytestmark = pytest.mark.django_db(transaction=True)


class RemoteParser:
    """A pure parser whose execution belongs to the explicitly granted host."""

    identity = "test.remote:1"
    uses_remote_service = False
    replay_safe = True

    def configuration_snapshot(self):
        """Expose immutable configuration without requiring an online host."""
        return {}

    def process(self, request, root):
        """Fail if a deferred operation accidentally runs in the worker."""
        raise AssertionError("Remote parser must not run locally")


class RemoteMetadata:
    """A pure classification implementation independent of parser execution."""

    identity = ("test.metadata:1", "model:1")
    configuration = {}

    def classify(self, request):
        """Fail if waiting for the workstation holds a local worker."""
        raise AssertionError("Remote classifier must not run locally")


@pytest.fixture
def remote(settings, mocker):
    """Grant a processor both finite operations and disable real broker sends."""
    user = UserFactory(is_superuser=True, is_staff=True)
    settings.PARSER = "documents.tests.test_deferred_operations.RemoteParser"
    settings.CLASSIFICATION_PROVIDER = (
        "documents.tests.test_deferred_operations.RemoteMetadata"
    )
    settings.CLASSIFIER_ENABLED = True
    mocker.patch("documents.jobs._dispatch")
    mocker.patch("documents.search.get_backend")
    return user


def complete_parser(job, user, thumbnail):
    """Submit valid privately staged parser output using the remote contract."""
    claim = operations.claim(job.pk, "parser.process", user)
    request = claim["input"]["request"]
    directory = jobs.job_directory(job.pk)
    output = directory / request["output_dir"] / "thumbnail.webp"
    output.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(thumbnail, output)
    result = {
        "version": 1,
        "revision": request["revision"],
        "provider": RemoteParser.identity,
        "text": "OCR content from weekly workstation",
        "page_count": 1,
        "date": None,
        "archive": None,
        "archive_type": None,
        "thumbnail": file_reference(directory, output),
    }
    operations.submit_result(job.pk, "parser.process", user, claim["token"], result)
    return claim, result


def test_week_offline_ocr_metadata_chain(
    sample_doc, remote, thumbnail_webp_file, mocker
):
    """OCR commits once and records metadata atomically before another offline week."""
    tag = TagFactory(matching_algorithm=6)
    job = jobs.stage_reprocess(
        sample_doc.pk, actor_id=remote.pk, remote_ocr=True, metadata=True
    )
    assert jobs.run_reprocess(job.pk) is False
    job.refresh_from_db()
    assert job.status == "waiting"
    assert job.attempts == 0
    DocumentJob.objects.filter(pk=job.pk).update(
        updated=timezone.now() - timedelta(days=8)
    )
    dispatch = mocker.patch("documents.jobs._dispatch")
    jobs.recover_document_jobs()
    dispatch.assert_not_called()
    assert (jobs.job_directory(job.pk) / job.input["staged_name"]).is_file()
    claim, result = complete_parser(job, remote, thumbnail_webp_file)
    assert (
        operations.submit_result(
            job.pk, "parser.process", remote, claim["token"], result
        )["status"]
        == "staged"
    )
    assert jobs.run_reprocess(job.pk) is True
    job.refresh_from_db()
    child = DocumentJob.objects.get(pk=job.result["metadata_job"])
    sample_doc.refresh_from_db()
    assert sample_doc.content == result["text"]
    run_enrichment(str(child.pk))
    child.refresh_from_db()
    assert child.status == "waiting"
    assert Document.objects.filter(pk=sample_doc.pk).exists()
    DocumentJob.objects.filter(pk=child.pk).update(
        updated=timezone.now() - timedelta(days=8)
    )
    claim = operations.claim(child.pk, "metadata", remote)
    request = claim["input"]
    assignments = {field: [] for field in request["candidates"]}
    assignments["tags"] = [tag.pk]
    metadata = {
        "version": 1,
        "revision": request["revision"],
        "provider": RemoteMetadata.identity[0],
        "model": RemoteMetadata.identity[1],
        "assignments": assignments,
    }
    operations.submit_result(child.pk, "metadata", remote, claim["token"], metadata)
    run_enrichment(str(child.pk))
    run_enrichment(str(child.pk))
    child.refresh_from_db()
    assert child.status == "succeeded"
    assert list(sample_doc.tags.values_list("pk", flat=True)) == [tag.pk]
    assert DocumentJob.objects.filter(kind="enrich").count() == 1


def pending_job(remote, *, replay_safe=True):
    """Admit an isolated finite operation using the shared invocation journal."""
    job = DocumentJob.objects.create(timeout=60, input={"actor_id": remote.pk})
    with pytest.raises(operations.PendingOperation):
        invoke(
            job,
            "work",
            "provider:1",
            {"value": 1},
            lambda _: None,
            replay_safe=replay_safe,
            operation="metadata.classify",
            provider=RemoteMetadata(),
        )
    return job


def test_stale_claim_cancel_and_explicit_access(remote):
    """Revoked grants, expired leases and cancellation never publish late results."""
    job = pending_job(remote)
    stranger = UserFactory()
    from django.core.exceptions import PermissionDenied

    with pytest.raises(PermissionDenied):
        operations.claim(job.pk, "work", stranger)
    first = operations.claim(job.pk, "work", remote)
    job.refresh_from_db()
    job.result["invocations"]["work"]["lease_until"] = (
        timezone.now() - timedelta(seconds=1)
    ).isoformat()
    job.save()
    second = operations.claim(job.pk, "work", remote)
    assert second["token"] != first["token"]
    with pytest.raises(operations.OperationConflict):
        operations.submit_result(job.pk, "work", remote, first["token"], {})
    operations.cancel(job.pk, remote)
    with pytest.raises(operations.OperationConflict):
        operations.submit_result(job.pk, "work", remote, second["token"], {})


def test_expired_effectful_claim_requires_reconciliation(remote):
    """Worker loss cannot authorize blind replay of an external effect."""
    job = pending_job(remote, replay_safe=False)
    operations.claim(job.pk, "work", remote)
    job.refresh_from_db()
    job.result["invocations"]["work"]["lease_until"] = (
        timezone.now() - timedelta(seconds=1)
    ).isoformat()
    job.save()
    assert operations.claim(job.pk, "work", remote)["status"] == "unknown"
    job.refresh_from_db()
    assert job.status == "attention"


def test_concurrent_edit_rejects_remote_ocr(sample_doc, remote, thumbnail_webp_file):
    """A queued OCR result cannot overwrite a user's later document edit."""
    job = jobs.stage_reprocess(sample_doc.pk, actor_id=remote.pk, remote_ocr=True)
    jobs.run_reprocess(job.pk)
    complete_parser(job, remote, thumbnail_webp_file)
    Document.objects.filter(pk=sample_doc.pk).update(content="User edit")
    with pytest.raises(jobs.RejectedResult):
        jobs.run_reprocess(job.pk)
    sample_doc.refresh_from_db()
    assert sample_doc.content == "User edit"
    job.refresh_from_db()
    assert job.status == "rejected"


def test_authenticated_api_claim_files_and_result(
    sample_doc, remote, thumbnail_webp_file
):
    """An authenticated grant scopes file reads and immutable upload acceptance."""
    from django.core.files.uploadedfile import SimpleUploadedFile
    from rest_framework.test import APIClient

    client = APIClient()
    job = jobs.stage_reprocess(sample_doc.pk, actor_id=remote.pk, remote_ocr=True)
    jobs.run_reprocess(job.pk)
    base = f"/api/operations/{job.pk}/parser.process/"
    assert client.post(base + "claim/").status_code in {401, 403}
    stranger = UserFactory()
    client.force_authenticate(stranger)
    assert client.post(base + "claim/").status_code == 403
    client.force_authenticate(remote)
    response = client.post(base + "claim/")
    assert response.status_code == 200
    claim = response.json()
    token = claim["token"]
    assert (
        client.get(
            base + "files/", {"path": "../../secret"}, HTTP_X_OPERATION_TOKEN=token
        ).status_code
        == 403
    )
    source = claim["input"]["request"]["source"]
    response = client.get(
        base + "files/", {"path": source["path"]}, HTTP_X_OPERATION_TOKEN=token
    )
    assert response.status_code == 200
    assert b"".join(response.streaming_content) == sample_doc.source_path.read_bytes()
    content = thumbnail_webp_file.read_bytes()
    response = client.post(
        base + "files/",
        {"file": SimpleUploadedFile("thumbnail.webp", content)},
        HTTP_X_OPERATION_TOKEN=token,
    )
    assert response.status_code == 201
    reference = response.json()
    assert reference["path"].startswith(claim["input"]["request"]["output_dir"] + "/")
    response = client.post(
        base + "files/",
        {"file": SimpleUploadedFile("thumbnail.webp", content)},
        HTTP_X_OPERATION_TOKEN=token,
    )
    assert response.status_code == 200
    assert response.json() == reference
    assert (
        client.post(
            base + "files/",
            {"file": SimpleUploadedFile("thumbnail.webp", b"different")},
            HTTP_X_OPERATION_TOKEN=token,
        ).status_code
        == 409
    )
    response = client.post(
        base + "result/", {"token": token, "result": {"malformed": True}}, format="json"
    )
    assert response.status_code == 202
    with pytest.raises(jobs.RejectedResult):
        jobs.run_reprocess(job.pk)
    sample_doc.refresh_from_db()
    assert sample_doc.content == "test content"


def test_reconcile_unknown_effect_does_not_reexecute(remote):
    """A verified outcome resumes core validation without rerunning an effect."""
    job = pending_job(remote, replay_safe=False)
    operations.claim(job.pk, "work", remote)
    job.refresh_from_db()
    job.result["invocations"]["work"]["lease_until"] = (
        timezone.now() - timedelta(seconds=1)
    ).isoformat()
    job.save()
    operations.claim(job.pk, "work", remote)
    operations.reconcile(
        job.pk, "work", remote, {"receipt": "confirmed"}, "External receipt 123"
    )
    result = invoke(
        job,
        "work",
        "provider:1",
        {"value": 1},
        lambda _: pytest.fail("Must not replay"),
        operation="metadata.classify",
    )
    assert result == {"receipt": "confirmed"}


def test_metadata_preserves_edit_and_provider_identity(sample_doc, remote, settings):
    """Metadata conflicts retain a user's edits and never adopt a new provider."""
    from documents.enrichment_jobs import submit_enrichment

    job = submit_enrichment(sample_doc.pk, actor_id=remote.pk)
    run_enrichment(str(job.pk))
    claim = operations.claim(job.pk, "metadata", remote)
    request = claim["input"]
    result = {
        "version": 1,
        "revision": request["revision"],
        "provider": RemoteMetadata.identity[0],
        "model": RemoteMetadata.identity[1],
        "assignments": {field: [] for field in request["candidates"]},
    }
    Document.objects.filter(pk=sample_doc.pk).update(title="User changed title")
    operations.submit_result(job.pk, "metadata", remote, claim["token"], result)
    with pytest.raises(jobs.RejectedResult):
        run_enrichment(str(job.pk))
    sample_doc.refresh_from_db()
    assert sample_doc.title == "User changed title"
    job.refresh_from_db()
    assert job.status == "rejected"


def test_followon_retains_provider_snapshot(
    sample_doc, remote, thumbnail_webp_file, settings
):
    """Changing metadata configuration during OCR cannot reinterpret its child."""
    job = jobs.stage_reprocess(
        sample_doc.pk, actor_id=remote.pk, remote_ocr=True, metadata=True
    )
    jobs.run_reprocess(job.pk)
    settings.CLASSIFIER_ENABLED = False
    complete_parser(job, remote, thumbnail_webp_file)
    assert jobs.run_reprocess(job.pk) is True
    job.refresh_from_db()
    child = DocumentJob.objects.get(pk=job.result["metadata_job"])
    with pytest.raises(jobs.RejectedResult):
        run_enrichment(str(child.pk))
    sample_doc.refresh_from_db()
    assert sample_doc.content == "OCR content from weekly workstation"


def test_remote_policy_cannot_override_document_opt_out(sample_doc, remote, settings):
    """Provider selection does not grant permission to export a local-only input."""
    settings.REMOTE_OCR_MODE = "workflow_only"
    job = jobs.stage_reprocess(sample_doc.pk, actor_id=remote.pk, remote_ocr=False)
    assert jobs.run_reprocess(job.pk) is False
    job.refresh_from_db()
    assert job.status == "waiting"
    with pytest.raises(PermissionDenied):
        operations.claim(job.pk, "parser.process", remote)


def test_completed_parser_submission_remains_idempotent(
    sample_doc, remote, thumbnail_webp_file
):
    """Completed durable outcomes accept identical duplicate submissions."""
    job = jobs.stage_reprocess(sample_doc.pk, actor_id=remote.pk, remote_ocr=True)
    jobs.run_reprocess(job.pk)
    claim, result = complete_parser(job, remote, thumbnail_webp_file)
    assert jobs.run_reprocess(job.pk) is True
    assert (
        operations.submit_result(
            job.pk, "parser.process", remote, claim["token"], result
        )["status"]
        == "succeeded"
    )
