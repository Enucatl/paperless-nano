"""Thumbnail regeneration validates outputs without invoking OCR."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from documents.jobs import RejectedResult
from documents.management.commands.document_thumbnails import _process_document
from documents.models import Document
from paperless.parsers.providers import PlainTextProvider
from paperless.parsers.registry import get_parser_registry

if TYPE_CHECKING:
    from pathlib import Path

    from pytest_mock import MockerFixture

    from paperless_testing.dirs import PaperlessDirs

pytestmark = pytest.mark.django_db


def test_thumbnail_regeneration_never_parses(
    sample_doc: Document,
    thumbnail_webp_file: Path,
    paperless_dirs: PaperlessDirs,
    mocker: MockerFixture,
) -> None:
    """The shared provider boundary preserves thumbnail-only CLI behavior."""
    parse = mocker.patch("paperless.parsers.tesseract.RasterisedDocumentParser.parse")
    mocker.patch(
        "paperless.parsers.tesseract.RasterisedDocumentParser.get_thumbnail",
        return_value=thumbnail_webp_file,
    )
    original = sample_doc.source_path.read_bytes()
    _process_document(sample_doc.pk)
    parse.assert_not_called()
    assert sample_doc.source_path.read_bytes() == original
    assert sample_doc.thumbnail_path.read_bytes() == thumbnail_webp_file.read_bytes()
    assert not list((paperless_dirs.data_dir / "jobs").iterdir())


def test_changed_document_keeps_thumbnail(
    sample_doc: Document,
    thumbnail_webp_file: Path,
    mocker: MockerFixture,
) -> None:
    """A concurrent content update prevents an older thumbnail from publishing."""
    previous = sample_doc.thumbnail_path.read_bytes()

    def change_document(*args: object) -> Path:
        Document.objects.filter(pk=sample_doc.pk).update(content="newer content")
        return thumbnail_webp_file

    mocker.patch(
        "paperless.parsers.tesseract.RasterisedDocumentParser.get_thumbnail",
        side_effect=change_document,
    )
    with pytest.raises(RejectedResult, match="changed during thumbnail"):
        _process_document(sample_doc.pk)
    assert sample_doc.thumbnail_path.read_bytes() == previous


def test_thumbnail_selection_skips_text_only_provider(
    sample_doc: Document,
    thumbnail_webp_file: Path,
    mocker: MockerFixture,
) -> None:
    """A higher-scoring text-only plugin cannot break thumbnail regeneration."""

    class Provider(PlainTextProvider):
        """Advertise PDF text extraction without a thumbnail capability."""

        @classmethod
        def supported_mime_types(cls) -> dict[str, str]:
            return {"application/pdf": ".pdf"}

    process = mocker.patch.object(Provider, "process")
    mocker.patch.object(get_parser_registry(), "_external", [Provider])
    mocker.patch(
        "paperless.parsers.tesseract.RasterisedDocumentParser.get_thumbnail",
        return_value=thumbnail_webp_file,
    )
    _process_document(sample_doc.pk)
    process.assert_not_called()
    assert sample_doc.thumbnail_path.read_bytes() == thumbnail_webp_file.read_bytes()


def test_invalid_thumbnail_keeps_published_file(
    sample_doc: Document,
    tmp_path: Path,
    mocker: MockerFixture,
) -> None:
    """A provider cannot replace a valid thumbnail with undecodable output."""
    previous = sample_doc.thumbnail_path.read_bytes()
    invalid = tmp_path / "invalid.webp"
    invalid.write_text("not an image")
    mocker.patch(
        "paperless.parsers.tesseract.RasterisedDocumentParser.get_thumbnail",
        return_value=invalid,
    )
    with pytest.raises(RejectedResult):
        _process_document(sample_doc.pk)
    assert sample_doc.thumbnail_path.read_bytes() == previous


def test_lost_source_retains_staged_input(
    sample_doc: Document,
    thumbnail_webp_file: Path,
    paperless_dirs: PaperlessDirs,
    mocker: MockerFixture,
) -> None:
    """A disappearing permanent source cannot make cleanup erase its staged copy."""
    source = sample_doc.source_path
    original = source.read_bytes()

    def remove_source(*args: object) -> Path:
        source.unlink()
        return thumbnail_webp_file

    mocker.patch(
        "paperless.parsers.tesseract.RasterisedDocumentParser.get_thumbnail",
        side_effect=remove_source,
    )
    with pytest.raises(FileNotFoundError):
        _process_document(sample_doc.pk)
    inputs = list((paperless_dirs.data_dir / "jobs").glob("*/parser-*-input/*"))
    assert len(inputs) == 1
    assert inputs[0].read_bytes() == original
