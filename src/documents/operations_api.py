"""Authenticated processor claims and core-owned operation admission."""

import os
from base64 import urlsafe_b64encode
from hashlib import sha256
from uuid import UUID

from cryptography.fernet import Fernet
from cryptography.fernet import InvalidToken
from django.core.exceptions import PermissionDenied
from django.http import FileResponse
from django.http import Http404
from django.utils.crypto import salted_hmac
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
from filelock import FileLock
from filelock import Timeout
from rest_framework import serializers
from rest_framework.exceptions import APIException
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from documents import deferred_operations as operations
from documents.job_state import RejectedResult
from documents.job_storage import job_directory
from documents.job_storage import sync_directory
from documents.models import Document
from documents.models import DocumentJob
from paperless.parsers.contract import file_reference
from paperless.parsers.contract import relative_path

MAX_CLAIM_OUTPUT_BYTES = 256 * 1024 * 1024
DISCOVERY_PAGE_JOBS = 50
MULTIPART_OVERHEAD_BYTES = 64 * 1024


class Conflict(APIException):
    """A competing claim or immutable input prevents this state transition."""

    status_code = 409


class SubmissionSerializer(serializers.Serializer):
    """Admit a concrete document once using a caller-owned stable request ID."""

    request_id = serializers.UUIDField()
    document_id = serializers.IntegerField(min_value=1)
    operation = serializers.ChoiceField(choices=["ocr", "metadata", "ocr_metadata"])


class OperationAPIView(APIView):
    """Use the normal API authentication and explicit persisted processor grants."""

    permission_classes = [IsAuthenticated]

    def handle_exception(self, exc):
        """Translate lifecycle conflicts without exposing internal tracebacks."""
        if isinstance(exc, (operations.OperationConflict, RejectedResult, Timeout)):
            exc = Conflict(str(exc))
        elif isinstance(exc, (Document.DoesNotExist, DocumentJob.DoesNotExist)):
            exc = Http404()
        return super().handle_exception(exc)


class OperationsView(OperationAPIView):
    """List authorized pending work and submit optional document enrichment."""

    @extend_schema(responses={200: {"type": "array", "items": {"type": "object"}}})
    def get(self, request):
        """Expose operation status without leaking other processors' inputs."""
        status = request.query_params.get("status")
        if status not in (None, "pending"):
            raise serializers.ValidationError({"status": "Only pending is supported."})
        jobs = DocumentJob.objects.all()
        if status == "pending":
            jobs = jobs.filter(status=DocumentJob.Status.WAITING)
        cursor = request.query_params.get("cursor")
        next_cursor = None
        if cursor is not None:
            if status != "pending":
                raise serializers.ValidationError(
                    {"cursor": "Requires pending status."}
                )
            # A page can end on a job the processor has no permission to see.
            cipher = Fernet(
                urlsafe_b64encode(
                    salted_hmac(
                        "documents.pending.cursor", b"", algorithm="sha256"
                    ).digest()
                )
            )
            if cursor != "start":
                try:
                    after = UUID(bytes=cipher.decrypt(cursor.encode()))
                except (InvalidToken, ValueError) as exc:
                    raise serializers.ValidationError(
                        {"cursor": "Invalid cursor."}
                    ) from exc
                jobs = jobs.filter(pk__gt=after)
            jobs = jobs.only("status", "result", "error").order_by("pk")
            page = list(jobs[: DISCOVERY_PAGE_JOBS + 1])
            if len(page) > DISCOVERY_PAGE_JOBS:
                page.pop()
                next_cursor = cipher.encrypt(page[-1].pk.bytes).decode()
            jobs = page
        else:
            jobs = jobs.iterator()
        actor = operations.active_processor(request.user)
        results = []
        for job in jobs:
            for key, entry in job.result.get("invocations", {}).items():
                if "policy" not in entry:
                    continue
                if status == "pending" and (
                    entry["status"] != "pending"
                    or not entry.get("remote_allowed", True)
                ):
                    continue
                if not operations.may_process(entry, actor) and (
                    status == "pending"
                    or operations.requester_id(job) != request.user.pk
                ):
                    continue
                results.append(
                    {
                        "job_id": str(job.pk),
                        "key": key,
                        "id": entry["id"],
                        "operation": entry["operation"],
                        "contract": entry.get("plugin")
                        if entry["policy"].get("external_only")
                        else None,
                        "status": entry["status"],
                        "job_status": job.status,
                        "error": entry.get("error") or job.error,
                    }
                )
        headers = {"X-Next-Cursor": next_cursor} if next_cursor else {}
        return Response(results, headers=headers)

    @extend_schema(request=SubmissionSerializer, responses={202: OpenApiTypes.OBJECT})
    def post(self, request):
        """Queue OCR, metadata, or their durable chain without waiting for a host."""
        from documents.deferred_operations import resume
        from documents.enrichment_jobs import submit_enrichment
        from documents.jobs import stage_reprocess

        serializer = SubmissionSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        values = serializer.validated_data
        if values["operation"] == "metadata":
            job = submit_enrichment(
                values["document_id"],
                actor_id=request.user.pk,
                job_id=values["request_id"],
            )
        else:
            job = stage_reprocess(
                values["document_id"],
                actor_id=request.user.pk,
                job_id=str(values["request_id"]),
                remote_ocr=True,
                metadata=values["operation"] == "ocr_metadata",
            )
            if job.status == DocumentJob.Status.QUEUED:
                resume(job)
        return Response({"job_id": str(job.pk), "status": job.status}, status=202)


