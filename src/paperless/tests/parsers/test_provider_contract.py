"""Parser contracts reject unsafe files and remain usable without Django."""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from paperless.parsers.contract import InvalidParserResult
from paperless.parsers.contract import file_reference
from paperless.parsers.contract import resolve_reference
from paperless.parsers.contract import validate_result
from paperless.parsers.providers import LegacyParserProvider
from paperless.parsers.providers import PlainTextProvider


@pytest.fixture
def request_data(tmp_path):
    """Provide a serialized private input and explicit text-only archive policy."""
    source = tmp_path / "input.txt"
    source.write_text("Invoice number 42")
    return {
        "version": 1,
        "revision": "revision-1",
        "source": file_reference(tmp_path, source),
        "mime_type": "text/plain",
        "filename": "invoice.txt",
        "settings": {"ocr": {"archive_file_generation": "never"}},
        "archive_policy": "never",
        "output_type": "pdfa",
        "operation": "process",
        "output_dir": "attempt-1",
        "allow_remote": False,
    }


def test_text_only_provider_roundtrip(request_data, tmp_path):
    """A replacement receives JSON values and returns text without renditions."""
    provider = PlainTextProvider()
    request = json.loads(json.dumps(request_data))
    result = json.loads(json.dumps(provider.process(request, tmp_path)))
    validate_result(request, result, tmp_path, provider.identity)
    assert result["text"] == "Invoice number 42"
    assert result["archive"] is None
    assert result["thumbnail"] is None


@pytest.mark.parametrize("policy", ["auto", "always", "never"])
def test_archive_preferences_are_advisory(request_data, tmp_path, policy):
    """Every archive preference permits a text-only replacement."""
    provider = PlainTextProvider()
    request_data["archive_policy"] = policy
    result = provider.process(request_data, tmp_path)
    validate_result(request_data, result, tmp_path, provider.identity)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("version", True),
        ("revision", "old-revision"),
        ("provider", "other-provider"),
        ("text", None),
        ("page_count", True),
        ("page_count", 0),
        ("page_count", -1),
        ("date", "yesterday"),
        ("date", 12),
        ("archive_type", "pdfa"),
    ],
)
def test_invalid_result(request_data, tmp_path, field, value):
    """Malformed, stale and mismatched provider results are rejected."""
    provider = PlainTextProvider()
    result = provider.process(request_data, tmp_path)
    result[field] = value
    with pytest.raises(InvalidParserResult):
        validate_result(request_data, result, tmp_path, provider.identity)


def test_changed_input_rejected(request_data, tmp_path):
    """A provider cannot silently mutate the staged source it was handed."""
    provider = PlainTextProvider()
    result = provider.process(request_data, tmp_path)
    (tmp_path / "input.txt").write_text("changed")
    with pytest.raises(InvalidParserResult, match="changed"):
        validate_result(request_data, result, tmp_path, provider.identity)


@pytest.mark.parametrize("name", ["../input.txt", "/etc/passwd", ""])
def test_reference_cannot_escape_operation(request_data, tmp_path, name):
    """References cannot address another job or permanent media file."""
    reference = {**request_data["source"], "path": name}
    with pytest.raises(InvalidParserResult):
        resolve_reference(tmp_path, reference)


def test_reference_cannot_follow_symlink(request_data, tmp_path):
    """Symlinks are rejected even when their target is inside the operation."""
    (tmp_path / "link.txt").symlink_to(tmp_path / "input.txt")
    reference = {**request_data["source"], "path": "link.txt"}
    with pytest.raises(InvalidParserResult, match="symlinks"):
        resolve_reference(tmp_path, reference)


def test_output_cannot_alias_input(request_data, tmp_path):
    """A hard-linked source cannot masquerade as generated provider output."""
    output = tmp_path / request_data["output_dir"]
    output.mkdir()
    archive = output / "archive.pdf"
    archive.hardlink_to(tmp_path / "input.txt")
    provider = PlainTextProvider()
    result = provider.process(request_data, tmp_path)
    result.update(
        archive=file_reference(tmp_path, archive), archive_type="searchable_pdf"
    )
    with pytest.raises(InvalidParserResult, match="aliases"):
        validate_result(request_data, result, tmp_path, provider.identity)


def test_pdfa_is_distinct_from_searchable_pdf(request_data, tmp_path):
    """A supported archive format may differ from the requested preference."""
    output = tmp_path / request_data["output_dir"]
    output.mkdir()
    archive = output / "archive.pdf"
    archive.write_bytes(b"%PDF-1.7\n")
    provider = PlainTextProvider()
    result = provider.process(request_data, tmp_path)
    result.update(
        archive=file_reference(tmp_path, archive), archive_type="searchable_pdf"
    )
    request_data["output_type"] = "pdfa"
    validate_result(request_data, result, tmp_path, provider.identity)


def test_contract_and_replacement_do_not_import_django():
    """Standalone providers can use the contract without loading the framework."""
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from paperless.parsers.providers import PlainTextProvider; "
                "import sys; assert not any(k == 'django' or k.startswith('django.') "
                "for k in sys.modules)"
            ),
        ],
        check=True,
        cwd=Path(__file__).parents[3],
    )


