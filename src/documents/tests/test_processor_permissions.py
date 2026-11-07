"""Processor capabilities remain separate from routing and requester authority."""

from unittest.mock import Mock

import pytest
from django.contrib.auth.models import Group
from django.contrib.auth.models import Permission
from django.core.exceptions import PermissionDenied
from django.core.files.uploadedfile import SimpleUploadedFile
from rest_framework.test import APIClient

from documents import deferred_operations as operations
from documents.job_state import execution_policy
from documents.job_storage import job_directory
from documents.models import DocumentJob
from documents.operation_journal import invoke
from paperless.parsers.contract import file_reference
from paperless_testing.factories import UserFactory

pytestmark = [
    pytest.mark.django_db(transaction=True),
    pytest.mark.usefixtures("paperless_dirs"),
]


def test_group_revocation_blocks_every_processor_endpoint(settings, mocker):
    """Even an existing claim and cached user permissions cannot bypass revocation."""
    mocker.patch("documents.jobs._dispatch")
    user = UserFactory()
    group = Group.objects.create(name="OCR workstation")
    permission = Permission.objects.get(codename="process_parser")
    group.permissions.add(permission)
    user.groups.add(group)
    job = DocumentJob.objects.create(timeout=60)
    source = job_directory(job.pk) / "input.txt"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text("private input")
    inputs = {"source": file_reference(source.parent, source)}
    with pytest.raises(operations.PendingOperation):
        invoke(
            job,
            "parser.process",
            "parser:1",
            inputs,
            Mock(),
        )
    client = APIClient()
    client.force_authenticate(user)
    base = f"/api/operations/{job.pk}/parser.process/"
    lease = client.post(base + "claim/").json()
    token = lease["token"]
    assert user.has_perm("documents.process_parser")
    group.permissions.remove(permission)
    assert client.get("/api/operations/").json() == []
    assert client.post(base + "claim/").status_code == 403
    assert (
        client.get(
            base + "files/", {"path": "input.txt"}, HTTP_X_OPERATION_TOKEN=token
        ).status_code
        == 403
    )
    assert (
        client.post(
            base + "files/",
            {"file": SimpleUploadedFile("out.txt", b"out")},
            HTTP_X_OPERATION_TOKEN=token,
        ).status_code
        == 403
    )
    assert (
        client.post(
            base + "result/", {"token": token, "result": None}, format="json"
        ).status_code
        == 403
    )
    group.permissions.add(permission)
    operations.submit_result(job.pk, "parser.process", user, token, None)
    group.permissions.clear()
    with pytest.raises(PermissionDenied):
        operations.submit_result(job.pk, "parser.process", user, token, None)


@pytest.mark.parametrize("capability", ["parser", "metadata", "search", "ingestion"])
def test_grants_are_capability_scoped(capability):
    """A permission for one kind of work never grants other work or files."""
    user = UserFactory()
    user.user_permissions.add(Permission.objects.get(codename=f"process_{capability}"))
    for other in ("parser", "metadata", "search", "ingestion"):
        policy = execution_policy(f"{other}.run", "provider")
        if other == capability:
            operations.authorize({"policy": policy}, user)
        else:
            with pytest.raises(PermissionDenied):
                operations.authorize({"policy": policy}, user)


def test_legacy_user_policy_does_not_grant_processor_access():
    """Legacy user routing cannot substitute for a capability grant."""
    user = UserFactory()
    user.user_permissions.add(Permission.objects.get(codename="process_parser"))
    with pytest.raises(PermissionDenied):
        operations.authorize({"policy": {"users": [user.pk]}}, user)


def test_waiting_provider_never_runs_on_coordinator():
    """Repeated delivery waits without executing or consuming attempts."""
    job = DocumentJob.objects.create(timeout=60)
    execute = Mock()
    for _ in range(4):
        with pytest.raises(operations.PendingOperation):
            invoke(
                job,
                "metadata",
                "provider:1",
                {},
                execute,
                replay_safe=True,
                operation="metadata.classify",
            )
    execute.assert_not_called()
    job.refresh_from_db()
    assert job.result["invocations"]["metadata"].get("attempts", 0) == 0
    assert job.status == DocumentJob.Status.WAITING


@pytest.mark.parametrize(
    "operation",
    [
        "parser.process",
        "parser.thumbnail",
        "metadata.classify",
        "search.write",
        "search.rebuild",
        "search.optimize",
        "search.wipe",
        "prepare:0",
    ],
)
def test_finite_provider_operations_queue_without_placement(operation):
    """Every supported finite operation requires a processor capability."""
    policy = execution_policy(operation, "provider")
    assert policy is not None
    assert policy["permission"].startswith("documents.process_")


def test_lifecycle_hooks_remain_core_owned():
    """Completion hooks cannot be routed to a generic processor."""
    assert execution_policy("complete:post_consume", "hook") is None


def test_pending_discovery_excludes_history_and_unclaimable_operations():
    """A workstation discovers only authorized unclaimed work without leasing it."""
    user = UserFactory()
    user.user_permissions.add(Permission.objects.get(codename="process_parser"))
    entry = {
        "id": "invocation",
        "operation": "parser.process",
        "status": "pending",
        "policy": {"permission": "documents.process_parser"},
        "input": {"private": "not returned"},
    }
    job = DocumentJob.objects.create(
        timeout=60,
        status=DocumentJob.Status.WAITING,
        result={"invocations": {"parser.process": entry}},
    )
    for state, invocation in [
        (DocumentJob.Status.SUCCEEDED, entry),
        (DocumentJob.Status.CANCELLED, entry),
        (DocumentJob.Status.WAITING, {**entry, "status": "leased"}),
        (DocumentJob.Status.WAITING, {**entry, "remote_allowed": False}),
    ]:
        DocumentJob.objects.create(
            timeout=60, status=state, result={"invocations": {"parser": invocation}}
        )
    client = APIClient()
    client.force_authenticate(user)
    response = client.get("/api/operations/", {"status": "pending"})
    assert response.status_code == 200
    assert [row["job_id"] for row in response.json()] == [str(job.pk)]
    assert "not returned" not in response.content.decode()
    job.refresh_from_db()
    assert job.result["invocations"]["parser.process"] == entry
    assert len(client.get("/api/operations/").json()) == 5
    user.user_permissions.clear()
    assert client.get("/api/operations/", {"status": "pending"}).json() == []
    assert client.get("/api/operations/", {"status": "leased"}).status_code == 400
