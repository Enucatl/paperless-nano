"""Processor capabilities remain separate from routing and requester authority."""

from types import SimpleNamespace
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
    settings.DEFERRED_OPERATIONS = {}
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
            provider=SimpleNamespace(execution_mode="external"),
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
        policy = execution_policy(
            f"{other}.run", "provider", SimpleNamespace(execution_mode="external")
        )
        if other == capability:
            operations.authorize({"policy": policy}, user)
        else:
            with pytest.raises(PermissionDenied):
                operations.authorize({"policy": policy}, user)


@pytest.mark.parametrize("leased", [False, True])
def test_legacy_policy_survives_configuration_migration(settings, mocker, leased):
    """Permission grants and changed config cannot widen a retained user-ID grant."""
    mocker.patch("documents.jobs._dispatch")
    original, newcomer = UserFactory(), UserFactory()
    settings.DEFERRED_OPERATIONS = {"parser.process": {"users": [original.pk]}}
    job = DocumentJob.objects.create(timeout=60)
    with pytest.raises(operations.PendingOperation):
        invoke(job, "parser.process", "parser:1", {}, Mock())
    lease = operations.claim(job.pk, "parser.process", original) if leased else None
    settings.DEFERRED_OPERATIONS = {}
    newcomer.user_permissions.add(Permission.objects.get(codename="process_parser"))
    with pytest.raises(operations.PendingOperation):
        invoke(
            job,
            "parser.process",
            "parser:1",
            {},
            Mock(),
            provider=SimpleNamespace(execution_mode="external"),
        )
    with pytest.raises(PermissionDenied):
        operations.claim(job.pk, "parser.process", newcomer)
    lease = lease or operations.claim(job.pk, "parser.process", original)
    operations.submit_result(job.pk, "parser.process", original, lease["token"], None)
    execute = Mock()
    assert (
        invoke(
            job,
            "parser.process",
            "parser:1",
            {},
            execute,
            provider=SimpleNamespace(execution_mode="local"),
        )
        is None
    )
    execute.assert_not_called()
    job.refresh_from_db()
    assert job.result["invocations"]["parser.process"]["policy"] == {
        "users": [original.pk]
    }


def test_pending_local_invocation_does_not_change_route(settings):
    """An old pending local entry cannot become external on coordinator replay."""
    settings.DEFERRED_OPERATIONS = {}
    job = DocumentJob.objects.create(timeout=60)
    with pytest.raises(operations.PendingOperation):
        invoke(
            job,
            "metadata",
            "provider:1",
            {},
            Mock(side_effect=operations.PendingOperation()),
            replay_safe=True,
        )
    execute = Mock(return_value={"local": True})
    assert invoke(
        job,
        "metadata",
        "provider:1",
        {},
        execute,
        replay_safe=True,
        operation="metadata.classify",
        provider=SimpleNamespace(execution_mode="external"),
    ) == {"local": True}
    execute.assert_called_once()


def test_unknown_execution_mode_fails_closed():
    """A typo cannot silently run external work on the local worker."""
    with pytest.raises(ValueError, match="execution_mode"):
        execution_policy(
            "parser.process", "provider", SimpleNamespace(execution_mode="externl")
        )
