"""Install archives produced by external OCR services without changing originals."""

import tempfile
from pathlib import Path

from django.conf import settings
from django.core.files.uploadedfile import UploadedFile
from django.db import transaction
from django.utils import timezone
from filelock import FileLock
from rest_framework.exceptions import APIException
from rest_framework.exceptions import ValidationError

from documents.caching import clear_document_caches
from documents.file_handling import create_source_path_directory
from documents.file_handling import generate_unique_filename
from documents.file_handling import validate_path_in_root
from documents.language import detect_document_language
from documents.models import Document
from documents.utils import compute_checksum


class StaleArchiveSource(APIException):
    """Reject OCR results generated from a different original."""

    status_code = 409
    default_detail = "The source document has changed; regenerate the archive."
    default_code = "stale_source"


def attach_external_archive(
    document_id: int,
    archive: UploadedFile,
    content: str,
    source_checksum: str,
) -> Document:
    """Validate and install an external PDF and its text as one document update."""
    import pikepdf

    from documents.tasks import bulk_update_documents

    new_path = None
    created_path = None
    with tempfile.NamedTemporaryFile(dir=settings.SCRATCH_DIR, suffix=".pdf") as staged:
        for chunk in archive.chunks():
            staged.write(chunk)
        staged.flush()
        staged.seek(0)
        if staged.read(5) != b"%PDF-":
            raise ValidationError({"archive": "Upload a valid PDF."})
        try:
            with pikepdf.open(staged.name) as pdf:
                if pdf.is_encrypted or not len(pdf.pages):
                    raise ValidationError(
                        {"archive": "Upload an unencrypted PDF with pages."},
                    )
        except (pikepdf.PdfError, pikepdf.PasswordError) as error:
            raise ValidationError(
                {"archive": "Upload a readable, unencrypted PDF."},
            ) from error

        checksum = compute_checksum(Path(staged.name))
        try:
            with transaction.atomic(durable=True):
                document = Document.objects.select_for_update().get(pk=document_id)
                with FileLock(settings.MEDIA_LOCK):
                    if (
                        document.checksum != source_checksum
                        or compute_checksum(document.source_path) != source_checksum
                    ):
                        raise StaleArchiveSource
                    old_path = document.archive_path
                    if old_path:
                        validate_path_in_root(old_path, settings.ARCHIVE_DIR)
                    # A fresh filename keeps the previous archive intact until commit.
                    document.archive_filename = None
                    filename = generate_unique_filename(document, archive_filename=True)
                    new_path = settings.ARCHIVE_DIR / filename
                    validate_path_in_root(new_path, settings.ARCHIVE_DIR)
                    create_source_path_directory(new_path)
                    staged.seek(0)
                    with new_path.open("xb") as destination:
                        created_path = new_path
                        while chunk := staged.read(65536):
                            destination.write(chunk)
                    root = document.root_document or document
                    language = (
                        document.language
                        or root.language
                        or detect_document_language(content)
                    )
                    Document.objects.filter(pk=document.pk).update(
                        archive_filename=str(filename),
                        archive_checksum=checksum,
                        content=content,
                        language=language,
                        modified=timezone.now(),
                    )
                    if document.root_document_id:
                        Document.objects.filter(
                            pk=document.root_document_id, language=""
                        ).update(language=language)
                        Document.objects.filter(pk=document.root_document_id).update(
                            modified=timezone.now(),
                        )
                    if settings.AUDIT_LOG_ENABLED:
                        from auditlog.models import LogEntry

                        LogEntry.objects.log_create(
                            instance=document,
                            action=LogEntry.Action.UPDATE,
                            changes={
                                "content": [document.content, content],
                                "language": [document.language, language],
                                "archive_checksum": [
                                    document.archive_checksum,
                                    checksum,
                                ],
                                "archive_filename": [
                                    str(old_path.relative_to(settings.ARCHIVE_DIR))
                                    if old_path
                                    else None,
                                    str(filename),
                                ],
                            },
                            additional_data={"reason": "Attach external OCR archive"},
                        )
                    document.refresh_from_db()
                    if old_path and old_path != new_path:
                        transaction.on_commit(
                            lambda: old_path.unlink(missing_ok=True),
                            robust=True,
                        )
                    transaction.on_commit(
                        lambda: clear_document_caches(document.pk),
                        robust=True,
                    )
                    if document.root_document_id:
                        transaction.on_commit(
                            lambda: clear_document_caches(document.root_document_id),
                            robust=True,
                        )
                    transaction.on_commit(
                        lambda: bulk_update_documents.delay(
                            document_ids=[document.root_document_id or document.pk],
                        ),
                        robust=True,
                    )
        except Exception:
            if created_path is not None:
                created_path.unlink(missing_ok=True)
            raise
    return document
