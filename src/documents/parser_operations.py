"""Core policy for explicit parser requests and durable provider outputs."""

import dataclasses
import hashlib
import json
import os
from contextlib import contextmanager
from contextlib import nullcontext
from pathlib import Path
from uuid import uuid4

from django.conf import settings

from paperless.config import BarcodeConfig
from paperless.config import OcrConfig
from paperless.config import RemoteOCRConfig
from paperless.parsers import ParserContext
from paperless.parsers.contract import InvalidParserResult
from paperless.parsers.contract import file_reference
from paperless.parsers.contract import resolve_reference
from paperless.parsers.contract import validate_result
from paperless.parsers.providers import LegacyParserProvider
from paperless.parsers.registry import get_parser_registry


def effective_configuration(*, mailrule_id: int | None = None) -> dict:
    """Snapshot effective processing values without storing service credentials."""
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
                ParserContext(mailrule_id=mailrule_id, configuration=configuration)
            )
        yield instance


def _required_outputs(
    parser: object, provider: object, source: Path, mime_type: str, configuration: dict
) -> list[str]:
    """Preserve legacy archive policy and require advertised modern renditions."""
    from documents.consumer import should_produce_archive
    from documents.jobs import RejectedResult
    from paperless.parsers.utils import pdf_born_digital_text

    required = ["text"]
    if isinstance(provider, LegacyParserProvider):
        archive = should_produce_archive(
            parser, mime_type, source, configuration=configuration
        )
    else:
        generation = configuration["ocr"]["archive_file_generation"]
        archive = bool(getattr(parser, "requires_pdf_rendition", False)) or (
            mime_type
            not in {"application/pdf", "text/plain", "text/csv", "application/csv"}
            and not mime_type.startswith("image/")
        )
        if generation == "always":
            archive = True
        elif generation == "auto":
            archive |= mime_type.startswith("image/") or (
                mime_type == "application/pdf" and not pdf_born_digital_text(source)[1]
            )
    if archive:
        kind = (
            "pdfa"
            if configuration["ocr"]["output_type"].startswith("pdfa")
            else "searchable_pdf"
        )
        # Older plugins promise a display PDF, not necessarily PDF/A compliance.
        if isinstance(provider, LegacyParserProvider):
            kind = provider.archive_type(configuration)
        if not provider.capabilities[kind]:
            raise RejectedResult(
                f"Selected parser cannot produce required {kind} rendition"
            )
        required.append(kind)
    if provider.capabilities["thumbnail"]:
        required.append("thumbnail")
    return required


