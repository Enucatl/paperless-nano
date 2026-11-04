"""Adapters extending the existing parser interface with explicit results."""

from __future__ import annotations

import dataclasses
import shutil
from contextlib import contextmanager
from contextlib import nullcontext
from pathlib import Path
from typing import TYPE_CHECKING

from paperless.parsers.contract import InvalidParserResult
from paperless.parsers.contract import ParserProvider
from paperless.parsers.contract import ParserRequest
from paperless.parsers.contract import ParserResult
from paperless.parsers.contract import configuration_snapshot
from paperless.parsers.contract import file_reference
from paperless.parsers.contract import relative_path
from paperless.parsers.contract import resolve_reference
from paperless.parsers.registry import get_parser_registry

if TYPE_CHECKING:
    from paperless.parsers import ParserProtocol


def default_configuration(*, mailrule_id: int | None = None) -> dict:
    """Snapshot effective processing values without storing service credentials."""
    from django.conf import settings

    from paperless.config import BarcodeConfig
    from paperless.config import OcrConfig
    from paperless.config import RemoteOCRConfig

    remote = dataclasses.asdict(RemoteOCRConfig())
    remote.pop("remote_ocr_api_key")
    configuration = {
        "ocr": dataclasses.asdict(OcrConfig()),
        "barcodes": dataclasses.asdict(BarcodeConfig()),
        "remote": remote,
        "execution": {
            name: getattr(settings, name)
            for name in (
                "THREADS_PER_WORKER",
                "THUMBNAIL_FONT_NAME",
                "TIKA_ENDPOINT",
                "TIKA_GOTENBERG_ENDPOINT",
                "REMOTE_OCR_ALLOW_INTERNAL_ENDPOINTS",
                "EMAIL_PARSE_DEFAULT_LAYOUT",
                "OCR_OUTPUT_TYPE",
                "CELERY_TASK_TIME_LIMIT",
            )
        },
    }
    if mailrule_id:
        from paperless_mail.models import MailRule

        configuration["mailrule_id"] = mailrule_id
        configuration["mail_layout"] = (
            MailRule.objects.filter(pk=mailrule_id)
            .values_list("pdf_layout", flat=True)
            .first()
        )
    return configuration


@contextmanager
def configured_parser(
    parser_class: type, configuration: dict, *, mailrule_id: int | None = None
):
    """Keep old plugin construction while supplying snapshots to built-in parsers."""
    from paperless.config import RemoteOCRConfig
    from paperless.parsers import ParserContext

    if f"{parser_class.__module__}.{parser_class.__qualname__}" in {
        "paperless.parsers.tesseract.RasterisedDocumentParser",
        "paperless.parsers.remote.RemoteDocumentParser",
        "paperless.parsers.text.TextDocumentParser",
        "paperless.parsers.mail.MailDocumentParser",
        "paperless.parsers.tika.TikaDocumentParser",
    }:
        runtime = {
            **configuration,
            "remote": {
                **configuration["remote"],
                "remote_ocr_api_key": RemoteOCRConfig().remote_ocr_api_key,
            },
        }
        parser = parser_class(configuration=runtime)
    else:
        parser = parser_class()
    with (
        parser
        if hasattr(type(parser), "__enter__")
        else nullcontext(parser) as instance
    ):
        if not hasattr(type(instance), "process"):
            instance.configure(
                ParserContext(
                    mailrule_id=mailrule_id or configuration.get("mailrule_id"),
                    configuration=configuration,
                )
            )
        yield instance


