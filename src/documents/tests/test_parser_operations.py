"""Replacement parsers retain the core's publication and recovery guarantees."""

from __future__ import annotations

import json
import shutil
from typing import TYPE_CHECKING
from unittest.mock import Mock

import pytest
from django.utils import timezone

from documents import ingestion_jobs
from documents import jobs
from documents.data_models import ConsumableDocument
from documents.data_models import DocumentSource
from documents.exceptions import ConsumerError
from documents.models import Document
from documents.models import DocumentJob
from paperless.parsers.contract import ParserProvider
from paperless.parsers.contract import file_reference
from paperless.parsers.contract import resolve_reference

if TYPE_CHECKING:
    from pathlib import Path

    from pytest import MonkeyPatch
    from pytest_django.fixtures import Settings
    from pytest_mock import MockerFixture

    from paperless.parsers.contract import ParserRequest
    from paperless.parsers.contract import ParserResult
    from paperless_testing.dirs import PaperlessDirs

pytestmark = pytest.mark.django_db(transaction=True)


def _thumbnail(request: ParserRequest, root: Path, source: Path) -> dict:
    """Return a valid private thumbnail without touching a default parser."""
    thumbnail = root / request["output_dir"] / "thumbnail.webp"
    thumbnail.parent.mkdir(exist_ok=True)
    shutil.copyfile(source, thumbnail)
    return file_reference(root, thumbnail)


@pytest.fixture
def provider(
    mocker: MockerFixture,
    monkeypatch: MonkeyPatch,
    settings: Settings,
    thumbnail_webp_file: Path,
) -> type:
    """Register a text-only provider without any legacy parser or ORM methods."""

    class Provider(ParserProvider):
        """Supply explicit serializable results from a private input reference."""

        identity = "test.text-only:1"
        uses_remote_service = False

        def configuration_snapshot(self) -> dict:
            """Snapshot this implementation's configured language."""
            return {"language": settings.OCR_LANGUAGE}

        @staticmethod
        def output(request: ParserRequest, root: Path) -> ParserResult:
            """Read a durable reference and return JSON-compatible content."""
            json.dumps(request)
            assert resolve_reference(root, request["source"]).is_file()
            return {
                "version": 1,
                "revision": request["revision"],
                "provider": Provider.identity,
                "text": "replacement provider content",
                "page_count": 1,
                "date": None,
                "archive": None,
                "thumbnail": None,
                "archive_type": None,
            }

        process = Mock(side_effect=output)

    settings.ARCHIVE_FILE_GENERATION = "never"
    settings.PARSER = "paperless_testing.explicit_parser.Provider"
    import sys
    import types

    module = types.ModuleType("paperless_testing.explicit_parser")
    module.Provider = Provider
    monkeypatch.setitem(sys.modules, module.__name__, module)
    mocker.patch(
        "paperless.parsers.tesseract.RasterisedDocumentParser.get_thumbnail",
        return_value=thumbnail_webp_file,
    )
    mocker.patch("documents.search.get_backend")
    mocker.patch("documents.consumer.load_classifier", return_value=None)
    return Provider


def test_text_only_reprocessing_keeps_original_and_existing_archive(
    sample_doc: Document,
    provider: type,
) -> None:
    """An explicit never policy permits text-only results without losing files."""
    original = sample_doc.source_path.read_bytes()
    archive = sample_doc.archive_path.read_bytes()
    job = jobs.stage_reprocess(sample_doc.pk)

    assert jobs.run_reprocess(job.pk)

    sample_doc.refresh_from_db()
    job.refresh_from_db()
    assert sample_doc.content == "replacement provider content"
    assert sample_doc.source_path.read_bytes() == original
    assert sample_doc.archive_path.read_bytes() == archive
    assert job.status == DocumentJob.Status.SUCCEEDED
    assert job.cleaned
    assert not jobs.job_directory(job.pk).exists()
    assert provider.process.call_count == 1
    assert "test.text-only:1" in json.dumps(job.provider)
    json.dumps(job.result)
    assert "remote_ocr_api_key" not in json.dumps(job.configuration)


