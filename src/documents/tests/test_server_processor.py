"""Server processors submit the same durable results as remote processors."""

from types import SimpleNamespace

import pytest

from documents.models import DocumentJob
from documents.search_operations import run_search
from documents.search_operations import submit_search
from paperless_testing.factories import UserFactory
from paperless_testing.queued import drive_processors

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("paperless_dirs"),
]


def test_server_accepts_result_in_same_task(mocker):
    """Computation happens once; authoritative completion follows submission."""
    provider = SimpleNamespace(identity="search:1", configuration={})
    mocker.patch(
        "documents.search.get_backend", return_value=SimpleNamespace(provider=provider)
    )
    mocker.patch("documents.search_operations.run_search.apply_async")
    mocker.patch("documents.jobs._dispatch")
    execute = mocker.patch("documents.search_operations._execute", return_value=None)
    job = submit_search(provider, "wipe", {})
    run_search(str(job.pk))
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.WAITING
    execute.assert_not_called()
    processor = UserFactory(is_superuser=True)
    drive_processors(run_search, processor)(str(job.pk))
    job.refresh_from_db()
    assert job.result["invocations"]["maintenance"]["status"] == "succeeded"
    assert job.result["invocations"]["maintenance"]["processor_location"] == "server"
    drive_processors(run_search, processor)(str(job.pk))
    execute.assert_called_once()
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.SUCCEEDED


def test_cancelled_work_never_executes_on_server(mocker):
    """Cancellation remains authoritative when a server later sees the job."""
    from documents.deferred_operations import cancel

    provider = SimpleNamespace(identity="search:1", configuration={})
    mocker.patch(
        "documents.search.get_backend", return_value=SimpleNamespace(provider=provider)
    )
    mocker.patch("documents.search_operations.run_search.apply_async")
    execute = mocker.patch("documents.search_operations._execute")
    job = submit_search(provider, "wipe", {})
    run_search(str(job.pk))
    processor = UserFactory(is_superuser=True)
    cancel(job.pk, processor)
    drive_processors(run_search, processor)(str(job.pk))
    execute.assert_not_called()
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.CANCELLED


@pytest.mark.parametrize(
    "status",
    [DocumentJob.Status.WAITING, DocumentJob.Status.RUNNING, DocumentJob.Status.QUEUED],
)
def test_server_can_resume_expired_safe_claim(mocker, status):
    """An offline safe processor leaves bounded work available to the server."""
    from datetime import timedelta

    from django.utils import timezone

    from documents.deferred_operations import claim
    from documents.deferred_operations import recover_waiting

    provider = SimpleNamespace(identity="search:1", configuration={}, replay_safe=True)
    mocker.patch(
        "documents.search.get_backend", return_value=SimpleNamespace(provider=provider)
    )
    mocker.patch("documents.search_operations.run_search.apply_async")
    mocker.patch("documents.jobs._dispatch")
    execute = mocker.patch("documents.search_operations._execute", return_value=None)
    job = submit_search(provider, "optimize", {})
    run_search(str(job.pk))
    processor = UserFactory(is_superuser=True)
    claim(job.pk, "maintenance", processor)
    job.refresh_from_db()
    job.result["invocations"]["maintenance"]["lease_until"] = (
        timezone.now() - timedelta(seconds=1)
    ).isoformat()
    job.status = status
    job.save(update_fields=["result", "status"])
    recover_waiting()
    drive_processors(run_search, processor)(str(job.pk))
    job.refresh_from_db()
    assert job.result["invocations"]["maintenance"]["status"] == "succeeded"
    assert job.result["invocations"]["maintenance"]["attempts"] == 2
    execute.assert_called_once()


def test_upgrade_quarantines_unfinished_outcomes():
    """An upgrade preserves evidence without silently claiming legacy work."""
    from importlib import import_module

    from django.apps import apps

    pending = DocumentJob.objects.create(
        kind="search",
        timeout=30,
        status=DocumentJob.Status.WAITING,
        result={"invocations": {"work": {"status": "leased", "token": "retained"}}},
    )
    done = DocumentJob.objects.create(
        kind="search",
        timeout=30,
        status=DocumentJob.Status.SUCCEEDED,
        result={"receipt": "preserved"},
    )
    import_module("documents.migrations.0031_queued_operations").quarantine_legacy(
        apps, None
    )
    pending.refresh_from_db()
    done.refresh_from_db()
    assert pending.status == DocumentJob.Status.ATTENTION
    assert pending.result["invocations"]["work"]["token"] == "retained"
    assert done.status == DocumentJob.Status.SUCCEEDED
    assert done.result == {"receipt": "preserved"}


def test_server_preserves_rejected_configuration_before_claim(mocker):
    """A changed provider remains reviewable instead of returning to the queue."""
    from documents.operation_journal import NeedsAttention

    provider = SimpleNamespace(identity="search:1", configuration={})
    mocker.patch(
        "documents.search.get_backend", return_value=SimpleNamespace(provider=provider)
    )
    mocker.patch("documents.search_operations.run_search.apply_async")
    execute = mocker.patch("documents.search_operations._execute")
    job = submit_search(provider, "wipe", {})
    run_search(str(job.pk))
    provider.configuration = {"changed": True}
    with pytest.raises(NeedsAttention):
        drive_processors(run_search, UserFactory(is_superuser=True))(str(job.pk))
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.ATTENTION
    assert job.result["invocations"]["maintenance"].get("attempts", 0) == 0
    execute.assert_not_called()


