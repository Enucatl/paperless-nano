"""Publish source-bound OCR provenance in the same transaction as document text."""

import json

from django.conf import settings
from django.contrib.auth.models import User

from documents.job_state import RejectedResult
from documents.job_state import check_permission
from documents.job_state import requester_id
from documents.models import CustomField
from documents.models import CustomFieldInstance
from documents.models import Document
from documents.models import DocumentJob


def field_snapshot() -> dict | None:
    """Retain an explicit existing longtext field; providers cannot create schema."""
    configured = settings.OCR_PROVENANCE_FIELD
    if configured in (None, ""):
        return None
    try:
        field = CustomField.objects.get(pk=int(configured), data_type="longtext")
    except (ValueError, TypeError, CustomField.DoesNotExist) as error:
        raise RejectedResult(
            "OCR provenance requires an existing longtext field"
        ) from error
    if field.pk in settings.ENRICHMENT_FIELDS.values():
        raise RejectedResult("OCR provenance and enrichment require distinct fields")
    return {"id": field.pk, "data_type": field.data_type}


def publish_provenance(job: DocumentJob, document: Document) -> None:
    """Apply only the reserved field after current actor/configuration validation."""
    provenance = job.result.get("parser_result", {}).get("provenance")
    if provenance is None:
        return
    retained = job.configuration.get("provenance_field")
    if not retained or retained != field_snapshot():
        raise RejectedResult("OCR provenance field changed or was not configured")
    actor_id = requester_id(job)
    if job.kind == "consume":
        if job.input.get("root_document_id"):
            check_permission(
                Document.objects.get(pk=job.input["root_document_id"]), actor_id
            )
        elif actor_id is not None:
            actor = User.objects.filter(pk=actor_id, is_active=True).first()
            if actor is None or not actor.has_perm("documents.add_document"):
                raise RejectedResult("The requesting user can no longer add documents")
    else:
        check_permission(document, actor_id)
    CustomFieldInstance.objects.update_or_create(
        document=document,
        field_id=retained["id"],
        defaults={
            "value_long_text": json.dumps(
                provenance, ensure_ascii=False, allow_nan=False
            )
        },
    )
