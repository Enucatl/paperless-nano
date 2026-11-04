"""Core policy for explicit parser requests and durable provider outputs."""

import dataclasses
import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4

from django.conf import settings

from paperless.config import OcrConfig
from paperless.config import RemoteOCRConfig
from paperless.parsers.contract import InvalidParserResult
from paperless.parsers.contract import file_reference
from paperless.parsers.contract import resolve_reference
from paperless.parsers.contract import validate_result
from paperless.parsers.providers import DefaultParser
from paperless.parsers.providers import LegacyParserProvider
from paperless.parsers.providers import configured_parser
from paperless.parsers.providers import default_configuration
from paperless.parsers.registry import get_parser_registry


def parser_class() -> type:
    """Load only the explicitly configured implementation."""
    from django.utils.module_loading import import_string

    return import_string(settings.PARSER)


def effective_configuration(*, mailrule_id: int | None = None) -> dict:
    """Snapshot common preferences and implementation-owned, secret-free settings."""
    cls = parser_class()
    implementation = (
        default_configuration(mailrule_id=mailrule_id)
        if cls is DefaultParser
        else cls().configuration_snapshot()
    )
    preferences = OcrConfig()
    return {
        "parser": settings.PARSER,
        "allow_remote_by_default": RemoteOCRConfig().remote_ocr_by_default,
        "settings": implementation,
        "archive_policy": preferences.archive_file_generation,
        "output_type": preferences.output_type,
    }


