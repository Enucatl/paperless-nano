"""Rich proposals stay untrusted until an atomic core-owned application."""

import json
from datetime import date

import pytest
from django.contrib.auth.models import Permission
from django.db import connection

from documents import enrichment_jobs
from documents.deferred_operations import cancel
from documents.deferred_operations import claim
from documents.deferred_operations import submit_result
from documents.enrichment_jobs import submit_enrichment
from documents.job_state import RejectedResult
from documents.models import Correspondent
from documents.models import CustomField
from documents.models import CustomFieldInstance
from documents.models import DocumentJob
from documents.models import Tag
from paperless_testing.factories import CorrespondentFactory
from paperless_testing.factories import TagFactory
from paperless_testing.factories import UserFactory

pytestmark = pytest.mark.django_db(transaction=True)


class RichMetadata:
    """Return a complete proposal without document write access."""

    identity = {"provider": "rich:1", "model": "model:1", "prompt": "prompt:1"}
    configuration = {"threshold": 0.8}
    replay_safe = True
    uses_remote_service = False

    def enrich(self, request):
        """Propose known values; core owns entity creation and custom-field storage."""
        return {
            "version": 1,
            "revision": request["revision"],
            "identity": self.identity,
            "title": "Invoice from Acme",
            "document_date": "2024-01-02",
            "summary": "",
            "processing_date": request["date"],
            "audit": {"model": "model:1", "metadata": {"title": "Invoice from Acme"}},
            "correspondent": {"create": "Acme"},
            "language": {"create": "language:en"},
        }


@pytest.fixture
def rich(settings, mocker, paperless_dirs):
    """Install a rich provider while leaving classification independent."""
    settings.ENRICHMENT_PROVIDER = (
        "documents.tests.test_rich_enrichment_jobs.RichMetadata"
    )
    settings.ENRICHMENT_FIELDS = {
        role: CustomField.objects.create(name=role, data_type=kind).pk
        for role, kind in (
            ("summary", "longtext"),
            ("processing_date", "date"),
            ("audit", "longtext"),
        )
    }
    mocker.patch("documents.jobs._dispatch")
    mocker.patch("documents.search.get_backend")
    mocker.patch("documents.workflows.runner.run_workflows")
    mocker.patch("documents.signals.document_updated.send")
    return UserFactory(is_superuser=True)


def stage_result(document, processor, *, actor_id=None, change=None):
    """Deliver an immutable result through the real capability-only claim path."""
    job = submit_enrichment(document.pk, actor_id=actor_id)
    enrichment_jobs.run_enrichment(str(job.pk))
    lease = claim(job.pk, "metadata", processor)
    result = RichMetadata().enrich(lease["input"])
    if change:
        change(result)
    submit_result(job.pk, "metadata", processor, lease["token"], result)
    return job, lease, result


def test_apply_preserves_unrelated_metadata_and_duplicate_delivery(
    sample_doc, rich, settings
):
    """Atomic publication preserves unrelated values and applies a staged result once."""
    keep = TagFactory(name="unrelated")
    old = TagFactory(name="language:de")
    sample_doc.tags.add(keep, old)
    extra = CustomField.objects.create(name="unrelated", data_type="string")
    CustomFieldInstance.objects.create(
        document=sample_doc, field=extra, value_text="keep"
    )
    source = sample_doc.source_path.read_bytes()
    archive = sample_doc.archive_path.read_bytes()
    job, lease, result = stage_result(sample_doc, rich)
    assert Correspondent.objects.count() == 0
    enrichment_jobs.run_enrichment(str(job.pk))
    sample_doc.refresh_from_db()
    assert sample_doc.title == "Invoice from Acme"
    assert sample_doc.created == date(2024, 1, 2)
    assert sample_doc.correspondent.name == "Acme"
    assert set(sample_doc.tags.values_list("name", flat=True)) == {
        "unrelated",
        "language:en",
    }
    assert sample_doc.custom_fields.get(field=extra).value == "keep"
    assert (
        sample_doc.custom_fields.get(
            field_id=settings.ENRICHMENT_FIELDS["summary"]
        ).value
        == ""
    )
    assert (
        sample_doc.custom_fields.get(
            field_id=settings.ENRICHMENT_FIELDS["processing_date"]
        ).value.isoformat()
        == lease["input"]["date"]
    )
    audit = json.loads(
        sample_doc.custom_fields.get(field_id=settings.ENRICHMENT_FIELDS["audit"]).value
    )
    assert audit["application"]["correspondent_id"] == sample_doc.correspondent_id
    modified = sample_doc.modified
    assert (
        submit_result(job.pk, "metadata", rich, lease["token"], result)["status"]
        == "succeeded"
    )
    enrichment_jobs.run_enrichment(str(job.pk))
    sample_doc.refresh_from_db()
    assert sample_doc.modified == modified
    assert Correspondent.objects.filter(name="Acme").count() == 1
    assert Tag.objects.filter(name="language:en").count() == 1
    assert sample_doc.source_path.read_bytes() == source
    assert sample_doc.archive_path.read_bytes() == archive