@pytest.mark.parametrize("policy", ["always", "auto"])
def test_archive_preference_is_advisory(
    sample_doc: Document,
    provider: type,
    settings: Settings,
    policy: str,
    multi_page_images_pdf_file: Path,
) -> None:
    """A replacement can omit an archive under either archive preference."""
    from documents.utils import compute_checksum

    settings.ARCHIVE_FILE_GENERATION = policy
    if policy == "auto":
        shutil.copyfile(multi_page_images_pdf_file, sample_doc.source_path)
        sample_doc.checksum = compute_checksum(sample_doc.source_path)
        sample_doc.save(update_fields=["checksum"])
    job = jobs.stage_reprocess(sample_doc.pk)

    assert jobs.run_reprocess(job.pk)
    job.refresh_from_db()
    sample_doc.refresh_from_db()
    assert job.status == DocumentJob.Status.SUCCEEDED
    assert sample_doc.content == "replacement provider content"
    provider.process.assert_called_once()


def test_auto_policy_accepts_text_only_for_born_digital_pdf(
    sample_doc: Document,
    provider: type,
    settings: Settings,
) -> None:
    """The automatic policy needs no redundant archive for a readable PDF."""
    settings.ARCHIVE_FILE_GENERATION = "auto"
    job = jobs.stage_reprocess(sample_doc.pk)

    jobs.run_reprocess(job.pk)

    sample_doc.refresh_from_db()
    assert sample_doc.content == "replacement provider content"


@pytest.mark.parametrize(
    "mime_type",
    [
        "message/rfc822",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ],
)
def test_text_only_provider_can_omit_display_rendition(
    sample_doc: Document,
    provider: type,
    mime_type: str,
) -> None:
    """A replacement owns whether unsupported original formats have previews."""
    sample_doc.mime_type = mime_type
    sample_doc.save(update_fields=["mime_type"])
    job = jobs.stage_reprocess(sample_doc.pk)

    provider.process.side_effect = lambda request, root: {
        **provider.output(request, root),
        "thumbnail": _thumbnail(request, root, sample_doc.thumbnail_path),
    }
    assert jobs.run_reprocess(job.pk)
    provider.process.assert_called_once()
    sample_doc.refresh_from_db()
    assert sample_doc.content == "replacement provider content"


def test_remote_provider_cannot_bypass_processing_permission(
    sample_doc: Document,
    provider: type,
    settings: Settings,
) -> None:
    """Execution rechecks remote permission even if registry selection misbehaves."""
    provider.uses_remote_service = True
    settings.REMOTE_OCR_MODE = "workflow_only"
    job = jobs.stage_reprocess(sample_doc.pk, remote_ocr=False)

    with pytest.raises(jobs.RejectedResult):
        jobs.run_reprocess(job.pk)

    provider.process.assert_not_called()
    sample_doc.refresh_from_db()
    assert sample_doc.content == "test content"


def test_remote_provider_credentials_stay_out_of_durable_snapshots(
    sample_doc: Document,
    provider: type,
    settings: Settings,
) -> None:
    """Permitted remote execution receives its key without storing it in the job."""
    settings.REMOTE_OCR_API_KEY = "private-provider-test-key"
    settings.REMOTE_OCR_MODE = "workflow_only"
    provider.uses_remote_service = True
    job = jobs.stage_reprocess(sample_doc.pk, remote_ocr=True)

    jobs.run_reprocess(job.pk)

    request = provider.process.call_args.args[0]
    assert request["allow_remote"] is True
    assert "configuration" not in request
    assert settings.REMOTE_OCR_API_KEY not in json.dumps(request)
    job.refresh_from_db()
    for snapshot in (job.configuration, job.result["parser_request"]):
        serialized = json.dumps(snapshot)
        assert "remote_ocr_api_key" not in serialized
        assert settings.REMOTE_OCR_API_KEY not in serialized
    assert job.status == DocumentJob.Status.SUCCEEDED


