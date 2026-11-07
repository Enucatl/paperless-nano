"""Independent workers exchange JSON while core owns publication and permissions."""

import builtins

import pytest
from django.contrib.auth.models import Permission
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from documents import enrichment_jobs
from documents import jobs
from documents.external_workers import METADATA_CONTRACT
from documents.external_workers import PARSER_CONTRACT
from documents.job_state import RejectedResult
from documents.models import DocumentJob
from paperless_testing.factories import UserFactory

pytestmark = pytest.mark.django_db(transaction=True)


@pytest.fixture
def external(settings, mocker, paperless_dirs):
    """Configure generic core contracts and forbid loading a worker implementation."""
    settings.PARSER = "documents.external_workers.ExternalParser"
    settings.ENRICHMENT_PROVIDER = "documents.external_workers.ExternalMetadata"
    settings.ENRICHMENT_ALLOW_REMOTE = True
    settings.ARCHIVE_FILE_GENERATION = "never"
    user = UserFactory()
    user.user_permissions.add(
        *Permission.objects.filter(codename__in=["process_parser", "process_metadata"])
    )
    # Even a fully capable server must leave these contracts for external workers.
    server = UserFactory(is_superuser=True)
    settings.PROCESSOR_USER = server.username
    settings.PROCESSOR_CAPABILITIES = ["parser", "metadata", "ingestion", "search"]
    mocker.patch("documents.jobs._dispatch")
    mocker.patch("documents.search.get_backend")
    mocker.patch("documents.workflows.runner.run_workflows")
    mocker.patch("documents.signals.document_updated.send")
    real_import = builtins.__import__

    def independent(name, *args, **kwargs):
        if name == "paperless_inference" or name.startswith("paperless_inference."):
            raise AssertionError("Core attempted to import the worker package")
        return real_import(name, *args, **kwargs)

    mocker.patch("builtins.__import__", side_effect=independent)
    client = APIClient()
    client.force_authenticate(user)
    return client, user


def parser_claim(sample_doc, client):
    """Admit OCR and lease it solely through the public operation API."""
    job = jobs.stage_reprocess(sample_doc.pk, remote_ocr=True)
    jobs.run_reprocess(job.pk)
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.WAITING
    assert job.result["invocations"]["parser.process"].get("attempts", 0) == 0
    listing = client.get("/api/operations/?status=pending").json()
    assert any(item["contract"] == PARSER_CONTRACT for item in listing)
    base = f"/api/operations/{job.pk}/parser.process/"
    response = client.post(base + "claim/")
    assert response.status_code == 200
    claim = response.json()
    assert claim["input"]["request"]["settings"] == {}
    return job, base, claim


def parser_result(claim):
    """Represent an arbitrary non-Python worker's JSON response."""
    return {
        "version": 1,
        "revision": claim["input"]["request"]["revision"],
        "provider": PARSER_CONTRACT,
        "execution": PARSER_CONTRACT,
        "worker": {"implementation": "independent-ocr/7", "model": "own-model"},
        "text": "Independently extracted content",
        "page_count": 1,
        "date": None,
        "archive": None,
        "thumbnail": None,
        "archive_type": None,
    }


@pytest.mark.parametrize("local_output", ["pdf", "pdfa-1", "pdfa-3"])
def test_external_archive_flavor_belongs_to_worker(
    sample_doc, external, settings, local_output
):
    """Local parser profile choices do not constrain external PDF/A workers."""
    settings.OCR_OUTPUT_TYPE = local_output
    client, _ = external
    _, _, claim = parser_claim(sample_doc, client)
    assert claim["input"]["request"]["output_type"] == "pdfa"


def test_plain_worker_files_results_and_publication(
    sample_doc, external, thumbnail_webp_file
):
    """Core accepts a worker without its package and preserves source bytes."""
    client, _ = external
    original = sample_doc.source_path.read_bytes()
    job, base, claim = parser_claim(sample_doc, client)
    response = client.get(
        base + "files/",
        {"path": claim["input"]["request"]["source"]["path"]},
        HTTP_X_OPERATION_TOKEN=claim["token"],
    )
    assert b"".join(response.streaming_content) == original
    response = client.post(
        base + "files/",
        {
            "file": SimpleUploadedFile(
                "thumbnail.webp", thumbnail_webp_file.read_bytes()
            )
        },
        HTTP_X_OPERATION_TOKEN=claim["token"],
    )
    assert response.status_code == 201
    result = parser_result(claim)
    result["thumbnail"] = response.json()
    body = {"token": claim["token"], "result": result}
    assert client.post(base + "result/", body, format="json").status_code == 202
    assert jobs.run_reprocess(job.pk)
    sample_doc.refresh_from_db()
    job.refresh_from_db()
    assert sample_doc.content == result["text"]
    assert sample_doc.source_path.read_bytes() == original
    assert job.status == DocumentJob.Status.SUCCEEDED
    assert job.result["parser_result"]["worker"] == result["worker"]


