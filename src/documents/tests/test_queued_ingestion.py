"""Ingestion preparation waits without computing in the coordinator."""

import pytest
from celery.exceptions import Ignore

from documents import ingestion_jobs
from documents.models import Document
from documents.models import DocumentJob
from documents.server_processor import process
from documents.tests.test_ingestion_jobs import incoming as incoming_fixture
from paperless_testing.factories import UserFactory

incoming = incoming_fixture
pytestmark = pytest.mark.django_db(transaction=True)


def test_preflight_is_claimed_separately_before_result_validation(incoming, mocker):
    """Local-only inputs still queue and a processor only stages their outcome."""
    mocker.patch("documents.jobs._dispatch")
    job = ingestion_jobs.stage_consumption(incoming)
    with pytest.raises(Ignore):
        ingestion_jobs.run_consumption(job.pk)
    job.refresh_from_db()
    entry = job.result["invocations"]["prepare:0"]
    assert job.status == DocumentJob.Status.WAITING
    assert entry["status"] == "pending"
    assert entry.get("attempts", 0) == 0
    assert not Document.objects.exists()
    process(job.pk, "prepare:0", UserFactory(is_superuser=True))
    job.refresh_from_db()
    entry = job.result["invocations"]["prepare:0"]
    assert entry["status"] == "staged"
    assert entry["attempts"] == 1
    assert not Document.objects.exists()