@pytest.mark.parametrize(
    "change",
    [
        lambda result: result.update(title=12),
        lambda result: result.update(
            custom_fields=[{"field": 999999, "data_type": "string", "value": "bad"}]
        ),
        lambda result: result.update(correspondent={"id": 999999}),
    ],
    ids=["title-type", "unmanaged-field", "forbidden-relation"],
)
def test_malformed_result_changes_nothing(sample_doc, rich, change):
    """Invalid types, unmanaged fields and forbidden relations reject the whole proposal."""
    job, _, _ = stage_result(sample_doc, rich, change=change)
    with pytest.raises(RejectedResult):
        enrichment_jobs.run_enrichment(str(job.pk))
    sample_doc.refresh_from_db()
    assert sample_doc.title == "test"
    assert not Correspondent.objects.exists()
    assert not sample_doc.custom_fields.exists()
    assert DocumentJob.objects.get(pk=job.pk).status == DocumentJob.Status.REJECTED


@pytest.mark.parametrize(
    "mutation",
    [
        "title",
        "custom_field",
        "provider",
        "configuration",
        "field_mapping",
        "field_type",
        "candidate",
    ],
)
def test_changed_snapshot_rejects_staged_result(
    sample_doc, rich, settings, monkeypatch, mutation
):
    """User edits, schema changes and changed model inputs cannot be overwritten."""
    candidate = CorrespondentFactory(name="existing")
    field = CustomField.objects.create(name="kept", data_type="string")
    stored = CustomFieldInstance.objects.create(
        document=sample_doc, field=field, value_text="before"
    )
    job, _, _ = stage_result(sample_doc, rich)
    if mutation == "title":
        sample_doc.title = "User edit"
        sample_doc.save()
    elif mutation == "custom_field":
        stored.value_text = "user edit"
        stored.save()
    elif mutation == "provider":
        monkeypatch.setattr(
            RichMetadata, "identity", {**RichMetadata.identity, "model": "model:2"}
        )
    elif mutation == "configuration":
        monkeypatch.setattr(RichMetadata, "configuration", {"threshold": 0.9})
    elif mutation == "field_mapping":
        settings.ENRICHMENT_FIELDS = {**settings.ENRICHMENT_FIELDS, "summary": field.pk}
    elif mutation == "field_type":
        CustomField.objects.filter(pk=settings.ENRICHMENT_FIELDS["summary"]).update(
            data_type="string"
        )
    else:
        candidate.name = "renamed"
        candidate.save()
    with pytest.raises(RejectedResult):
        enrichment_jobs.run_enrichment(str(job.pk))
    assert not Correspondent.objects.filter(name="Acme").exists()
    assert not Tag.objects.filter(name="language:en").exists()


@pytest.mark.parametrize(
    "revoked", ["change_document", "add_correspondent", "add_tag", "process_metadata"]
)
def test_permission_revocation_after_delivery(sample_doc, rich, revoked):
    """Both requester and processor capabilities are checked again on application."""
    actor = UserFactory()
    sample_doc.owner = actor
    sample_doc.save()
    actor.user_permissions.add(
        *Permission.objects.filter(
            codename__in=["change_document", "add_correspondent", "add_tag"]
        )
    )
    processor = UserFactory()
    processor.user_permissions.add(Permission.objects.get(codename="process_metadata"))
    job, _, _ = stage_result(sample_doc, processor, actor_id=actor.pk)
    target = processor if revoked == "process_metadata" else actor
    target.user_permissions.remove(Permission.objects.get(codename=revoked))
    with pytest.raises(Exception, match=r"(cannot|no longer|not authorized)"):
        enrichment_jobs.run_enrichment(str(job.pk))
    assert not Correspondent.objects.filter(name="Acme").exists()
    sample_doc.refresh_from_db()
    assert sample_doc.title == "test"


def test_cancellation_does_not_apply_staged_result(sample_doc, rich):
    """Cancellation before publication leaves successful OCR and metadata untouched."""
    job, _, _ = stage_result(sample_doc, rich)
    cancel(job.pk, rich)
    enrichment_jobs.run_enrichment(str(job.pk))
    sample_doc.refresh_from_db()
    assert sample_doc.title == "test"
    assert sample_doc.archive_path.exists()
    assert not Correspondent.objects.exists()


