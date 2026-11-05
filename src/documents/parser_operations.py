"""Core policy for explicit parser requests and durable provider outputs."""

import hashlib
import json
import os
from pathlib import Path
from uuid import uuid4
from uuid import uuid5

from django.conf import settings

from paperless.config import OcrConfig
from paperless.config import RemoteOCRConfig
from paperless.parsers.contract import InvalidParserResult
from paperless.parsers.contract import configuration_snapshot
from paperless.parsers.contract import execution_snapshot
from paperless.parsers.contract import file_reference
from paperless.parsers.contract import resolve_reference
from paperless.parsers.contract import validate_result
from paperless.parsers.providers import LocalThumbnailProvider


def parser_class() -> type:
    """Load only the explicitly configured implementation."""
    from django.utils.module_loading import import_string

    return import_string(settings.PARSER)


def effective_configuration(
    *, mailrule_id: int | None = None, thumbnail_only: bool = False
) -> dict:
    """Snapshot common preferences and implementation-owned, secret-free settings."""
    provider = LocalThumbnailProvider() if thumbnail_only else parser_class()()
    implementation = configuration_snapshot(provider, {"mailrule_id": mailrule_id})
    preferences = OcrConfig()
    return {
        "parser": "paperless.parsers.providers.LocalThumbnailProvider"
        if thumbnail_only
        else settings.PARSER,
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
    from documents.job_state import RejectedResult

    try:
        request = {
            "source": file_reference(directory, source),
            "mime_type": mime_type,
            "filename": filename,
            "allow_remote": allow_remote,
            "settings": effective_configuration()["settings"],
        }
        return execution_snapshot(parser(), request, directory)["replay_safe"]
    except InvalidParserResult as error:
        raise RejectedResult(str(error)) from error


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
    from documents.job_state import RejectedResult
    from documents.job_storage import atomic_copy as _copy
    from documents.job_storage import sync_directory as _sync_directory
    from documents.job_storage import validate_file as _validate_file
    from documents.models import DocumentJob
    from documents.utils import compute_checksum

    provider = parser
    configuration = json.loads(json.dumps(configuration))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    request = None
    operation_key = "parser.thumbnail" if operation == "thumbnail" else "parser.process"
    if job_id:
        job = DocumentJob.objects.get(pk=job_id)
        request = job.result.get("parser_request")
    if request is None:
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
            request["invocation_id"] = (
                str(uuid5(job.pk, operation_key)) if job_id else invocation_id
            )
        request["revision"] = hashlib.sha256(
            json.dumps(request, sort_keys=True).encode()
        ).hexdigest()
    else:
        try:
            resolve_reference(directory, request["source"])
        except InvalidParserResult as error:
            raise RejectedResult(str(error)) from error
    identity = provider.identity
    try:
        selection = execution_snapshot(provider, request, directory)
    except InvalidParserResult as error:
        raise RejectedResult(str(error)) from error
    if job_id:
        job = DocumentJob.objects.get(pk=job_id)
        selected_provider = {
            "class": f"{type(parser).__module__}.{type(parser).__qualname__}",
            "identity": identity,
        }
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

    def validate_output(result: dict) -> None:
        """Apply the same domain checks to immediate and deferred outputs."""
        try:
            validate_result(frozen_request, result, directory, identity)
            if result.get("execution", result["provider"]) != selection["identity"]:
                raise InvalidParserResult("Selected parser changed during execution")
            if execution_snapshot(provider, frozen_request, directory) != selection:
                raise InvalidParserResult("Selected parser changed during execution")
            if provider.identity != identity:
                raise InvalidParserResult("Parser identity changed during execution")
        except (ValueError, TypeError, KeyError, OSError) as error:
            raise RejectedResult(str(error)) from error

    def execute(identifier: str) -> dict:
        """Run one admitted attempt using private output files."""
        import shutil

        from paperless.parsers.contract import relative_path

        output = relative_path(directory, frozen_request["output_dir"])
        if output.exists():
            shutil.rmtree(output)
        runtime_request = json.loads(json.dumps(frozen_request))
        runtime_request["invocation_id"] = identifier
        return provider.process(runtime_request, directory)

    try:
        if job_id:
            from documents.operation_journal import invoke

            result = invoke(
                job,
                operation_key,
                identity,
                {"request": frozen_request, "selection": selection},
                execute,
                replay_safe=selection["replay_safe"],
                provider=selection,
                remote_allowed=frozen_request["allow_remote"],
                reconcile=(
                    lambda identifier: provider.reconcile(
                        {**frozen_request, "invocation_id": identifier}, directory
                    )
                )
                if callable(getattr(provider, "reconcile", None))
                else None,
                validate=validate_output,
            )
        else:
            result = execute(invocation_id or "immediate")
        validate_output(result)
    except InvalidParserResult as error:
        raise RejectedResult(str(error)) from error
    try:
        validate_result(frozen_request, result, directory, identity)
        resolve_reference(directory, frozen_request["source"])
    except (ValueError, TypeError, KeyError, OSError) as error:
        raise RejectedResult(str(error)) from error
    try:
        if execution_snapshot(provider, frozen_request, directory) != selection:
            raise RejectedResult("Selected parser changed during execution")
    except InvalidParserResult as error:
        raise RejectedResult(str(error)) from error
    if provider.identity != identity:
        raise RejectedResult("Parser identity changed during execution")
    if result["thumbnail"] is None:
        thumbnail_provider = LocalThumbnailProvider()
        thumbnail_request = {
            **frozen_request,
            "operation": "thumbnail",
            "output_dir": f"{frozen_request['output_dir']}/thumbnail",
            "settings": configuration_snapshot(thumbnail_provider, {}),
            "allow_remote": False,
        }
        try:
            selection_before = execution_snapshot(
                thumbnail_provider, thumbnail_request, directory
            )

            def validate_thumbnail(thumbnail_result: dict) -> None:
                """Keep auxiliary output and engine freshness checks together."""
                validate_result(
                    thumbnail_request,
                    thumbnail_result,
                    directory,
                    thumbnail_provider.identity,
                )
                if (
                    execution_snapshot(thumbnail_provider, thumbnail_request, directory)
                    != selection_before
                ):
                    raise RejectedResult(
                        "Thumbnail parser identity changed during execution"
                    )

            def create_thumbnail(identifier: str) -> dict:
                """Give the auxiliary attempt its own stable identity."""
                import shutil

                from paperless.parsers.contract import relative_path

                output = relative_path(directory, thumbnail_request["output_dir"])
                if output.exists():
                    shutil.rmtree(output)
                auxiliary_request = json.loads(json.dumps(thumbnail_request))
                auxiliary_request["invocation_id"] = identifier
                return thumbnail_provider.process(auxiliary_request, directory)

            if job_id:
                thumbnail_result = invoke(
                    job,
                    "parser.thumbnail",
                    thumbnail_provider.identity,
                    {"request": thumbnail_request, "selection": selection_before},
                    create_thumbnail,
                    replay_safe=selection_before["replay_safe"],
                    provider=thumbnail_provider,
                    remote_allowed=frozen_request["allow_remote"],
                    validate=validate_thumbnail,
                )
            else:
                thumbnail_result = create_thumbnail(
                    invocation_id or "immediate-thumbnail"
                )
            validate_thumbnail(thumbnail_result)
        except (ValueError, TypeError, KeyError, OSError) as error:
            raise RejectedResult(str(error)) from error
        result["thumbnail"] = thumbnail_result["thumbnail"]
        result["thumbnail_provider"] = selection_before["identity"]
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
    from documents.job_state import RejectedResult
    from documents.job_storage import job_directory

    request = job.result.get("parser_request")
    if request is None:
        return  # Jobs prepared before the provider contract keep their recovery path.
    if job.configuration != effective_configuration(
        mailrule_id=job.input.get("mailrule_id"),
        thumbnail_only=job.input.get("thumbnail_only", False),
    ):
        raise RejectedResult("Processing configuration changed before publication")
    directory = job_directory(job.pk)
    try:
        resolve_reference(directory, request["source"])
    except (ValueError, TypeError, KeyError, OSError) as error:
        raise RejectedResult(str(error)) from error
    cls = LocalThumbnailProvider if job.input.get("thumbnail_only") else parser_class()
    if (
        f"{cls.__module__}.{cls.__qualname__}" != job.provider["class"]
        or cls().identity != job.provider["identity"]
    ):
        raise RejectedResult("Selected parser changed before publication")
    try:
        selection = execution_snapshot(cls(), request, directory)
    except InvalidParserResult as error:
        raise RejectedResult("Selected parser changed before publication") from error
    if selection != job.provider.get("implementation"):
        raise RejectedResult("Selected parser changed before publication")