def parser_replay_safe(
    parser: type,
    source: Path,
    mime_type: str,
    filename: str,
    directory: Path,
    *,
    allow_remote: bool,
) -> bool:
    """Use an implementation's explicit replay promise, including the default adapter."""
    if parser is DefaultParser:
        try:
            parser = parser().selected_class(
                {
                    "source": file_reference(directory, source),
                    "mime_type": mime_type,
                    "filename": filename,
                    "allow_remote": allow_remote,
                },
                directory,
            )
        except InvalidParserResult as error:
            from documents.jobs import RejectedResult

            raise RejectedResult(str(error)) from error
    return getattr(parser, "replay_safe", False) is True or parser.__module__ in {
        "paperless.parsers.tesseract",
        "paperless.parsers.text",
        "paperless.parsers.mail",
    }


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
    operation: str = "process",
    invocation_id: str | None = None,
) -> dict:
    """Execute and validate a provider without granting it permanent-file ownership."""
    from documents.jobs import RejectedResult
    from documents.jobs import _copy
    from documents.jobs import _sync_directory
    from documents.jobs import _validate_file
    from documents.models import DocumentJob
    from documents.utils import compute_checksum

    provider = (
        parser
        if operation != "thumbnail" and hasattr(type(parser), "process")
        else LegacyParserProvider(parser)
    )
    configuration = json.loads(json.dumps(configuration))
    if isinstance(provider, LegacyParserProvider) and isinstance(
        getattr(parser, "settings", None), OcrConfig
    ):
        configuration["settings"]["ocr"] = dataclasses.asdict(parser.settings)
    if getattr(parser, "uses_remote_service", False) and not allow_remote:
        raise RejectedResult("Remote processing is not permitted for this document")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    output_dir = f"parser-{uuid4()}"
    staged = directory / (output_dir + "-input") / source.name
    _copy(source, staged)
    source = staged
    request = {
        "version": 1,
        "revision": "",
        "source": file_reference(directory, source),
        "mime_type": mime_type,
        "filename": filename,
        "settings": configuration["settings"],
        "operation": operation,
        "archive_policy": configuration["archive_policy"],
        "output_type": configuration["output_type"],
        "output_dir": output_dir,
        "allow_remote": allow_remote,
    }
    if invocation_id or job_id:
        request["invocation_id"] = invocation_id or str(job_id)
    request["revision"] = hashlib.sha256(
        json.dumps(request, sort_keys=True).encode()
    ).hexdigest()
    identity = provider.identity
    if not isinstance(identity, str) or not identity:
        raise RejectedResult("Parser identity must be a nonempty string")
    try:
        selection = (
            provider.selection(request, directory)
            if isinstance(provider, DefaultParser)
            else None
        )
    except InvalidParserResult as error:
        raise RejectedResult(str(error)) from error
    if job_id:
        job = DocumentJob.objects.get(pk=job_id)
        selected_provider = {
            "class": f"{type(parser).__module__}.{type(parser).__qualname__}",
            "identity": identity,
        }
        if selection is not None:
            selected_provider["implementation"] = selection
        previous_request = job.result.get("parser_request")
        if previous_request is not None and (
            job.configuration != configuration or job.provider != selected_provider
        ):
            raise RejectedResult(
                "Processing configuration or provider changed before retry"
            )
        job.provider = selected_provider
        job.configuration = configuration
        job.result["parser_request"] = request
        job.save(update_fields=["provider", "configuration", "result"])
    # Keep a separate snapshot so provider mutation cannot rewrite validation policy.
    frozen_request = json.loads(json.dumps(request))
    try:
        runtime_request = json.loads(json.dumps(request))
        result = provider.process(runtime_request, directory)
    except InvalidParserResult as error:
        raise RejectedResult(str(error)) from error
    try:
        validate_result(frozen_request, result, directory, identity)
        resolve_reference(directory, frozen_request["source"])
    except (ValueError, TypeError, KeyError, OSError) as error:
        raise RejectedResult(str(error)) from error
    if selection is not None and result.get("implementation") != selection["identity"]:
        raise RejectedResult("Selected parser changed during execution")
    if provider.identity != identity:
        raise RejectedResult("Parser identity changed during execution")
    if result["thumbnail"] is None:
        fallback = get_parser_registry().get_parser_for_file(
            mime_type,
            filename,
            source,
            allow_remote=False,
            required_capabilities=("thumbnail",),
        )
        if fallback is None:
            raise RejectedResult("No local thumbnail parser is available")
        thumbnail_configuration = default_configuration()
        with configured_parser(fallback, thumbnail_configuration) as thumbnail_parser:
            thumbnail_provider = LegacyParserProvider(thumbnail_parser)
            thumbnail_request = {
                **frozen_request,
                "operation": "thumbnail",
                "settings": thumbnail_configuration,
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
            frozen_request,
            result,
            directory,
            identity,
        )
    except (ValueError, TypeError, KeyError, OSError) as error:
        raise RejectedResult(str(error)) from error
    if result["thumbnail"] is None:
        raise RejectedResult("No local thumbnail parser produced a thumbnail")
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


def validate_parser_job(job: object) -> None:
    """Reject prepared results if configuration or selected provider has changed."""
    from documents.jobs import RejectedResult
    from documents.jobs import job_directory

    request = job.result.get("parser_request")
    if request is None:
        return  # Jobs prepared before the provider contract keep their recovery path.
    if job.configuration != effective_configuration(
        mailrule_id=job.input.get("mailrule_id")
    ):
        raise RejectedResult("Processing configuration changed before publication")
    directory = job_directory(job.pk)
    try:
        resolve_reference(directory, request["source"])
    except (ValueError, TypeError, KeyError, OSError) as error:
        raise RejectedResult(str(error)) from error
    cls = parser_class()
    if (
        f"{cls.__module__}.{cls.__qualname__}" != job.provider["class"]
        or cls().identity != job.provider["identity"]
    ):
        raise RejectedResult("Selected parser changed before publication")
    if cls is DefaultParser:
        try:
            selection = cls().selection(request, directory)
        except InvalidParserResult as error:
            raise RejectedResult(
                "Selected parser changed before publication"
            ) from error
        if selection != job.provider.get("implementation"):
            raise RejectedResult("Selected parser changed before publication")