class OperationClaimView(OperationAPIView):
    """Lease one durable operation for a bounded execution attempt."""

    @extend_schema(request=None, responses=OpenApiTypes.OBJECT)
    def post(self, request, job_id, key):
        """Return immutable computation input and an opaque attempt token."""
        return Response(operations.claim(job_id, key, request.user))


class OperationResultView(OperationAPIView):
    """Stage processor proposals for domain validation by the core."""

    @extend_schema(request=OpenApiTypes.OBJECT, responses={202: OpenApiTypes.OBJECT})
    def post(self, request, job_id, key):
        """Accept a matching claim's result idempotently."""
        token = request.data.get("token")
        if not isinstance(token, str) or "result" not in request.data:
            raise serializers.ValidationError("token and result are required")
        return Response(
            operations.submit_result(
                job_id, key, request.user, token, request.data["result"]
            ),
            status=202,
        )


def input_references(value) -> set[str]:
    """Enumerate only explicit immutable file references from accepted inputs."""
    if isinstance(value, dict):
        found = (
            {value["path"]}
            if isinstance(value.get("path"), str) and "sha256" in value
            else set()
        )
        for item in value.values():
            found.update(input_references(item))
        return found
    if isinstance(value, list):
        return set().union(*(input_references(item) for item in value))
    return set()