def test_capability_account_provisioning_is_idempotent():
    """The service gets no document rights and cannot take over login users."""
    from django.contrib.auth.models import User
    from django.core.management import call_command
    from django.core.management.base import CommandError

    for _ in range(2):
        call_command(
            "document_processor_account",
            "--capability",
            "parser",
            "--capability",
            "search",
            user="processor",
        )
    processor = User.objects.get(username="processor")
    assert not processor.has_usable_password()
    assert processor.get_all_permissions() == {
        "documents.process_parser",
        "documents.process_search",
    }
    login = UserFactory()
    login.set_password("test-only")
    login.save()
    with pytest.raises(CommandError, match="dedicated"):
        call_command(
            "document_processor_account", user=login.username, capability=["parser"]
        )


def test_tasks_exposes_retained_operation_failure():
    """A retry waiting on an endpoint retains an actionable explanation."""
    from documents.serialisers import TaskSerializerV10
    from paperless_testing.factories import PaperlessTaskFactory

    job = DocumentJob.objects.create(
        kind="search",
        timeout=30,
        status=DocumentJob.Status.WAITING,
        error="Endpoint unavailable; retry pending",
        result={
            "invocations": {
                "work": {
                    "operation": "search.write",
                    "status": "pending",
                    "error": "",
                    "policy": {"permission": "documents.process_search"},
                }
            }
        },
    )
    task = PaperlessTaskFactory(task_id=str(job.pk))
    assert TaskSerializerV10(task).get_operation(task) == {
        "name": "search.write",
        "status": "pending",
        "error": job.error,
    }


def test_local_thumbnail_runs_while_ocr_waits_for_workstation(settings, mocker):
    """Removing server OCR placement must not strand the core thumbnail fallback."""
    from django.contrib.auth.models import Permission

    from documents.job_state import PendingOperation
    from documents.operation_journal import invoke

    processor = UserFactory()
    processor.user_permissions.add(Permission.objects.get(codename="process_ingestion"))
    settings.PROCESSOR_USER = processor.username
    settings.PROCESSOR_CAPABILITIES = ["ingestion"]
    mocker.patch("documents.jobs._dispatch")
    thumbnail = mocker.Mock(return_value={"thumbnail": "private rendition"})
    thumbnail_job = DocumentJob.objects.create(timeout=60)
    assert invoke(
        thumbnail_job,
        "parser.thumbnail",
        "paperless.local-thumbnail:1",
        {},
        thumbnail,
        replay_safe=True,
        operation="parser.thumbnail",
    ) == {"thumbnail": "private rendition"}
    thumbnail.assert_called_once()
    thumbnail_job.refresh_from_db()
    entry = thumbnail_job.result["invocations"]["parser.thumbnail"]
    assert entry["policy"] == {"permission": "documents.process_ingestion"}
    assert entry["processor_location"] == "server"
    assert entry["status"] == "succeeded"

    ocr = mocker.Mock()
    ocr_job = DocumentJob.objects.create(timeout=60)
    with pytest.raises(PendingOperation):
        invoke(
            ocr_job,
            "parser.process",
            "paperless-inference.paddle:1",
            {},
            ocr,
            replay_safe=True,
            operation="parser.process",
        )
    ocr.assert_not_called()
    ocr_job.refresh_from_db()
    assert ocr_job.result["invocations"]["parser.process"].get("attempts", 0) == 0

    processor.user_permissions.clear()
    forbidden = DocumentJob.objects.create(timeout=60)
    with pytest.raises(PendingOperation):
        invoke(
            forbidden,
            "parser.thumbnail",
            "paperless.local-thumbnail:1",
            {},
            thumbnail,
            replay_safe=True,
            operation="parser.thumbnail",
        )
    assert thumbnail.call_count == 1
    forbidden.refresh_from_db()
    assert forbidden.result["invocations"]["parser.thumbnail"].get("attempts", 0) == 0


def test_retained_thumbnail_policy_still_requires_original_grant(settings):
    """Changing placement must not rewrite authorization on retained operations."""
    from django.contrib.auth.models import Permission

    from documents.server_processor import selected_user

    processor = UserFactory()
    processor.user_permissions.add(Permission.objects.get(codename="process_ingestion"))
    settings.PROCESSOR_USER = processor.username
    settings.PROCESSOR_CAPABILITIES = ["ingestion"]
    entry = {
        "operation": "parser.thumbnail",
        "policy": {"permission": "documents.process_parser"},
    }
    assert selected_user(entry) is None
    processor.user_permissions.add(Permission.objects.get(codename="process_parser"))
    assert selected_user(entry) == processor
    assert entry["policy"] == {"permission": "documents.process_parser"}
