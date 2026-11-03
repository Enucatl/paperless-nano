# ruff: noqa: INP001
"""Seed the isolated memory benchmark from its bounded private snapshot."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SNAPSHOT = Path("/snapshot")
PARSER_FIELDS = {
    "output_type",
    "pages",
    "language",
    "mode",
    "archive_file_generation",
    "image_dpi",
    "unpaper_clean",
    "deskew",
    "rotate_pages",
    "rotate_pages_threshold",
    "max_image_pixels",
    "color_conversion_strategy",
    "user_args",
    "barcodes_enabled",
    "barcode_enable_tiff_support",
    "barcode_string",
    "barcode_enable_asn",
    "barcode_asn_prefix",
    "barcode_upscale",
    "barcode_dpi",
    "barcode_max_pages",
    "barcode_enable_tag",
    "barcode_tag_mapping",
    "barcode_tag_split",
    "barcode_retain_split_pages",
    "barcode_store_values",
}


def main() -> None:
    """Populate a fresh benchmark database without copying identities or effects."""
    token = os.environ["BENCH_TOKEN"]
    if not 1 <= len(token) <= 40:
        raise ValueError("BENCH_TOKEN must contain between 1 and 40 characters")
    if os.environ.get("BENCH_ISOLATED") != "1":
        raise RuntimeError("Set BENCH_ISOLATED=1 only in the isolated benchmark")

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "paperless.settings")
    import django

    django.setup()

    from django.conf import settings
    from django.contrib.auth import get_user_model
    from django.core.management.color import no_style
    from django.db import connection
    from django.db import transaction
    from rest_framework.authtoken.models import Token

    from documents.models import Document
    from documents.models import Tag
    from documents.models import Workflow
    from documents.models import WorkflowAction
    from documents.models import WorkflowTrigger
    from documents.search import get_backend
    from paperless.models import ApplicationConfiguration

    if Document.global_objects.exists():
        raise RuntimeError("Benchmark seed requires an empty document database")
    rows = json.loads((SNAPSHOT / "selected-documents.json").read_text())
    manifest = json.loads((SNAPSHOT / "manifest.json").read_text())
    files_by_id = {row["id"]: row["files"] for row in manifest["documents"]}
    assert len(rows) == len(files_by_id) == 14
    candidates = sorted(
        (
            row
            for row in rows
            if row["mime_type"] == "application/pdf"
            and 1 <= (row["page_count"] or 0) <= 10
        ),
        key=lambda row: (row["page_count"], row["id"]),
    )
    reserved = [candidates[0], candidates[-1]]
    reserved_ids = {row["id"] for row in reserved}
    assert len(reserved_ids) == 2
    seed_rows = [row for row in rows if row["id"] not in reserved_ids]
    assert len(seed_rows) == 12
    assert {row["checksum"] for row in seed_rows}.isdisjoint(
        row["checksum"] for row in reserved
    )
    assert not settings.LOCAL_OCR_ENABLED
    assert not settings.CLASSIFIER_ENABLED
    assert not settings.AI_ALLOWED

    with transaction.atomic():
        user = get_user_model().objects.create_superuser(
            username="benchadmin",
            email="benchadmin@example.invalid",
            password="isolated-benchmark-only",
        )
        Token.objects.create(user=user, key=token)
        app_rows = json.loads(
            (SNAPSHOT / "config/application-configuration.json").read_text(),
        )
        app_field_names = {
            field.name for field in ApplicationConfiguration._meta.concrete_fields
        }
        app_values = {
            key: value
            for key, value in app_rows[0].items()
            if key in PARSER_FIELDS and key in app_field_names
        }
        ApplicationConfiguration.objects.update_or_create(pk=1, defaults=app_values)

        documents = []
        for row in seed_rows:
            values = {}
            for field in Document._meta.concrete_fields:
                if field.generated or field.is_relation or field.attname not in row:
                    continue
                if field.name in {
                    "deleted_at",
                    "restored_at",
                    "transaction_id",
                    "content_job",
                    "version_index",
                    "version_label",
                }:
                    continue
                values[field.attname] = field.to_python(row[field.attname])
            values["owner_id"] = user.pk
            document = Document(**values)
            assert document.root_document_id is None
            assert document.source_path.is_file()
            assert document.thumbnail_path.is_file()
            if document.archive_path:
                assert document.archive_path.is_file()
            documents.append(document)
        Document.objects.bulk_create(documents)
        # auto_now changes modified during bulk insertion; retain the copied dates.
        for document, row in zip(documents, seed_rows, strict=True):
            document.modified = Document._meta.get_field("modified").to_python(
                row["modified"],
            )
        Document.objects.bulk_update(documents, ["modified"])
        with connection.cursor() as cursor:
            for sql in connection.ops.sequence_reset_sql(no_style(), [Document]):
                cursor.execute(sql)

        trigger_tag = Tag.objects.create(
            name="benchmark-trigger",
            matching_algorithm=Tag.MATCH_NONE,
            owner=user,
        )
        final_tag = Tag.objects.create(
            name="benchmark-final",
            matching_algorithm=Tag.MATCH_NONE,
            owner=user,
        )
        trigger = WorkflowTrigger.objects.create(
            type=WorkflowTrigger.WorkflowTriggerType.DOCUMENT_UPDATED,
        )
        trigger.filter_has_tags.add(trigger_tag)
        action = WorkflowAction.objects.create(assign_title="benchmarkfinalstate")
        action.assign_tags.add(final_tag)
        workflow = Workflow.objects.create(name="Benchmark final-state indexing")
        workflow.triggers.add(trigger)
        workflow.actions.add(action)

    settings.INDEX_DIR.mkdir(parents=True, exist_ok=True)
    backend = get_backend()
    backend.rebuild(
        Document.objects.all()
        .select_related("correspondent", "document_type", "storage_path", "owner")
        .prefetch_related(
            "tags",
            "notes__user",
            "custom_fields__field",
            "barcodes",
            "versions__barcodes",
        ),
    )
    assert set(backend.search_ids("*", user=None)) == {row["id"] for row in seed_rows}
    backend.close()
    result = {
        "seeded_count": len(seed_rows),
        "seeded_ids": [row["id"] for row in seed_rows],
        "reserved": [
            {
                "id": row["id"],
                "page_count": row["page_count"],
                "original": files_by_id[row["id"]]["original"]["path"],
                "sha256": files_by_id[row["id"]]["original"]["sha256"],
            }
            for row in reserved
        ],
        "workflow_id": workflow.pk,
        "trigger_tag_id": trigger_tag.pk,
        "final_tag_id": final_tag.pk,
        "workflow_final_title": "benchmarkfinalstate",
        "copied_parser_fields": sorted(app_values),
    }
    results = SNAPSHOT / "results"
    results.mkdir(mode=0o700, exist_ok=True)
    output = results / "seed-result.json"
    output.write_text(json.dumps(result, indent=2) + "\n")
    output.chmod(0o600)
    sys.stdout.write(
        json.dumps({"seeded_documents": len(seed_rows), "reserved_documents": 2}) + "\n"
    )


if __name__ == "__main__":
    main()