class OperationFileView(OperationAPIView):
    """Transport contained immutable inputs and privately staged output files."""

    @extend_schema(responses={200: OpenApiTypes.BINARY})
    def get(self, request, job_id, key):
        """Read only this claim's declared input, never arbitrary job storage."""
        with operations.locked_operation(job_id, key, request.user) as (job, entry):
            operations.check_claim(
                job, entry, request.user, request.headers.get("X-Operation-Token", "")
            )
            name = request.query_params.get("path", "")
            if name not in input_references(entry["input"]):
                raise PermissionDenied("File is not an input of this operation")
            path = relative_path(job_directory(job_id), name)
            return FileResponse(path.open("rb"), as_attachment=True, filename=path.name)

    @extend_schema(
        request={
            "multipart/form-data": {
                "type": "object",
                "properties": {"file": {"type": "string", "format": "binary"}},
                "required": ["file"],
            },
        },
        responses={200: OpenApiTypes.OBJECT, 201: OpenApiTypes.OBJECT},
    )
    def post(self, request, job_id, key):
        """Store one output under the claim's private namespace without applying it."""
        import magic

        token = request.headers.get("X-Operation-Token", "")
        directory = job_directory(job_id)
        with operations.locked_operation(job_id, key, request.user) as (job, entry):
            operations.check_claim(job, entry, request.user, token)
            operation = entry["operation"]
            if operation not in {"parser.process", "parser.thumbnail"}:
                raise serializers.ValidationError("This operation has no file outputs")
            parser_request = entry["input"].get("request", {})
            allowed_types = {"image/webp"}
            if (
                operation == "parser.process"
                and parser_request.get("archive_policy") != "never"
            ):
                allowed_types.add("application/pdf")
            output_dir = parser_request.get("output_dir", "remote")
            claim_dir = relative_path(directory, f"{output_dir}/{token}")
            claim_dir.mkdir(parents=True, exist_ok=True)

        # Serialize only this claim's transfers while the job remains cancellable.
        with FileLock(directory / f".upload-{token}.lock", timeout=0):
            if (
                int(request.META.get("CONTENT_LENGTH") or 0)
                > MAX_CLAIM_OUTPUT_BYTES + MULTIPART_OVERHEAD_BYTES
            ):
                raise serializers.ValidationError("Upload exceeds the claim byte limit")
            files = request.FILES
            if set(files) != {"file"} or len(files.getlist("file")) != 1:
                raise serializers.ValidationError("Exactly one file is required")
            upload = files["file"]
            destination = relative_path(claim_dir, upload.name)
            if destination.parent != claim_dir:
                raise serializers.ValidationError("Unexpected parser output filename")
            temporary = directory / f".upload-{token}.part"
            others = [p for p in claim_dir.iterdir() if p != destination]
            if len(others) >= len(allowed_types):
                raise serializers.ValidationError("Claim output count exceeded")
            available = MAX_CLAIM_OUTPUT_BYTES - sum(p.stat().st_size for p in others)
            existing = (
                file_reference(directory, destination) if destination.exists() else None
            )
            digest = sha256()
            size = 0
            try:
                with temporary.open("wb") as output:
                    for chunk in upload.chunks():
                        size += len(chunk)
                        if size > available:
                            raise serializers.ValidationError(
                                "Upload exceeds the claim byte limit"
                            )
                        digest.update(chunk)
                        if existing is None:
                            output.write(chunk)
                    if existing is None:
                        output.flush()
                        os.fsync(output.fileno())
                reference = {
                    "path": str(destination.relative_to(directory)),
                    "size": size,
                    "sha256": digest.hexdigest(),
                }
                if existing is not None and existing != reference:
                    raise Conflict("Output already uploaded with different content")
                media_type = magic.from_file(
                    destination if existing else temporary, mime=True
                )
                if media_type not in allowed_types:
                    raise serializers.ValidationError(
                        "Unexpected parser output media type"
                    )
                if any(
                    magic.from_file(path, mime=True) == media_type for path in others
                ):
                    raise serializers.ValidationError("Claim output count exceeded")
                with operations.locked_operation(job_id, key, request.user) as (
                    job,
                    entry,
                ):
                    operations.check_claim(job, entry, request.user, token)
                    if existing is None:
                        temporary.replace(destination)
                    sync_directory(claim_dir)
                return Response(reference, status=201 if existing is None else 200)
            finally:
                temporary.unlink(missing_ok=True)


class OperationCancelView(OperationAPIView):
    """Cancel queued work while retaining inputs and any uncertain effects."""

    @extend_schema(request=None, responses={204: None})
    def post(self, request, job_id):
        """Invalidate outstanding claims without changing published documents."""
        operations.cancel(job_id, request.user)
        return Response(status=204)


class OperationReconcileView(OperationAPIView):
    """Record verified effects after a processor disappears during execution."""

    @extend_schema(request=OpenApiTypes.OBJECT, responses={202: OpenApiTypes.OBJECT})
    def post(self, request, job_id, key):
        """Require administrator evidence and stage the result for core validation."""
        if "result" not in request.data:
            raise serializers.ValidationError("result is required")
        return Response(
            operations.reconcile(
                job_id,
                key,
                request.user,
                request.data["result"],
                request.data.get("evidence"),
            ),
            status=202,
        )