def test_thumbnail_fallback_cannot_launder_mutated_input(
    sample_doc: Document,
    provider: type,
    thumbnail_webp_file: Path,
    mocker: MockerFixture,
) -> None:
    """Fallback validation uses its own snapshot and retains the admitted input."""

    class ThumbnailProvider(provider):
        """Attempt to legitimize a rewritten source through a nested mutation."""

        identity = "test.thumbnail:1"
        version = "1"

        def get_thumbnail(self, source: Path, mime_type: str) -> Path:
            """Modify an admitted source while returning a valid thumbnail."""
            source.write_bytes(b"fallback replaced input")
            return thumbnail_webp_file

        def configure(self, context: object) -> None:
            """Accept legacy auxiliary configuration."""

    mocker.patch(
        "documents.parser_operations.get_parser_registry",
    ).return_value.get_parser_for_file.return_value = ThumbnailProvider
    apply = mocker.spy(jobs, "_apply")
    original = sample_doc.source_path.read_bytes()
    job = jobs.stage_reprocess(sample_doc.pk)

    with pytest.raises(jobs.RejectedResult):
        jobs.run_reprocess(job.pk)

    apply.assert_not_called()
    job.refresh_from_db()
    sample_doc.refresh_from_db()
    assert job.status == DocumentJob.Status.REJECTED
    assert sample_doc.content == "test content"
    assert sample_doc.source_path.read_bytes() == original
    assert (
        jobs.job_directory(job.pk) / job.input["staged_name"]
    ).read_bytes() == original


def test_timeout_can_retry_without_publishing_partial_output(
    sample_doc: Document,
    provider: type,
) -> None:
    """A failed provider attempt retains the input and the next attempt can succeed."""
    provider.replay_safe = True
    job = jobs.stage_reprocess(sample_doc.pk)
    provider.process.side_effect = TimeoutError("provider deadline")

    with pytest.raises(TimeoutError, match="provider deadline"):
        jobs.run_reprocess(job.pk)

    job.refresh_from_db()
    sample_doc.refresh_from_db()
    assert job.status == DocumentJob.Status.QUEUED
    assert sample_doc.content == "test content"
    assert (jobs.job_directory(job.pk) / job.input["staged_name"]).is_file()
    provider.process.side_effect = provider.output

    jobs.run_reprocess(job.pk)

    job.refresh_from_db()
    assert job.attempts == 2
    assert job.cleaned


@pytest.mark.parametrize("change", ["text", "revision", "provider"])
def test_invalid_provider_output_retains_input_and_permanent_files(
    sample_doc: Document,
    provider: type,
    change: str,
) -> None:
    """Malformed, stale and missing declared output never reaches publication."""
    original = sample_doc.source_path.read_bytes()
    archive = sample_doc.archive_path.read_bytes()
    thumbnail = sample_doc.thumbnail_path.read_bytes()

    def invalid(request: ParserRequest, root: Path) -> ParserResult:
        """Corrupt one provider claim after otherwise valid processing."""
        result = provider.output(request, root)
        if change != "thumbnail":
            result[change] = None if change == "text" else "different"
        return result

    provider.process.side_effect = invalid
    job = jobs.stage_reprocess(sample_doc.pk)

    with pytest.raises(jobs.RejectedResult):
        jobs.run_reprocess(job.pk)

    sample_doc.refresh_from_db()
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.REJECTED
    assert sample_doc.content == "test content"
    assert sample_doc.source_path.read_bytes() == original
    assert sample_doc.archive_path.read_bytes() == archive
    assert sample_doc.thumbnail_path.read_bytes() == thumbnail
    assert (
        jobs.job_directory(job.pk) / job.input["staged_name"]
    ).read_bytes() == original


@pytest.mark.parametrize("change", ["document", "configuration", "identity"])
def test_change_during_provider_execution_rejects_result(
    sample_doc: Document,
    provider: type,
    settings: Settings,
    change: str,
) -> None:
    """Execution cannot apply content against a different document or configuration."""

    def changed(request: ParserRequest, root: Path) -> ParserResult:
        """Alter core state while the replacement provider is processing."""
        result = provider.output(request, root)
        if change == "document":
            Document.objects.filter(pk=sample_doc.pk).update(
                content="user edit",
                modified=timezone.now(),
            )
        elif change == "configuration":
            settings.OCR_LANGUAGE = "deu"
        else:
            provider.identity = "test.text-only:2"
        return result

    provider.process.side_effect = changed
    job = jobs.stage_reprocess(sample_doc.pk)

    with pytest.raises(jobs.RejectedResult):
        jobs.run_reprocess(job.pk)

    sample_doc.refresh_from_db()
    assert sample_doc.content == (
        "user edit" if change == "document" else "test content"
    )


