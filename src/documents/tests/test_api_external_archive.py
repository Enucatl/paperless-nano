"""External OCR archives preserve source files and commit text with the PDF."""

from hashlib import sha256
from io import BytesIO
from unittest.mock import patch

import pikepdf
from django.conf import settings
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import DatabaseError
from rest_framework.test import APITestCase

from documents.models import Document
from paperless_testing.dirs import DirectoriesMixin
from paperless_testing.factories import DocumentFactory
from paperless_testing.factories import UserFactory
from paperless_testing.permissions import grant_global
from paperless_testing.permissions import grant_object


class TestExternalArchive(DirectoriesMixin, APITestCase):
    """Exercise the archive API against real PDF and source files."""

    def setUp(self) -> None:
        super().setUp()
        self.user = UserFactory(superuser=True)
        self.client.force_authenticate(self.user)
        self.original = b"original image bytes"
        self.document = DocumentFactory(
            owner=self.user,
            mime_type="image/png",
            checksum=sha256(self.original).hexdigest(),
            content="old text",
        )
        Document.objects.filter(pk=self.document.pk).update(
            filename="original.png",
            archive_filename="old.pdf",
        )
        self.document.refresh_from_db()
        self.document.source_path.write_bytes(self.original)
        self.document.archive_path.write_bytes(b"previous archive")
        self.old_archive = self.document.archive_path
        buffer = BytesIO()
        with pikepdf.Pdf.new() as pdf:
            pdf.add_blank_page()
            pdf.save(buffer)
        self.pdf = buffer.getvalue()
        self.url = f"/api/documents/{self.document.pk}/archive/"

    def upload(self, **overrides):
        """Submit a fresh multipart upload for each assertion."""
        data = {
            "archive": SimpleUploadedFile("ocr.pdf", self.pdf, "application/pdf"),
            "source_checksum": self.document.checksum,
            "content": "  external OCR text\n",
        }
        data.update(overrides)
        return self.client.post(self.url, data, format="multipart")

    @patch("documents.tasks.bulk_update_documents.delay")
    def test_attach_preserves_original_and_updates_archive_and_text(
        self, update,
    ) -> None:
        """Install valid output, then remove the previous archive after commit."""
        with self.captureOnCommitCallbacks(execute=True):
            response = self.upload()
            self.assertTrue(self.old_archive.exists())
        self.assertEqual(response.status_code, 200, response.data)
        self.document.refresh_from_db()
        self.assertEqual(self.document.source_path.read_bytes(), self.original)
        self.assertEqual(self.document.archive_path.read_bytes(), self.pdf)
        self.assertEqual(self.document.content, "  external OCR text\n")
        self.assertEqual(self.document.archive_checksum, sha256(self.pdf).hexdigest())
        self.assertFalse(self.old_archive.exists())
        update.assert_called_once_with(document_ids=[self.document.pk])

    @patch("documents.tasks.bulk_update_documents.delay")
    def test_root_upload_updates_latest_version_like_original_download(
        self, update,
    ) -> None:
        """Root IDs target the same latest version as an original download."""
        version_bytes = b"latest version original"
        version = DocumentFactory(
            root_document=self.document,
            version_index=2,
            mime_type="image/png",
            checksum=sha256(version_bytes).hexdigest(),
            content="latest old text",
        )
        Document.objects.filter(pk=version.pk).update(filename="version.png")
        version.refresh_from_db()
        version.source_path.write_bytes(version_bytes)
        with self.captureOnCommitCallbacks(execute=True):
            response = self.upload(source_checksum=version.checksum)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["id"], version.pk)
        version.refresh_from_db()
        self.document.refresh_from_db()
        self.assertEqual(version.content, "  external OCR text\n")
        self.assertEqual(version.archive_path.read_bytes(), self.pdf)
        self.assertEqual(version.source_path.read_bytes(), version_bytes)
        self.assertEqual(self.document.content, "old text")
        self.assertEqual(self.old_archive.read_bytes(), b"previous archive")
        update.assert_called_once_with(document_ids=[self.document.pk])

    def test_stale_checksum_preserves_existing_archive_and_text(self) -> None:
        """Results tied to a previous source cannot overwrite current output."""
        response = self.upload(source_checksum="0" * 64)
        self.assertEqual(response.status_code, 409)
        self.document.refresh_from_db()
        self.assertEqual(self.document.content, "old text")
        self.assertEqual(self.old_archive.read_bytes(), b"previous archive")
        self.assertEqual(list(settings.ARCHIVE_DIR.iterdir()), [self.old_archive])

    def test_changed_source_bytes_rejected(self) -> None:
        """Check actual source bytes even when the database checksum is stale."""
        self.document.source_path.write_bytes(b"changed source")
        self.assertEqual(self.upload().status_code, 409)
        self.assertEqual(self.old_archive.read_bytes(), b"previous archive")

    def test_invalid_and_encrypted_pdf_rejected(self) -> None:
        """Reject malformed and password-protected PDFs before writing media."""
        encrypted = BytesIO()
        with pikepdf.open(BytesIO(self.pdf)) as pdf:
            pdf.save(
                encrypted, encryption=pikepdf.Encryption(owner="owner", user="secret"),
            )
        for data in (b"not a PDF", b"%PDF-invalid", encrypted.getvalue()):
            with self.subTest(data=data[:12]):
                response = self.upload(archive=SimpleUploadedFile("bad.pdf", data))
                self.assertEqual(response.status_code, 400)
        self.assertEqual(self.old_archive.read_bytes(), b"previous archive")

    def test_change_permission_required(self) -> None:
        """Viewing a document does not authorize replacing its OCR result."""
        viewer = UserFactory()
        grant_global(viewer, "view_document", "change_document")
        grant_object(viewer, self.document, "view_document")
        self.client.force_authenticate(viewer)
        self.assertEqual(self.upload().status_code, 403)
        self.assertEqual(self.old_archive.read_bytes(), b"previous archive")

    def test_database_failure_keeps_previous_archive(self) -> None:
        """Rollback discards only the newly written PDF."""
        with patch("django.db.models.query.QuerySet.update", side_effect=DatabaseError):
            with self.assertRaises(DatabaseError):
                self.upload()
        self.document.refresh_from_db()
        self.assertEqual(self.document.content, "old text")
        self.assertEqual(self.document.archive_path, self.old_archive)
        self.assertEqual(self.old_archive.read_bytes(), b"previous archive")
        self.assertEqual(list(settings.ARCHIVE_DIR.iterdir()), [self.old_archive])
