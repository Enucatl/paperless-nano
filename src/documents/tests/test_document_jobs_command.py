"""Manual recovery never repeats uncertain ingestion or hides retained files."""

from __future__ import annotations

import json
from io import StringIO
from typing import TYPE_CHECKING

import pytest
from django.core.management import CommandError
from django.core.management import call_command

from documents.jobs import job_directory
from documents.models import DocumentJob

if TYPE_CHECKING:
    from pytest_mock import MockerFixture

    from paperless_testing.dirs import PaperlessDirs

pytestmark = pytest.mark.django_db


def test_inspect_reports_publication_and_retained_directory(
    paperless_dirs: PaperlessDirs,
) -> None:
    """Operators can find recoverable input and publication evidence without mutation."""
    job = DocumentJob.objects.create(
        kind="consume",
        status=DocumentJob.Status.ATTENTION,
        timeout=60,
        error="Worker stopped after publication",
    )
    directory = job_directory(job.pk)
    directory.mkdir()
    original = directory / "input"
    original.write_bytes(b"recoverable")
    publication = [{"target": "/media/original.pdf", "checksum": "known"}]
    (directory / "publication.json").write_text(json.dumps(publication))
    stdout = StringIO()

    call_command("document_jobs", job=str(job.pk), stdout=stdout)

    report = json.loads(stdout.getvalue())
    assert report["status"] == "attention"
    assert report["error"] == job.error
    assert report["staging_directory"] == str(directory)
    assert report["publication"] == publication
    assert not report["dispatched"]
    assert original.read_bytes() == b"recoverable"


@pytest.mark.parametrize("acknowledge", [False, True])
def test_uncommitted_ingestion_cannot_be_replayed(
    paperless_dirs: PaperlessDirs,
    mocker: MockerFixture,
    *,
    acknowledge: bool,
) -> None:
    """Acknowledgment cannot turn an ambiguous pipeline into a second ingestion."""
    job = DocumentJob.objects.create(
        kind="consume",
        status=DocumentJob.Status.ATTENTION,
        timeout=60,
    )
    dispatch = mocker.patch("documents.ingestion_jobs.dispatch_consumption")

    with pytest.raises(CommandError, match="cannot be replayed safely"):
        call_command(
            "document_jobs",
            job=str(job.pk),
            resume=True,
            acknowledge_effects=acknowledge,
        )

    dispatch.assert_not_called()
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.ATTENTION


def test_committed_uncertain_effects_need_explicit_acknowledgment(
    paperless_dirs: PaperlessDirs,
    mocker: MockerFixture,
) -> None:
    """Confirmed external effects are skipped while safe completion is redispatched."""
    job = DocumentJob.objects.create(
        kind="consume",
        status=DocumentJob.Status.ATTENTION,
        timeout=60,
        result={
            "payload": {"document_id": 42},
            "post_consume": "started",
            "delivery": "started",
            "followup": ["index", "cleanup"],
        },
    )
    dispatch = mocker.patch("documents.ingestion_jobs.dispatch_consumption")
    with pytest.raises(CommandError, match="requires --acknowledge-effects"):
        call_command("document_jobs", job=str(job.pk), resume=True)
    dispatch.assert_not_called()

    call_command(
        "document_jobs",
        job=str(job.pk),
        resume=True,
        acknowledge_effects=True,
        stdout=StringIO(),
    )

    dispatch.assert_called_once()
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.COMMITTED
    assert job.result["post_consume"] == "done"
    assert job.result["delivery"] == "done"
    assert job.result["callbacks_acknowledged"] is True
    assert job.result["followup"] == ["index", "cleanup"]


@pytest.mark.parametrize("status", ["queued", "prepared", "committed", "succeeded"])
def test_safe_reprocessing_states_are_redispatched(
    paperless_dirs: PaperlessDirs,
    mocker: MockerFixture,
    status: str,
) -> None:
    """Safe retries keep their job identifier and original input/result journal."""
    job = DocumentJob.objects.create(status=status, timeout=60)
    dispatch = mocker.patch("documents.jobs._dispatch")

    call_command("document_jobs", job=str(job.pk), resume=True, stdout=StringIO())

    dispatch.assert_called_once()
    assert dispatch.call_args.args[0].pk == job.pk
    job.refresh_from_db()
    assert job.status == status
