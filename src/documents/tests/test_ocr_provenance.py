"""OCR audit publication follows document permissions and durable commit ownership."""

import json

import pytest

from documents import jobs
from documents.job_state import RejectedResult
from documents.models import CustomField
from documents.models import CustomFieldInstance
from documents.models import DocumentJob
from documents.ocr_provenance import field_snapshot
from documents.ocr_provenance import publish_provenance
from documents.tests.test_parser_operations import local_processors  # noqa: F401
from documents.tests.test_parser_operations import provider  # noqa: F401
from documents.tests.test_rich_enrichment_jobs import rich  # noqa: F401

pytestmark = pytest.mark.django_db(transaction=True)


def test_reprocess_publishes_provenance_and_preserves_other_fields(
    sample_doc,
    provider,  # noqa: F811
    settings,
):
    """Text and source-bound provenance commit once, retaining unrelated values."""
    field = CustomField.objects.create(name="ai_ocr_output", data_type="longtext")
    unrelated = CustomField.objects.create(name="User notes", data_type="string")
    CustomFieldInstance.objects.create(
        document=sample_doc, field=unrelated, value_text="keep"
    )
    settings.OCR_PROVENANCE_FIELD = str(field.pk)

    def result(request, root):
        """Supply provenance bound to the same retained source as the parser result."""
        return {
            **provider.output(request, root),
            "provenance": {
                "schema_version": 1,
                "source_sha256": request["source"]["sha256"],
                "page_count": 1,
                "pipeline": "test",
                "model": "model:1",
                "pages": [],
            },
        }

    provider.process.side_effect = result
    job = jobs.stage_reprocess(sample_doc.pk)
    assert jobs.run_reprocess(job.pk)
    sample_doc.refresh_from_db()
    value = json.loads(sample_doc.custom_fields.get(field=field).value)
    assert value["model"] == "model:1"
    assert sample_doc.custom_fields.get(field=unrelated).value == "keep"
    assert sample_doc.content == "replacement provider content"
    assert jobs.run_reprocess(job.pk)
    assert sample_doc.custom_fields.filter(field=field).count() == 1


def test_provenance_requires_configured_separate_longtext(settings):
    """An invalid schema or shared metadata target cannot silently drop the audit."""
    field = CustomField.objects.create(name="short", data_type="string")
    settings.OCR_PROVENANCE_FIELD = field.pk
    with pytest.raises(RejectedResult, match="longtext"):
        field_snapshot()
    field.data_type = "longtext"
    field.save()
    settings.ENRICHMENT_FIELDS = {"audit": field.pk}
    with pytest.raises(RejectedResult, match="distinct"):
        field_snapshot()


def test_unconfigured_provenance_rejects_before_publication(
    sample_doc,
    provider,  # noqa: F811
    settings,
):
    """A missing audit destination cannot silently discard a successful OCR receipt."""
    settings.OCR_PROVENANCE_FIELD = None
    original = sample_doc.content
    provider.process.side_effect = lambda request, root: {
        **provider.output(request, root),
        "provenance": {
            "schema_version": 1,
            "page_count": 1,
            "source_sha256": request["source"]["sha256"],
        },
    }
    job = jobs.stage_reprocess(sample_doc.pk)
    with pytest.raises(RejectedResult, match="provenance field"):
        jobs.run_reprocess(job.pk)
    sample_doc.refresh_from_db()
    assert sample_doc.content == original


def test_ingestion_commits_provenance(provider, settings, paperless_dirs):  # noqa: F811
    """New document publication retains its OCR audit alongside its content."""
    from documents import ingestion_jobs
    from documents.data_models import ConsumableDocument
    from documents.data_models import DocumentSource
    from documents.models import Document

    field = CustomField.objects.create(name="ai_ocr_output", data_type="longtext")
    settings.OCR_PROVENANCE_FIELD = field.pk
    source = paperless_dirs.scratch_dir / "input.txt"
    source.write_text("Invoice source")
    provider.process.side_effect = lambda request, root: {
        **provider.output(request, root),
        "provenance": {
            "schema_version": 1,
            "page_count": 1,
            "source_sha256": request["source"]["sha256"],
            "model": "model:1",
        },
    }
    job = ingestion_jobs.stage_consumption(
        ConsumableDocument(DocumentSource.ApiUpload, original_file=source)
    )
    result = ingestion_jobs.run_consumption(job.pk)
    document = Document.objects.get(pk=result["document_id"])
    assert (
        json.loads(document.custom_fields.get(field=field).value)["model"] == "model:1"
    )