def run_parser(
    parser: object,
    source: Path,
    mime_type: str,
    filename: str,
    configuration: dict,
    directory: Path,
    *,
    job_id: str | None = None,
    allow_remote: bool = False,
    required_outputs: list[str] | None = None,
) -> dict:
    """Execute and validate a provider without granting it permanent-file ownership."""
    from documents.jobs import RejectedResult
    from documents.jobs import _copy
    from documents.jobs import _sync_directory
    from documents.jobs import _validate_file
    from documents.models import DocumentJob
    from documents.utils import compute_checksum

    provider = (
        parser if hasattr(type(parser), "process") else LegacyParserProvider(parser)
    )
    configuration = json.loads(json.dumps(configuration))
    if isinstance(provider, LegacyParserProvider) and isinstance(
        getattr(parser, "settings", None), OcrConfig
    ):
        configuration["ocr"] = dataclasses.asdict(parser.settings)
    if getattr(parser, "uses_remote_service", False) and not allow_remote:
        raise RejectedResult("Remote processing is not permitted for this document")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    output_dir = f"parser-{uuid4()}"
    staged = directory / (output_dir + "-input") / source.name
    _copy(source, staged)
    source = staged
    required = (
        required_outputs
        if required_outputs is not None
        else _required_outputs(parser, provider, source, mime_type, configuration)
    )
    request = {
        "version": 1,
        "revision": "",
        "source": file_reference(directory, source),
        "mime_type": mime_type,
        "filename": filename,
        "configuration": configuration,
        "output_dir": output_dir,
        "required_outputs": required,
        "allow_remote": allow_remote,
    }
    request["revision"] = hashlib.sha256(
        json.dumps(request, sort_keys=True).encode()
    ).hexdigest()
    identity = provider.identity
    if not isinstance(identity, str) or not identity:
        raise RejectedResult("Parser identity must be a nonempty string")
    if job_id:
        job = DocumentJob.objects.get(pk=job_id)
        selected_provider = {
            "class": f"{type(parser).__module__}.{type(parser).__qualname__}",
            "name": parser.name,
            "version": parser.version,
            "identity": identity,
        }
        previous_request = job.result.get("parser_request")
        if previous_request is not None and (
            previous_request["configuration"] != configuration
            or job.provider != selected_provider
        ):
            raise RejectedResult(
                "Processing configuration or provider changed before retry"
            )
        job.provider = selected_provider
        job.configuration.update(configuration)
        job.configuration["produce_archive"] = any(
            kind in required for kind in ("pdfa", "searchable_pdf")
        )
        job.result["parser_request"] = request
        job.save(update_fields=["provider", "configuration", "result"])
    # Keep a separate snapshot so provider mutation cannot rewrite validation policy.
    frozen_request = json.loads(json.dumps(request))
    try:
        runtime_request = json.loads(json.dumps(request))
        if getattr(provider, "uses_remote_service", False):
            runtime_request["configuration"]["remote"]["remote_ocr_api_key"] = (
                RemoteOCRConfig().remote_ocr_api_key
            )
        result = provider.process(runtime_request, directory)
    except InvalidParserResult as error:
        raise RejectedResult(str(error)) from error
    try:
        validate_result(frozen_request, result, directory, identity)
        resolve_reference(directory, frozen_request["source"])
    except (ValueError, TypeError, KeyError, OSError) as error:
        raise RejectedResult(str(error)) from error
    if provider.identity != identity:
        raise RejectedResult("Parser identity changed during execution")
    if (
        not isinstance(provider, LegacyParserProvider)
        and result["archive"] is not None
        and (
            result["archive_type"] not in required
            or not provider.capabilities[result["archive_type"]]
        )
    ):
        raise RejectedResult("Parser returned an unrequested or unsupported rendition")
    if not provider.capabilities["thumbnail"]:
        fallback = get_parser_registry().get_parser_for_file(
            mime_type,
            filename,
            source,
            allow_remote=False,
            required_capabilities=("thumbnail",),
        )
        if fallback is None:
            raise RejectedResult("No local thumbnail parser is available")
        with configured_parser(fallback, configuration) as thumbnail_parser:
            thumbnail_provider = (
                thumbnail_parser
                if hasattr(type(thumbnail_parser), "process")
                else LegacyParserProvider(thumbnail_parser)
            )
            thumbnail_request = {
                **frozen_request,
                "required_outputs": ["thumbnail"],
                "allow_remote": False,
            }
            thumbnail_identity = thumbnail_provider.identity
            try:
                thumbnail_result = thumbnail_provider.process(
                    json.loads(json.dumps(thumbnail_request)), directory
                )
                validate_result(
                    thumbnail_request, thumbnail_result, directory, thumbnail_identity
                )
            except (ValueError, TypeError, KeyError, OSError) as error:
                raise RejectedResult(str(error)) from error
            if thumbnail_provider.identity != thumbnail_identity:
                raise RejectedResult(
                    "Thumbnail parser identity changed during execution"
                )
            result["thumbnail"] = thumbnail_result["thumbnail"]
            result["thumbnail_provider"] = thumbnail_identity
    try:
        validate_result(
            {**frozen_request, "required_outputs": [*required, "thumbnail"]},
            result,
            directory,
            identity,
        )
    except (ValueError, TypeError, KeyError, OSError) as error:
        raise RejectedResult(str(error)) from error
    for key, mime in (("thumbnail", "image/webp"), ("archive", "application/pdf")):
        if result[key] is not None:
            path = resolve_reference(directory, result[key])
            _validate_file(
                directory,
                {"name": result[key]["path"], "checksum": compute_checksum(path)},
                mime,
            )
            with path.open("rb") as stream:
                os.fsync(stream.fileno())
            _sync_directory(path.parent)
    if job_id:
        job.refresh_from_db()
        job.result["parser_result"] = result
        job.save(update_fields=["result"])
    return result


def validate_parser_job(job: object, *, registry: object = None) -> None:
    """Reject prepared results if configuration or selected provider has changed."""
    from documents.jobs import RejectedResult
    from documents.jobs import job_directory

    request = job.result.get("parser_request")
    if request is None:
        return  # Jobs prepared before the provider contract keep their recovery path.
    if request["configuration"] != effective_configuration(
        mailrule_id=job.input.get("mailrule_id")
    ):
        raise RejectedResult("Processing configuration changed before publication")
    directory = job_directory(job.pk)
    source = resolve_reference(directory, request["source"])
    parser_class = (registry or get_parser_registry()).get_parser_for_file(
        request["mime_type"],
        request["filename"],
        source,
        allow_remote=request["allow_remote"],
    )
    if (
        parser_class is None
        or f"{parser_class.__module__}.{parser_class.__qualname__}"
        != job.provider["class"]
    ):
        raise RejectedResult("Selected parser changed before publication")
    with configured_parser(
        parser_class, request["configuration"], mailrule_id=job.input.get("mailrule_id")
    ) as parser:
        provider = (
            parser if hasattr(type(parser), "process") else LegacyParserProvider(parser)
        )
        if provider.identity != job.provider["identity"]:
            raise RejectedResult("Parser identity changed before publication")