def test_commit_retry_does_not_create_or_apply_twice(sample_doc, rich, mocker):
    """A failure after database commit resumes file/search completion, not metadata."""
    job, _, _ = stage_result(sample_doc, rich)
    apply = mocker.spy(enrichment_jobs.rich_metadata, "apply")
    from documents.file_handling import reconcile_document_files

    def failed_after_commit(document):
        assert not connection.in_atomic_block
        raise OSError("file move unavailable")

    reconcile = mocker.patch(
        "documents.file_handling.reconcile_document_files",
        side_effect=failed_after_commit,
    )
    with pytest.raises(OSError, match="file move"):
        enrichment_jobs.run_enrichment(str(job.pk))
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.COMMITTED
    sample_doc.refresh_from_db()
    reconcile.side_effect = reconcile_document_files
    enrichment_jobs.run_enrichment(str(job.pk))
    sample_doc.refresh_from_db()
    assert apply.call_count == 1
    assert Correspondent.objects.filter(name="Acme").count() == 1
    assert DocumentJob.objects.get(pk=job.pk).status == DocumentJob.Status.SUCCEEDED


def test_uncertain_workflow_requires_review(sample_doc, rich, mocker):
    """Failed workflow effects are not blindly repeated after metadata has committed."""
    job, _, _ = stage_result(sample_doc, rich)
    workflow = mocker.patch(
        "documents.workflows.runner.run_workflows",
        side_effect=RuntimeError("notification lost"),
    )
    with pytest.raises(RuntimeError, match="notification"):
        enrichment_jobs.run_enrichment(str(job.pk))
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.ATTENTION
    assert "Metadata committed" in job.error
    enrichment_jobs.run_enrichment(str(job.pk))
    assert workflow.call_count == 1
    sample_doc.refresh_from_db()
    assert sample_doc.title == "Invoice from Acme"


def test_new_same_name_candidate_reuses_entity(sample_doc, rich):
    """A concurrent creator does not invalidate retained candidates or duplicate names."""
    job, _, _ = stage_result(sample_doc, rich)
    correspondent = CorrespondentFactory(name="Acme", owner=None)
    tag = TagFactory(name="language:en", owner=None)
    enrichment_jobs.run_enrichment(str(job.pk))
    sample_doc.refresh_from_db()
    assert sample_doc.correspondent_id == correspondent.pk
    assert list(sample_doc.tags.values_list("pk", flat=True)) == [tag.pk]


def test_hidden_same_name_entity_is_never_reused(sample_doc, rich):
    """Creation is scoped to the requester rather than a hidden entity owner."""
    hidden = CorrespondentFactory(name="Acme", owner=UserFactory())
    actor = UserFactory(is_superuser=False)
    actor.user_permissions.add(
        *Permission.objects.filter(
            codename__in=["change_document", "add_correspondent", "add_tag"]
        )
    )
    sample_doc.owner = actor
    sample_doc.save()
    job, _, _ = stage_result(sample_doc, rich, actor_id=actor.pk)
    enrichment_jobs.run_enrichment(str(job.pk))
    sample_doc.refresh_from_db()
    assert sample_doc.correspondent_id != hidden.pk
    assert sample_doc.correspondent.owner_id == actor.pk


def test_remote_inference_requires_explicit_policy(sample_doc, rich, monkeypatch):
    """Selecting a provider cannot implicitly enable remote document disclosure."""
    monkeypatch.setattr(RichMetadata, "uses_remote_service", True)
    with pytest.raises(RejectedResult, match="Remote"):
        submit_enrichment(sample_doc.pk)


def test_duplicate_managed_field_mapping_is_rejected(sample_doc, rich, settings):
    """Reserved roles cannot alias each other or an additional writable field."""
    settings.ENRICHMENT_FIELDS["additional"] = settings.ENRICHMENT_FIELDS["summary"]
    with pytest.raises(RejectedResult, match="distinct"):
        submit_enrichment(sample_doc.pk)


def test_empty_content_has_no_followup_effects(sample_doc, rich, mocker):
    """The empty-content no-op does not accidentally trigger update workflows."""
    sample_doc.content = ""
    sample_doc.save()
    workflow = mocker.patch("documents.workflows.runner.run_workflows")
    job, _, _ = stage_result(
        sample_doc,
        rich,
        change=lambda result: [
            result.pop(key)
            for key in list(result)
            if key not in {"version", "revision", "identity"}
        ],
    )
    enrichment_jobs.run_enrichment(str(job.pk))
    assert DocumentJob.objects.get(pk=job.pk).status == DocumentJob.Status.SUCCEEDED
    workflow.assert_not_called()
    assert not sample_doc.custom_fields.exists()