def test_changed_field_rejects_publication(sample_doc, settings):
    """A retained result cannot follow a configuration swap to a different field."""
    field = CustomField.objects.create(name="audit", data_type="longtext")
    settings.OCR_PROVENANCE_FIELD = field.pk
    job = DocumentJob(
        kind="reprocess",
        timeout=30,
        configuration={
            "provenance_field": {"id": field.pk + 1, "data_type": "longtext"}
        },
        result={"parser_result": {"provenance": {"schema_version": 1}}},
        input={"actor_id": None},
    )
    with pytest.raises(RejectedResult, match="changed"):
        publish_provenance(job, sample_doc)
    assert not sample_doc.custom_fields.filter(field=field).exists()


def test_revoked_actor_cannot_publish(sample_doc, settings, django_user_model):
    """A provider receipt cannot grant its original requester document-write access."""
    actor = django_user_model.objects.create_user(username="no-access")
    field = CustomField.objects.create(name="audit", data_type="longtext")
    settings.OCR_PROVENANCE_FIELD = field.pk
    job = DocumentJob(
        kind="reprocess",
        timeout=30,
        configuration={"provenance_field": field_snapshot()},
        result={"parser_result": {"provenance": {"schema_version": 1}}},
        input={"actor_id": actor.pk},
    )
    with pytest.raises(RejectedResult, match="no longer change"):
        publish_provenance(job, sample_doc)
    assert not sample_doc.custom_fields.filter(field=field).exists()


@pytest.mark.parametrize("invalid_schema", ["missing", "type"])
@pytest.mark.parametrize("concurrent_edit", [False, True])
def test_metadata_admission_failure_keeps_committed_ocr(
    sample_doc,
    provider,  # noqa: F811
    rich,  # noqa: F811
    settings,
    invalid_schema,
    concurrent_edit,
):
    """Schema repair resumes metadata admission without repeating successful OCR."""
    field = CustomField.objects.create(name="ocr audit", data_type="longtext")
    settings.OCR_PROVENANCE_FIELD = field.pk
    provider.process.side_effect = lambda request, root: {
        **provider.output(request, root),
        "provenance": {
            "schema_version": 1,
            "page_count": 1,
            "source_sha256": request["source"]["sha256"],
        },
    }
    job = jobs.stage_reprocess(sample_doc.pk, metadata=True)
    summary = CustomField.objects.get(pk=settings.ENRICHMENT_FIELDS["summary"])
    summary_id = summary.pk
    if invalid_schema == "missing":
        summary.delete()
    else:
        summary.data_type = "boolean"
        summary.save()
    with pytest.raises(RejectedResult, match="OCR committed; metadata admission"):
        jobs.run_reprocess(job.pk)
    job.refresh_from_db()
    sample_doc.refresh_from_db()
    assert job.status == DocumentJob.Status.COMMITTED
    assert sample_doc.content == "replacement provider content"
    assert sample_doc.source_path.exists()
    assert sample_doc.archive_path.exists()
    assert sample_doc.custom_fields.get(field=field).value
    assert not DocumentJob.objects.filter(kind="enrich").exists()
    CustomField.objects.update_or_create(
        pk=summary_id, defaults={"name": "summary", "data_type": "longtext"}
    )
    if concurrent_edit:
        sample_doc.title = "User title"
        sample_doc.save()
        with pytest.raises(RejectedResult, match="Document changed"):
            jobs.run_reprocess(job.pk)
        provider.process.assert_called_once()
        sample_doc.refresh_from_db()
        assert sample_doc.title == "User title"
        return
    assert jobs.run_reprocess(job.pk)
    provider.process.assert_called_once()
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.SUCCEEDED
    assert DocumentJob.objects.filter(pk=job.result["metadata_job"]).exists()
