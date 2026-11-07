"""Authenticated processor claims and core-owned operation admission."""

import os
from hashlib import sha256
from pathlib import Path
from tempfile import NamedTemporaryFile

from django.core.exceptions import PermissionDenied
from django.http import FileResponse
from django.http import Http404
from drf_spectacular.types import OpenApiTypes
from drf_spectacular.utils import extend_schema
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
        results = []
        for job in jobs.iterator():
            for key, entry in job.result.get("invocations", {}).items():
                if "policy" not in entry:
                    continue
                if status == "pending" and (
                    entry["status"] != "pending"
                    or not entry.get("remote_allowed", True)
                ):
                    continue
                try:
                    operations.authorize(entry, request.user)
                except PermissionDenied:
                    if (
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
                        "status": entry["status"],
                        "job_status": job.status,
                        "error": entry.get("error") or job.error,
                    }
                )
        return Response(results)

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
        with operations.locked_operation(job_id, key, request.user) as (job, entry):
            token = request.headers.get("X-Operation-Token", "")
            operations.check_claim(job, entry, request.user, token)
            upload = request.FILES.get("file")
            if upload is None:
                raise serializers.ValidationError("file is required")
            name = Path(upload.name).name
            directory = job_directory(job_id)
            output_dir = entry["input"].get("request", {}).get("output_dir", "remote")
            destination = relative_path(directory, f"{output_dir}/{token}/{name}")
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                digest = sha256()
                for chunk in upload.chunks():
                    digest.update(chunk)
                reference = file_reference(directory, destination)
                if digest.hexdigest() != reference["sha256"]:
                    raise Conflict("Output already uploaded with different content")
                return Response(reference)
            with NamedTemporaryFile(
                dir=destination.parent, prefix=".upload-", delete=False
            ) as output:
                temporary = Path(output.name)
                try:
                    for chunk in upload.chunks():
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                    temporary.replace(destination)
                    sync_directory(destination.parent)
                finally:
                    temporary.unlink(missing_ok=True)
            return Response(file_reference(directory, destination), status=201)


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