def test_external_parser_local_thumbnail_survives_cached_retry(
    sample_doc, external, thumbnail_webp_file, mocker
):
    """Core thumbnail provenance stays outside the retained worker proposal."""
    from documents.external_workers import ExternalParser
    from documents.job_storage import job_directory
    from documents.parser_operations import run_parser

    class WorkerStopped(BaseException):
        """Interrupt publication after the parser results are retained."""

    thumbnail = mocker.patch(
        "paperless.parsers.tesseract.RasterisedDocumentParser.get_thumbnail",
        return_value=thumbnail_webp_file,
    )
    client, _ = external
    original = sample_doc.source_path.read_bytes()
    job, base, claim = parser_claim(sample_doc, client)
    result = parser_result(claim)
    assert (
        client.post(
            base + "result/", {"token": claim["token"], "result": result}, format="json"
        ).status_code
        == 202
    )
    stopped = mocker.patch("documents.jobs._apply", side_effect=WorkerStopped)
    with pytest.raises(WorkerStopped):
        jobs.run_reprocess(job.pk)
    mocker.stop(stopped)
    job.refresh_from_db()
    retained = job.result["parser_result"]
    assert retained["thumbnail"] is not None
    assert retained["thumbnail_provider"]
    assert job.result["invocations"]["parser.process"]["result"] == result

    retried = run_parser(
        ExternalParser(),
        sample_doc.source_path,
        sample_doc.mime_type,
        job.input["filename"],
        job.configuration,
        job_directory(job.pk),
        job_id=str(job.pk),
        operation="reprocess",
        allow_remote=True,
    )
    assert retried == retained
    thumbnail.assert_called_once()
    assert jobs.run_reprocess(job.pk)
    sample_doc.refresh_from_db()
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.SUCCEEDED
    assert sample_doc.content == result["text"]
    assert sample_doc.source_path.read_bytes() == original
    assert job.result["invocations"]["parser.process"]["result"] == result


@pytest.mark.parametrize(
    "implementation", ["other-language-worker/1", "alternative-engine/2"]
)
def test_metadata_workers_are_interchangeable(sample_doc, external, implementation):
    """Unrelated implementations fulfill the same retained metadata contract."""
    client, _ = external
    job = enrichment_jobs.submit_enrichment(sample_doc.pk)
    enrichment_jobs.run_enrichment(str(job.pk))
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.WAITING
    assert job.result["invocations"]["metadata"].get("attempts", 0) == 0
    base = f"/api/operations/{job.pk}/metadata/"
    claim = client.post(base + "claim/").json()
    request = claim["input"]
    assert request["identity"] == METADATA_CONTRACT
    assert request["configuration"]["configuration"] == {}
    assert not {"model", "prompt", "endpoint", "credentials"} & request.keys()
    result = {
        "version": 1,
        "revision": request["revision"],
        "identity": METADATA_CONTRACT,
        "worker": {"implementation": implementation},
        "title": "Independent metadata",
    }
    assert (
        client.post(
            base + "result/", {"token": claim["token"], "result": result}, format="json"
        ).status_code
        == 202
    )
    enrichment_jobs.run_enrichment(str(job.pk))
    sample_doc.refresh_from_db()
    job.refresh_from_db()
    assert sample_doc.title == "Independent metadata"
    assert job.status == DocumentJob.Status.SUCCEEDED
    assert job.result["invocations"]["metadata"]["result"]["worker"] == result["worker"]


@pytest.mark.parametrize(
    "change",
    [
        "revision",
        "worker",
        "archive",
        "unknown",
        "thumbnail_provider",
        "permissions",
        "archive_type",
    ],
)
def test_external_parser_rejects_unsafe_result(sample_doc, external, settings, change):
    """The shared result channel never weakens domain or authorization checks."""
    client, user = external
    if change == "archive":
        settings.ARCHIVE_FILE_GENERATION = "always"
    elif change == "archive_type":
        settings.ARCHIVE_FILE_GENERATION = "auto"
    job, base, claim = parser_claim(sample_doc, client)
    result = parser_result(claim)
    if change == "revision":
        result["revision"] = "stale"
    elif change == "worker":
        result["worker"] = {"implementation": ""}
    elif change == "unknown":
        result["unexpected"] = True
    elif change == "thumbnail_provider":
        result["thumbnail_provider"] = "worker-supplied"
    elif change == "archive_type":
        upload = client.post(
            base + "files/",
            {
                "file": SimpleUploadedFile(
                    "archive.pdf", sample_doc.source_path.read_bytes()
                )
            },
            HTTP_X_OPERATION_TOKEN=claim["token"],
        )
        assert upload.status_code == 201
        result["archive"] = upload.json()
        result["archive_type"] = "searchable_pdf"
    body = {"token": claim["token"], "result": result}
    if change == "permissions":
        user.user_permissions.clear()
        assert client.post(base + "result/", body, format="json").status_code == 403
    else:
        assert client.post(base + "result/", body, format="json").status_code == 202
        with pytest.raises(RejectedResult):
            jobs.run_reprocess(job.pk)
    sample_doc.refresh_from_db()
    assert sample_doc.content == "test content"