def test_output_cannot_alias_permanent_document_files(
    sample_doc: Document,
    provider: type,
) -> None:
    """A provider-supplied symlink cannot turn cleanup into permanent-file deletion."""
    original = sample_doc.source_path.read_bytes()

    def outside(request: ParserRequest, root: Path) -> ParserResult:
        """Return an output reference outside the private provider directory."""
        result = provider.output(request, root)
        result["thumbnail"] = {
            "path": str(sample_doc.thumbnail_path),
            "sha256": "0" * 64,
            "size": sample_doc.thumbnail_path.stat().st_size,
        }
        return result

    provider.process.side_effect = outside
    job = jobs.stage_reprocess(sample_doc.pk)

    with pytest.raises(jobs.RejectedResult):
        jobs.run_reprocess(job.pk)

    assert sample_doc.source_path.read_bytes() == original
    assert sample_doc.thumbnail_path.is_file()


def test_provider_cannot_rewrite_its_source_reference(
    sample_doc: Document,
    provider: type,
) -> None:
    """Mutating the request must not authorize changed input bytes."""
    original = sample_doc.source_path.read_bytes()

    def changed(request: ParserRequest, root: Path) -> ParserResult:
        """Attempt to launder a changed source through a replacement checksum."""
        result = provider.output(request, root)
        source = resolve_reference(root, request["source"])
        source.write_bytes(b"provider replaced input")
        request["source"] = file_reference(root, source)
        return result

    provider.process.side_effect = changed
    job = jobs.stage_reprocess(sample_doc.pk)

    with pytest.raises(jobs.RejectedResult):
        jobs.run_reprocess(job.pk)

    assert sample_doc.source_path.read_bytes() == original
    assert (
        jobs.job_directory(job.pk) / job.input["staged_name"]
    ).read_bytes() == original


@pytest.mark.parametrize("change", ["configuration", "provider", "source"])
def test_prepared_result_rechecks_configuration_and_provider_before_publication(
    sample_doc: Document,
    provider: type,
    settings: Settings,
    mocker: MockerFixture,
    change: str,
) -> None:
    """Recovered output cannot apply after deployment or provider identity changes."""

    class WorkerStopped(BaseException):
        """Stop after preparation without applying ordinary failure handling."""

    job = jobs.stage_reprocess(sample_doc.pk)
    stopped = mocker.patch("documents.jobs._apply", side_effect=WorkerStopped)
    with pytest.raises(WorkerStopped):
        jobs.run_reprocess(job.pk)
    mocker.stop(stopped)
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.PREPARED
    if change == "configuration":
        settings.OCR_LANGUAGE = "deu"
    elif change == "source":
        resolve_reference(
            jobs.job_directory(job.pk), job.result["parser_request"]["source"]
        ).write_bytes(b"changed prepared input")
    else:
        provider.identity = "test.text-only:2"

    with pytest.raises(jobs.RejectedResult):
        jobs.run_reprocess(job.pk)

    provider.process.assert_called_once()
    sample_doc.refresh_from_db()
    assert sample_doc.content == "test content"
    assert (jobs.job_directory(job.pk) / job.input["staged_name"]).is_file()


@pytest.mark.parametrize("output", ["invalid_bytes", "missing_file"])
def test_output_references_require_real_decodable_files(
    sample_doc: Document,
    provider: type,
    output: str,
) -> None:
    """Correct reference metadata alone cannot authorize invalid rendition bytes."""

    def invalid(request: ParserRequest, root: Path) -> ParserResult:
        """Return a referenced rendition whose content cannot be published."""
        result = provider.output(request, root)
        path = root / request["output_dir"] / "thumbnail.webp"
        path.parent.mkdir()
        path.write_bytes(b"this is not a WebP image")
        result["thumbnail"] = file_reference(root, path)
        if output == "missing_file":
            path.unlink()
        return result

    provider.process.side_effect = invalid
    original_thumbnail = sample_doc.thumbnail_path.read_bytes()
    job = jobs.stage_reprocess(sample_doc.pk)

    with pytest.raises(jobs.RejectedResult):
        jobs.run_reprocess(job.pk)

    sample_doc.refresh_from_db()
    assert sample_doc.content == "test content"
    assert sample_doc.thumbnail_path.read_bytes() == original_thumbnail
    assert (jobs.job_directory(job.pk) / job.input["staged_name"]).is_file()