class LegacyParserProvider:
    """Keep existing parser plugins and their context-managed cleanup usable."""

    def __init__(self, parser: ParserProtocol) -> None:
        """Wrap a core-selected parser configured before entering its context."""
        self.parser = parser
        cls = type(parser)
        self.identity = f"{cls.__module__}.{cls.__qualname__}:{parser.version}"
        self.uses_remote_service = bool(getattr(parser, "uses_remote_service", False))

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
            if type(self.parser).__module__
            in {
                "paperless.parsers.tesseract",
                "paperless.parsers.mail",
                "paperless.parsers.tika",
            }
            and output_type.startswith("pdfa")
            else "searchable_pdf"
        )

    def process(self, request: ParserRequest, root: Path) -> ParserResult:
        """Run the existing parser and retain results before its context exits."""
        if self.uses_remote_service and not request["allow_remote"]:
            raise InvalidParserResult("Remote processing was not permitted")
        source = resolve_reference(root, request["source"])
        output_dir = relative_path(root, request["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        from documents.consumer import should_produce_archive

        thumbnail_only = request["operation"] == "thumbnail"
        produce_archive = not thumbnail_only and should_produce_archive(
            self.parser,
            request["mime_type"],
            source,
            configuration=request["settings"],
        )
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
            result["archive_type"] = self.archive_type(request["settings"])
        return result


class DefaultParser(ParserProvider):
    """Dispatch to the existing built-in and entry-point parser implementations."""

    identity = "paperless.default:1"
    uses_remote_service = False

    @staticmethod
    def accepted_mime_types() -> dict[str, str]:
        """Expose formats from this implementation's engine registry."""
        return {
            mime: extension
            for parser in get_parser_registry().all_parsers()
            for mime, extension in parser.supported_mime_types().items()
        }

    @staticmethod
    def initialize_worker() -> None:
        """Initialize engines owned by this implementation."""
        from paperless.parsers.registry import init_builtin_parsers

        init_builtin_parsers()

    @staticmethod
    def check() -> list:
        """Check this implementation's registry and OCR installation."""
        from django.core.checks import Error

        from paperless.checks import check_default_language_available

        if not get_parser_registry().all_parsers():
            return [Error("No parsers found. The consumer cannot consume documents.")]
        return check_default_language_available(None)

    def configuration_snapshot(self) -> dict:
        """Snapshot the default implementation's effective engine settings."""
        return default_configuration()

    def configuration_for(self, context: dict) -> dict:
        """Include mail layout in this implementation's configuration."""
        return default_configuration(mailrule_id=context.get("mailrule_id"))

    def selected_class(self, request: ParserRequest, root: Path) -> type:
        """Resolve only the default implementation's legacy dispatch."""
        cls = get_parser_registry().get_parser_for_file(
            request["mime_type"],
            request["filename"],
            resolve_reference(root, request["source"]),
            allow_remote=request["allow_remote"],
        )
        if cls is None:
            raise InvalidParserResult(f"No parser for {request['mime_type']}")
        return cls

    def selection(self, request: ParserRequest, root: Path) -> dict:
        """Describe the selected instance for retry and publication checks."""
        cls = self.selected_class(request, root)
        with configured_parser(cls, request["settings"]) as parser:
            provider = (
                parser
                if hasattr(type(parser), "process")
                else LegacyParserProvider(parser)
            )
            return {
                "class": f"{cls.__module__}.{cls.__qualname__}",
                "version": parser.version,
                "configuration": configuration_snapshot(provider, {})
                if hasattr(provider, "configuration_snapshot")
                else request["settings"],
                "identity": provider.identity,
                "replay_safe": getattr(parser, "replay_safe", False),
                "uses_remote_service": provider.uses_remote_service,
            }

    def process(self, request: ParserRequest, root: Path) -> ParserResult:
        """Select and invoke the legacy implementation behind the default boundary."""
        from paperless.config import OcrConfig
        from paperless.config import RemoteOCRConfig
        from paperless.parsers.contract import validate_result

        cls = self.selected_class(request, root)
        with configured_parser(cls, request["settings"]) as parser:
            provider = (
                parser
                if hasattr(type(parser), "process")
                else LegacyParserProvider(parser)
            )
            if (
                isinstance(getattr(parser, "settings", None), OcrConfig)
                and dataclasses.asdict(parser.settings) != request["settings"]["ocr"]
            ):
                raise InvalidParserResult(
                    "Processing configuration changed during parser construction"
                )
            identity = provider.identity
            if provider.uses_remote_service and not request["allow_remote"]:
                raise InvalidParserResult("Remote processing was not permitted")
            # Existing entry-point providers retain their original request shape.
            if not isinstance(provider, LegacyParserProvider):
                request = {
                    **request,
                    "configuration": request["settings"],
                    "required_outputs": ["text"],
                }
                if provider.uses_remote_service:
                    request["configuration"]["remote"]["remote_ocr_api_key"] = (
                        RemoteOCRConfig().remote_ocr_api_key
                    )
            result = provider.process(request, root)
            validate_result(request, result, root, identity)
            if provider.identity != identity:
                raise InvalidParserResult("Parser identity changed during execution")
            return {**result, "provider": self.identity, "execution": identity}


class LocalThumbnailProvider(DefaultParser):
    """Own local auxiliary rendering independently of the main parser selection."""

    identity = "paperless.local-thumbnail:1"

    def selected_class(self, request: ParserRequest, root: Path) -> type:
        """Select only implementations declaring local thumbnail support."""
        cls = get_parser_registry().get_parser_for_file(
            request["mime_type"],
            request["filename"],
            resolve_reference(root, request["source"]),
            allow_remote=False,
            required_capabilities=("thumbnail",),
        )
        if cls is None:
            raise InvalidParserResult("No local thumbnail parser is available")
        return cls

    def process(self, request: ParserRequest, root: Path) -> ParserResult:
        """Adapt legacy rendering within the auxiliary implementation boundary."""
        cls = self.selected_class(request, root)
        with configured_parser(cls, request["settings"]) as parser:
            provider = LegacyParserProvider(parser)
            result = provider.process(request, root)
            from paperless.parsers.contract import validate_result

            validate_result(request, result, root, provider.identity)
            return {**result, "provider": self.identity, "execution": provider.identity}


class PlainTextProvider(ParserProvider):
    """A framework-free replacement that extracts text and creates no rendition.

    The core supplies a local thumbnail fallback when one is available.
    """

    identity = "paperless.plain-text:1"
    name = "Paperless Plain Text Provider"
    version = "1"
    author = "Paperless-ngx Contributors"
    url = "https://github.com/paperless-ngx/paperless-ngx"
    uses_remote_service = False
    replay_safe = True

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
