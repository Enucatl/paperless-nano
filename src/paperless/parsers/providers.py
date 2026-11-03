"""Adapters extending the existing parser interface with explicit results."""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

from paperless.parsers.contract import InvalidParserResult
from paperless.parsers.contract import ParserCapabilities
from paperless.parsers.contract import ParserRequest
from paperless.parsers.contract import ParserResult
from paperless.parsers.contract import file_reference
from paperless.parsers.contract import relative_path
from paperless.parsers.contract import resolve_reference

if TYPE_CHECKING:
    from paperless.parsers import ParserProtocol


class LegacyParserProvider:
    """Keep existing parser plugins and their context-managed cleanup usable."""

    def __init__(self, parser: ParserProtocol) -> None:
        """Wrap a core-selected parser configured before entering its context."""
        self.parser = parser
        cls = type(parser)
        self.identity = f"{cls.__module__}.{cls.__qualname__}:{parser.version}"
        self.uses_remote_service = bool(getattr(parser, "uses_remote_service", False))
        # Old plugins promise searchable PDFs, not PDF/A conformance.
        self.capabilities: ParserCapabilities = {
            "text": True,
            "searchable_pdf": bool(
                parser.can_produce_archive or parser.requires_pdf_rendition,
            ),
            "pdfa": cls.__module__
            in {
                "paperless.parsers.tesseract",
                "paperless.parsers.mail",
                "paperless.parsers.tika",
            },
            "thumbnail": True,
        }

    def archive_type(self, configuration: dict) -> str:
        """Describe the rendition format selected by the existing implementation."""
        output_type = configuration.get("ocr", {}).get("output_type", "")
        if type(self.parser).__module__ == "paperless.parsers.mail":
            output_type = configuration["execution"]["OCR_OUTPUT_TYPE"]
        elif type(self.parser).__module__ == "paperless.parsers.tesseract":
            user_args = configuration["ocr"].get("user_args") or {}
            output_type = user_args.get("output_type", output_type)
        return (
            "pdfa"
            if self.capabilities["pdfa"] and output_type.startswith("pdfa")
            else "searchable_pdf"
        )

    def process(self, request: ParserRequest, root: Path) -> ParserResult:
        """Run the existing parser and retain results before its context exits."""
        if self.uses_remote_service and not request["allow_remote"]:
            raise InvalidParserResult("Remote processing was not permitted")
        source = resolve_reference(root, request["source"])
        output_dir = relative_path(root, request["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        produce_archive = bool(
            {"searchable_pdf", "pdfa"} & set(request["required_outputs"]),
        )
        thumbnail_only = request["required_outputs"] == ["thumbnail"]
        if not thumbnail_only:
            self.parser.parse(
                source,
                request["mime_type"],
                produce_archive=produce_archive,
            )
        thumbnail = self.parser.get_thumbnail(source, request["mime_type"])
        archive = None if thumbnail_only else self.parser.get_archive_path()
        date = (
            self.parser.get_date()
            if not thumbnail_only and hasattr(self.parser, "get_date")
            else None
        )
        result: ParserResult = {
            "version": 1,
            "revision": request["revision"],
            "provider": self.identity,
            "text": "" if thumbnail_only else self.parser.get_text(),
            "page_count": (
                self.parser.get_page_count(source, request["mime_type"])
                if not thumbnail_only and hasattr(self.parser, "get_page_count")
                else None
            ),
            "date": date.isoformat() if date is not None else None,
            "archive": None,
            "thumbnail": None,
            "archive_type": None,
        }
        for name, path in (("thumbnail.webp", thumbnail), ("archive.pdf", archive)):
            if path is None:
                continue
            path = Path(path)
            if path.is_symlink() or not path.is_file():
                raise InvalidParserResult("Legacy parser output is not a regular file")
            if path.samefile(source):
                raise InvalidParserResult("Legacy parser output aliases its source")
            target = output_dir / name
            # Never follow an output path left by another attempt.
            with target.open("xb") as destination, path.open("rb") as stream:
                shutil.copyfileobj(stream, destination)
            key = "thumbnail" if name == "thumbnail.webp" else "archive"
            result[key] = file_reference(root, target)
        if archive is not None:
            result["archive_type"] = self.archive_type(request["configuration"])
        return result


class PlainTextProvider:
    """A framework-free replacement that extracts text and creates no rendition.

    The core supplies a thumbnail fallback and permits this provider only when
    archive policy does not require a searchable PDF or PDF/A rendition.
    """

    identity = "paperless.plain-text:1"
    name = "Paperless Plain Text Provider"
    version = "1"
    author = "Paperless-ngx Contributors"
    url = "https://github.com/paperless-ngx/paperless-ngx"
    uses_remote_service = False
    capabilities: ParserCapabilities = {
        "text": True,
        "searchable_pdf": False,
        "pdfa": False,
        "thumbnail": False,
    }

    @classmethod
    def supported_mime_types(cls) -> dict[str, str]:
        """Return the text formats this provider can handle."""
        return {"text/plain": ".txt", "text/csv": ".csv", "application/csv": ".csv"}

    @classmethod
    def score(
        cls,
        mime_type: str,
        filename: str,
        path: Path | None = None,
    ) -> int | None:
        """Prefer this explicitly installed provider for supported text formats."""
        return 30 if mime_type in cls.supported_mime_types() else None

    def process(self, request: ParserRequest, root: Path) -> ParserResult:
        """Read a durable input without importing Django or touching core state."""
        if request["mime_type"] not in self.supported_mime_types():
            raise InvalidParserResult("Unsupported text provider input")
        source = resolve_reference(root, request["source"])
        return {
            "version": 1,
            "revision": request["revision"],
            "provider": self.identity,
            "text": source.read_text(encoding="utf-8", errors="replace"),
            "page_count": None,
            "date": None,
            "archive": None,
            "thumbnail": None,
            "archive_type": None,
        }