@pytest.mark.parametrize("explicit", [False, True])
def test_installed_text_provider_uses_real_registry_and_local_thumbnail_fallback(
    paperless_dirs: PaperlessDirs,
    settings: Settings,
    mocker: MockerFixture,
    fake_progress_manager: type,
    *,
    explicit: bool,
) -> None:
    """A real modern entrypoint replaces extraction through unchanged ingestion."""
    from paperless.parsers.providers import PlainTextProvider
    from paperless.parsers.registry import reset_parser_registry

    settings.ARCHIVE_FILE_GENERATION = "never"
    if explicit:
        settings.PARSER = "paperless.parsers.providers.PlainTextProvider"
    else:
        entrypoint = Mock()
        entrypoint.name = "plain-text"
        entrypoint.load.return_value = PlainTextProvider
        mocker.patch(
            "paperless.parsers.registry.entry_points", return_value=[entrypoint]
        )
    mocker.patch("documents.consumer.load_classifier", return_value=None)
    mocker.patch("documents.search.get_backend")
    source = paperless_dirs.scratch_dir / "plain.txt"
    text = "An explicitly installed plain text provider."
    source.write_text(text)
    job = ingestion_jobs.stage_consumption(
        ConsumableDocument(DocumentSource.ApiUpload, original_file=source),
    )
    reset_parser_registry()
    try:
        result = ingestion_jobs.run_consumption(job.pk)
    finally:
        reset_parser_registry()

    document = Document.objects.get(pk=result["document_id"])
    job.refresh_from_db()
    assert document.content == text
    assert document.source_path.read_text() == text
    assert document.thumbnail_path.is_file()
    assert document.archive_filename is None
    assert (job.provider if explicit else job.provider["implementation"])[
        "identity"
    ] == PlainTextProvider.identity
    assert "TextDocumentParser" in job.result["parser_result"]["thumbnail_provider"]
    assert job.cleaned


@pytest.mark.parametrize("fail", [False, True])
def test_ingestion_uses_same_provider_without_replaying_failed_work(
    paperless_dirs: PaperlessDirs,
    simple_digital_pdf_file: Path,
    provider: type,
    fake_progress_manager: type,
    *,
    fail: bool,
) -> None:
    """Both operation callers use the boundary and failed ingestion stays retained."""
    source = paperless_dirs.scratch_dir / "provider.pdf"
    shutil.copyfile(simple_digital_pdf_file, source)
    job = ingestion_jobs.stage_consumption(
        ConsumableDocument(DocumentSource.ApiUpload, original_file=source),
    )
    if fail:
        provider.process.side_effect = TimeoutError("provider deadline")
        with pytest.raises(ConsumerError, match="provider deadline"):
            ingestion_jobs.run_consumption(job.pk)
        with pytest.raises(ingestion_jobs.NeedsAttention):
            ingestion_jobs.run_consumption(job.pk)
        assert not Document.objects.exists()
        assert (jobs.job_directory(job.pk) / "input").is_file()
    else:
        result = ingestion_jobs.run_consumption(job.pk)
        document = Document.objects.get(pk=result["document_id"])
        assert document.content == "replacement provider content"
        assert document.page_count == 1
        assert document.archive_filename is None
        assert document.source_path.read_bytes() == simple_digital_pdf_file.read_bytes()
        job.refresh_from_db()
        assert job.cleaned
    provider.process.assert_called_once()


@pytest.mark.parametrize("change", ["configuration", "provider"])
def test_retry_keeps_the_selected_configuration_and_provider(
    sample_doc: Document,
    provider: type,
    settings: Settings,
    change: str,
) -> None:
    """A retry cannot silently replace the implementation of an admitted attempt."""
    provider.replay_safe = True
    job = jobs.stage_reprocess(sample_doc.pk)
    provider.process.side_effect = TimeoutError("provider deadline")
    with pytest.raises(TimeoutError):
        jobs.run_reprocess(job.pk)
    if change == "configuration":
        settings.OCR_LANGUAGE = "deu"
    else:
        provider.identity = "test.text-only:2"
    provider.process.side_effect = provider.output
    with pytest.raises(jobs.RejectedResult, match="changed before retry"):
        jobs.run_reprocess(job.pk)
    provider.process.assert_called_once()
    sample_doc.refresh_from_db()
    assert sample_doc.content == "test content"
    assert (jobs.job_directory(job.pk) / job.input["staged_name"]).is_file()


