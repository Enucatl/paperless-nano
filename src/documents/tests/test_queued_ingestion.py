"""Ingestion preparation waits without computing in the coordinator."""

import pytest
from celery.exceptions import Ignore

from documents import ingestion_jobs
from documents.models import Document
from documents.models import DocumentJob
from documents.tests.test_ingestion_jobs import incoming as incoming_fixture
from paperless_testing.factories import UserFactory

incoming = incoming_fixture
pytestmark = pytest.mark.django_db(transaction=True)


def test_preflight_is_accepted_before_next_unplaced_stage(incoming, mocker):
    """Ingestion advances placed preparation and leaves unplaced inference pending."""
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
    from django.test import override_settings

    with (
        override_settings(
            PROCESSOR_USER=UserFactory(is_superuser=True).username,
            PROCESSOR_CAPABILITIES=("ingestion",),
        ),
        pytest.raises(Ignore),
    ):
        from documents.deferred_operations import resume

        resume(job)
        ingestion_jobs.run_consumption(job.pk)
    job.refresh_from_db()
    entry = job.result["invocations"]["prepare:0"]
    assert entry["status"] == "succeeded"
    assert entry["attempts"] == 1
    assert not Document.objects.exists()