def test_restart_after_acceptance_revalidates_and_applies_once(
    sample_doc, rich, mocker
):
    """A process exit between journal acceptance and commit retains the proposal."""
    job, _, _ = stage_result(sample_doc, rich)
    apply = enrichment_jobs.rich_metadata.apply
    interrupted = mocker.patch.object(
        enrichment_jobs.rich_metadata,
        "apply",
        side_effect=SystemExit("process stopped"),
    )
    with pytest.raises(SystemExit):
        enrichment_jobs.run_enrichment(str(job.pk))
    job.refresh_from_db()
    assert job.result["invocations"]["metadata"]["status"] == "succeeded"
    assert not Correspondent.objects.exists()
    interrupted.side_effect = apply
    enrichment_jobs.run_enrichment(str(job.pk))
    sample_doc.refresh_from_db()
    assert sample_doc.title == "Invoice from Acme"
    assert DocumentJob.objects.get(pk=job.pk).status == DocumentJob.Status.SUCCEEDED


def test_failure_after_entity_creation_rolls_back_everything(sample_doc, rich, mocker):
    """Entity creation and metadata persistence share the same transaction."""
    job, _, _ = stage_result(sample_doc, rich)
    mocker.patch(
        "documents.enrichment_operations.save_document_metadata",
        side_effect=SystemExit("process stopped"),
    )
    with pytest.raises(SystemExit):
        enrichment_jobs.run_enrichment(str(job.pk))
    assert not Correspondent.objects.exists()
    assert not Tag.objects.exists()
    sample_doc.refresh_from_db()
    assert sample_doc.title == "test"


def test_concurrent_postgres_creators_use_one_entity(sample_doc, rich, mocker):
    """Two real connections race through the unique owner/name creation boundary."""
    if connection.vendor != "postgresql":
        pytest.skip("Requires PostgreSQL concurrency semantics")
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    from django.db import close_old_connections

    from documents.enrichment_operations import resolve_relation
    from paperless_testing.factories import DocumentFactory

    second = DocumentFactory(pk=2, content="other invoice")
    first_job, _, _ = stage_result(sample_doc, rich)
    second_job, _, _ = stage_result(second, rich)
    barrier = Barrier(2)

    def racing_relation(document, actor_id, model, proposal):
        """Synchronize both creators immediately before the database uniqueness race."""
        if model is Correspondent:
            barrier.wait(timeout=10)
        return resolve_relation(document, actor_id, model, proposal)

    def run(identifier):
        """Give each worker a separate connection and close it deterministically."""
        close_old_connections()
        try:
            enrichment_jobs.run_enrichment(str(identifier))
        finally:
            connection.close()

    mocker.patch("documents.file_handling.reconcile_document_files")
    mocker.patch(
        "documents.enrichment_operations.resolve_relation", side_effect=racing_relation
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(run, [first_job.pk, second_job.pk]))
    sample_doc.refresh_from_db()
    second.refresh_from_db()
    assert sample_doc.correspondent_id == second.correspondent_id
    assert Correspondent.objects.filter(name="Acme", owner=None).count() == 1
    assert Tag.objects.filter(name="language:en", owner=None).count() == 1


def test_operator_can_acknowledge_committed_followup(sample_doc, rich, mocker):
    """Reviewed external effects are skipped while file/search recovery still completes."""
    from io import StringIO

    from django.core.management import CommandError
    from django.core.management import call_command

    job, _, _ = stage_result(sample_doc, rich)
    workflow = mocker.patch(
        "documents.workflows.runner.run_workflows",
        side_effect=RuntimeError("external outcome unknown"),
    )
    with pytest.raises(RuntimeError):
        enrichment_jobs.run_enrichment(str(job.pk))
    with pytest.raises(CommandError, match="acknowledge"):
        call_command("document_jobs", job=str(job.pk), resume=True, stdout=StringIO())
    call_command(
        "document_jobs",
        job=str(job.pk),
        resume=True,
        acknowledge_effects=True,
        stdout=StringIO(),
    )
    enrichment_jobs.run_enrichment(str(job.pk))
    assert workflow.call_count == 1
    job.refresh_from_db()
    assert job.status == DocumentJob.Status.SUCCEEDED
    assert job.result["invocations"]["metadata:followup"]["acknowledged"]


def test_rich_metadata_cannot_write_ocr_provenance(sample_doc, rich, settings):
    """The administrator's rich mapping must not alias the reserved parser output."""
    settings.OCR_PROVENANCE_FIELD = str(settings.ENRICHMENT_FIELDS["audit"])
    with pytest.raises(RejectedResult, match="OCR provenance"):
        submit_enrichment(sample_doc.pk)


def test_language_replacement_preserves_unrelated_descendants(sample_doc, rich):
    """A managed language ancestor must remain while unrelated children need it."""
    old = TagFactory(name="language:de")
    unrelated = TagFactory(name="unrelated child", tn_parent=old)
    sample_doc.tags.add(old, unrelated)
    job, _, _ = stage_result(sample_doc, rich)
    enrichment_jobs.run_enrichment(str(job.pk))
    assert set(sample_doc.tags.values_list("name", flat=True)) == {
        "language:de",
        "language:en",
        "unrelated child",
    }