@pytest.mark.parametrize("policy", ["auto", "always", "never"])
@pytest.mark.parametrize("archive_type", ["searchable_pdf", "pdfa"])
def test_replacement_archive_is_validated_without_policy_negotiation(
    sample_doc: Document,
    provider: type,
    settings: Settings,
    policy: str,
    archive_type: str,
) -> None:
    """Supported archives can differ from preferences, including never."""
    settings.ARCHIVE_FILE_GENERATION = policy
    settings.OCR_OUTPUT_TYPE = "pdfa"

    def output(request: ParserRequest, root: Path) -> ParserResult:
        result = provider.output(request, root)
        target = root / request["output_dir"] / "archive.pdf"
        target.parent.mkdir()
        shutil.copyfile(sample_doc.source_path, target)
        return {
            **result,
            "archive": file_reference(root, target),
            "archive_type": archive_type,
        }

    provider.process.side_effect = output
    job = jobs.stage_reprocess(sample_doc.pk)
    assert jobs.run_reprocess(job.pk)
    sample_doc.refresh_from_db()
    assert sample_doc.archive_path.read_bytes() == sample_doc.source_path.read_bytes()


def test_returned_thumbnail_avoids_registry_in_ingestion_and_reprocessing(
    paperless_dirs: PaperlessDirs,
    simple_digital_pdf_file: Path,
    thumbnail_webp_file: Path,
    provider: type,
    mocker: MockerFixture,
    fake_progress_manager: type,
) -> None:
    """An explicit class without registry metadata owns the complete normal path."""
    mocker.patch(
        "paperless.parsers.registry.get_parser_registry",
        side_effect=AssertionError("default registry loaded"),
    )
    mocker.patch(
        "documents.parsers.get_parser_registry",
        side_effect=AssertionError("default registry loaded"),
    )
    mocker.patch(
        "documents.parser_operations.get_parser_registry",
        side_effect=AssertionError("default registry loaded"),
    )
    provider.process.side_effect = lambda request, root: {
        **provider.output(request, root),
        "thumbnail": _thumbnail(request, root, thumbnail_webp_file),
    }
    source = paperless_dirs.scratch_dir / "explicit.pdf"
    shutil.copyfile(simple_digital_pdf_file, source)
    job = ingestion_jobs.stage_consumption(
        ConsumableDocument(DocumentSource.ApiUpload, original_file=source)
    )
    result = ingestion_jobs.run_consumption(job.pk)
    reprocess = jobs.stage_reprocess(result["document_id"])
    assert jobs.run_reprocess(reprocess.pk)
    assert provider.process.call_count == 2


def test_missing_local_thumbnail_fails_without_publishing(
    sample_doc: Document,
    provider: type,
    mocker: MockerFixture,
) -> None:
    """Optional provider thumbnails do not weaken final publication requirements."""
    mocker.patch(
        "documents.parser_operations.get_parser_registry"
    ).return_value.get_parser_for_file.return_value = None
    job = jobs.stage_reprocess(sample_doc.pk)
    with pytest.raises(jobs.RejectedResult, match="No local thumbnail"):
        jobs.run_reprocess(job.pk)
    sample_doc.refresh_from_db()
    assert sample_doc.content == "test content"


def test_metadata_inspection_never_authorizes_remote_processing(
    sample_doc: Document,
    mocker: MockerFixture,
) -> None:
    """Metadata inspection cannot send document content to a remote plugin."""
    from documents.views import DocumentViewSet

    select = mocker.patch(
        "documents.views.get_parser_registry"
    ).return_value.get_parser_for_file
    select.return_value = None
    assert (
        DocumentViewSet().get_metadata(sample_doc.source_path, sample_doc.mime_type)
        == []
    )
    assert select.call_args.kwargs["allow_remote"] is False
    assert select.call_args.kwargs["required_capabilities"] == ("metadata",)
