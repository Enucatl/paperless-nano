"""Regression checks for requester identity and bounded execution recovery."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from django.contrib.auth.models import Permission
from rest_framework.test import APIClient

from documents.deferred_operations import PendingOperation
from documents.deferred_operations import claim
from documents.ingestion_state import NeedsAttention
from documents.ingestion_state import invoke
from documents.ingestion_state import operation_failure_status
from documents.job_state import RejectedResult
from documents.job_storage import job_directory
from documents.models import DocumentJob
from documents.operations_api import Conflict
from documents.operations_api import OperationFileView
from documents.search_operations import run_search
from documents.search_operations import submit_search
from paperless_testing.factories import UserFactory

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("paperless_dirs"),
]


def test_interrupted_upload_can_retry_the_same_filename(settings):
    """Partial streams never publish an immutable but incomplete output."""
    processor = UserFactory()
    processor.user_permissions.add(Permission.objects.get(codename="process_parser"))
    job = DocumentJob.objects.create(timeout=1800)
    with pytest.raises(PendingOperation):
        invoke(job, "work", "provider", {}, Mock(), operation="parser.process")
    token = claim(job.pk, "work", processor)["token"]

    def interrupted_chunks():
        """Fail after writing part of the submitted payload."""
        yield b"partial"
        raise OSError("upload interrupted")

    upload = SimpleNamespace(name="result.txt", chunks=interrupted_chunks)
    request = SimpleNamespace(
        user=processor,
        headers={"X-Operation-Token": token},
        FILES={"file": upload},
    )
    view = OperationFileView()
    with pytest.raises(OSError, match="interrupted"):
        view.post(request, job.pk, "work")
    output_directory = job_directory(job.pk) / "remote" / token
    assert list(output_directory.iterdir()) == []

    upload.chunks = lambda: iter([b"complete output"])
    response = view.post(request, job.pk, "work")
    assert response.status_code == 201
    assert (output_directory / upload.name).read_bytes() == b"complete output"
    assert view.post(request, job.pk, "work").data == response.data
    upload.chunks = lambda: iter([b"replacement"])
    with pytest.raises(Conflict):
        view.post(request, job.pk, "work")
    assert (output_directory / upload.name).read_bytes() == b"complete output"


def test_ingestion_requester_can_observe_and_cancel(settings):
    """An uploader remains the requester when ingestion nests its overrides."""
    actor = UserFactory()
    processor = UserFactory()
    processor.user_permissions.add(Permission.objects.get(codename="process_parser"))
    job = DocumentJob.objects.create(
        timeout=1800, kind="consume", input={"overrides": {"actor_id": actor.pk}}
    )
    with pytest.raises(PendingOperation):
        invoke(job, "work", "provider", {}, Mock(), operation="parser.process")
    job.refresh_from_db()
    assert job.result["invocations"]["work"]["authorization"] == {"actor_id": actor.pk}
    client = APIClient()
    client.force_authenticate(actor)
    assert client.get("/api/operations/").json()[0]["job_id"] == str(job.pk)
    assert client.post(f"/api/operations/{job.pk}/cancel/").status_code == 204
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.CANCELLED


def test_safe_execution_stops_after_three_actual_attempts(settings):
    """Recovery retries temporary computation failures without infinite execution."""
    job = DocumentJob.objects.create(timeout=1800)
    execute = Mock(side_effect=RuntimeError("temporarily unavailable"))
    for expected in ("queued", "queued", "failed"):
        with pytest.raises(RuntimeError):
            invoke(job, "work", "provider", {}, execute, replay_safe=True)
        job.refresh_from_db()
        assert operation_failure_status(job) == expected
    with pytest.raises(NeedsAttention, match="exhausted"):
        invoke(job, "work", "provider", {}, execute, replay_safe=True)
    assert execute.call_count == 3


def test_invalid_output_is_not_recomputed(settings):
    """A failed domain validator rejects the proposal rather than retrying it."""
    job = DocumentJob.objects.create(timeout=1800)
    execute = Mock(return_value={})
    validate = Mock(side_effect=ValueError("invalid proposal"))
    with pytest.raises(ValueError):
        invoke(
            job, "work", "provider", {}, execute, replay_safe=True, validate=validate
        )
    job.refresh_from_db()
    assert operation_failure_status(job) == "rejected"
    with pytest.raises(RejectedResult):
        invoke(
            job, "work", "provider", {}, execute, replay_safe=True, validate=validate
        )
    assert execute.call_count == 1


def test_nested_wait_does_not_consume_execution_attempt(settings):
    """An orchestration wrapper releases its attempt while a child is offline."""
    job = DocumentJob.objects.create(timeout=1800)
    for _ in range(4):
        with pytest.raises(PendingOperation):
            invoke(
                job,
                "work",
                "provider",
                {},
                Mock(side_effect=PendingOperation()),
                replay_safe=True,
            )
    job.refresh_from_db()
    assert job.result["invocations"]["work"]["attempts"] == 0


def test_search_retries_safe_failures_and_stops_recovering_success(settings, mocker):
    """Search uses the common attempt cap and marks successful cleanup complete."""
    provider = SimpleNamespace(
        identity="test",
        configuration={},
        replay_safe=True,
        optimize=Mock(side_effect=[RuntimeError("busy"), None]),
    )
    mocker.patch(
        "documents.search.get_backend", return_value=SimpleNamespace(provider=provider)
    )
    mocker.patch("documents.search_operations.run_search.apply_async")
    job = submit_search(provider, "optimize", {})
    from documents.server_processor import process

    processor = UserFactory()
    processor.user_permissions.add(Permission.objects.get(codename="process_search"))
    run_search(str(job.pk))
    job.refresh_from_db()
    key = next(iter(job.result["invocations"]))
    with pytest.raises(RuntimeError):
        process(job.pk, key, processor)
    job.refresh_from_db()
    assert job.result["invocations"][key]["attempts"] == 1
    from datetime import timedelta

    from django.utils import timezone

    from documents.deferred_operations import recover_waiting

    job.result["invocations"][key]["lease_until"] = (
        timezone.now() - timedelta(seconds=1)
    ).isoformat()
    job.save(update_fields=["result"])
    recover_waiting()
    process(job.pk, key, processor)
    run_search(str(job.pk))
    job.refresh_from_db()
    assert job.status == "succeeded"
    assert job.cleaned
    assert provider.optimize.call_count == 2


def test_task_operation_ignores_non_uuid_task_ids():
    """Ordinary task identifiers do not break the Tasks API."""
    from documents.serialisers import TaskSerializerV10

    assert (
        TaskSerializerV10().get_operation(SimpleNamespace(task_id="scheduled-task"))
        is None
    )


def test_task_operation_hides_claim_credentials():
    """The task summary exposes only the requested operation and its state."""
    from documents.serialisers import TaskSerializerV10

    job = DocumentJob.objects.create(
        timeout=60,
        result={
            "invocations": {
                "parse": {
                    "policy": {"permission": "documents.process_parser"},
                    "operation": "parser.process",
                    "status": "leased",
                    "token": "secret",
                }
            }
        },
    )
    assert TaskSerializerV10().get_operation(SimpleNamespace(task_id=str(job.pk))) == {
        "name": "parser.process",
        "status": "leased",
        "error": "",
    }


def test_task_operation_summaries_use_one_job_query(django_assert_num_queries):
    """A Tasks page batches operation journals rather than querying each row."""
    from rest_framework.serializers import ListSerializer

    from documents.serialisers import TaskSerializerV10

    jobs = [DocumentJob.objects.create(timeout=60) for _ in range(3)]
    tasks = [SimpleNamespace(task_id=str(job.pk)) for job in jobs]
    serializer = TaskSerializerV10()
    ListSerializer(child=serializer, instance=tasks)
    with django_assert_num_queries(1):
        assert [serializer.get_operation(task) for task in tasks] == [None] * 3