def test_framework_startup_preserves_checks_and_celery_exports():
    """Normal Django startup still registers checks and selects its Celery app."""
    subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import django; django.setup(); "
                "from paperless import celery_app, paths_check; "
                "from paperless.celery import app; "
                "from django.core.checks.registry import registry; "
                "assert celery_app is app; "
                "assert paths_check in registry.registered_checks"
            ),
        ],
        check=True,
        cwd=Path(__file__).parents[3],
    )


def test_legacy_adapter_retains_outputs_and_identity(request_data, tmp_path):
    """Legacy plugins keep their existing call shape and relinquish file lifetime."""

    class Parser:
        name = "legacy"
        version = "1"
        can_produce_archive = False
        requires_pdf_rendition = False

        def parse(self, path, mime_type, *, produce_archive):
            assert path == tmp_path / "input.txt"
            assert mime_type == "text/plain"
            assert produce_archive is False

        def get_text(self):
            return "legacy text"

        def get_archive_path(self):
            return None

        def get_thumbnail(self, path, mime_type):
            thumbnail = tmp_path / "scratch.webp"
            thumbnail.write_bytes(b"RIFF test WEBP")
            return thumbnail

    provider = LegacyParserProvider(Parser())
    result = provider.process(request_data, tmp_path)
    (tmp_path / "scratch.webp").unlink()
    validate_result(request_data, result, tmp_path, provider.identity)
    assert result["text"] == "legacy text"
    assert (
        resolve_reference(tmp_path, result["thumbnail"]).read_bytes()
        == b"RIFF test WEBP"
    )
    assert result["provider"].endswith("Parser:1")


@pytest.mark.parametrize("capability", ["thumbnail", "metadata"])
def test_legacy_fallback_skips_text_only_provider(capability):
    """Registry fallback filters capabilities before scoring eligible parsers."""
    from paperless.parsers.registry import ParserRegistry
    from paperless.parsers.text import TextDocumentParser

    registry = ParserRegistry()
    registry.register_builtin(PlainTextProvider)
    registry.register_builtin(TextDocumentParser)
    assert registry.get_parser_for_file("text/plain", "x.txt") is PlainTextProvider
    assert (
        registry.get_parser_for_file(
            "text/plain",
            "x.txt",
            allow_remote=False,
            required_capabilities=(capability,),
        )
        is TextDocumentParser
    )


@pytest.mark.django_db
def test_tesseract_snapshot_and_engine_concurrency(settings, mocker):
    """A configuration snapshot bypasses ORM reads and bounds overridden jobs."""
    from dataclasses import asdict

    from paperless.config import OcrConfig
    from paperless.parsers.tesseract import RasterisedDocumentParser

    configuration = {
        "ocr": asdict(OcrConfig()),
        "execution": {"THREADS_PER_WORKER": 2},
    }
    configuration["ocr"]["language"] = "deu"
    configuration["ocr"]["user_args"] = {"jobs": 32}
    settings.THREADS_PER_WORKER = 8
    mocker.patch(
        "paperless.parsers.tesseract.OcrConfig",
        side_effect=AssertionError("Snapshot must not reread database"),
    )
    with RasterisedDocumentParser(configuration=configuration) as parser:
        parameters = parser.construct_ocrmypdf_parameters(
            "input.pdf",
            "application/pdf",
            "archive.pdf",
            "text.txt",
        )
    assert parameters["jobs"] == 2
    assert parameters["language"] == "deu"


def test_failed_client_startup_cleans_scratch_and_open_clients(mocker):
    """A timeout while opening the second client cannot leak the first client."""
    from paperless.parsers.tika import TikaDocumentParser

    tika = mocker.patch("tika_client.TikaClient")
    mocker.patch("gotenberg_client.GotenbergClient", side_effect=TimeoutError)
    parser = TikaDocumentParser()
    with pytest.raises(TimeoutError):
        parser.__enter__()
    assert not parser._tempdir.exists()
    tika.return_value.__exit__.assert_called_once()


def test_explicit_selection_startup_does_not_load_default_engines():
    """Startup, checks and file naming do not initialize unselected parsers."""
    import os

    subprocess.run(
        [
            sys.executable,
            "-c",
            """
import django
django.setup()
from paperless.celery import on_worker_process_init
from documents.checks import parser_check
from documents.parsers import get_default_file_extension, is_mime_type_supported, get_supported_file_extensions
on_worker_process_init()
assert parser_check(None) == []
assert get_default_file_extension('application/pdf') == '.pdf'
assert is_mime_type_supported('application/pdf')
assert '.pdf' in get_supported_file_extensions()
import sys
assert not any(name in sys.modules for name in (
    'paperless.parsers.tesseract', 'paperless.parsers.remote',
    'paperless.parsers.mail', 'paperless.parsers.tika', 'ocrmypdf',
))
""",
        ],
        check=True,
        env={
            **os.environ,
            "PAPERLESS_PARSER": "paperless.parsers.providers.PlainTextProvider",
        },
        cwd=Path(__file__).parents[3],
    )


def test_legacy_configuration_preserves_mailrule_context():
    """Default dispatch preserves the context supplied to existing mail plugins."""
    from paperless.parsers.providers import configured_parser

    class Parser:
        def configure(self, context):
            self.context = context

    with configured_parser(Parser, {"mailrule_id": 42}) as parser:
        assert parser.context.mailrule_id == 42
