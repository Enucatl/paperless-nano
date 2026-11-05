import shutil
from pathlib import Path

import pytest
from django.core.management import call_command
from django.test import TestCase

from documents.management.commands.document_thumbnails import _process_document
from documents.models import Document
from documents.models import DocumentJob
from paperless_testing.assertions import FileSystemAssertsMixin
from paperless_testing.dirs import DirectoriesMixin
from paperless_testing.samples import SIMPLE_DIGITAL_PDF


@pytest.mark.management
class TestMakeThumbnails(DirectoriesMixin, FileSystemAssertsMixin, TestCase):
    def make_models(self) -> None:
        self.d1 = Document.objects.create(
            checksum="A",
            title="A",
            content="first document",
            mime_type="application/pdf",
            filename="test.pdf",
        )
        shutil.copy(
            SIMPLE_DIGITAL_PDF,
            self.d1.source_path,
        )

        self.d2 = Document.objects.create(
            checksum="B",
            title="B",
            content="second document",
            mime_type="application/pdf",
            filename="test2.pdf",
        )
        shutil.copy(
            SIMPLE_DIGITAL_PDF,
            self.d2.source_path,
        )

        self.d3 = Document.objects.create(
            checksum="C",
            title="C",
            content="third document",
            mime_type="application/pdf",
            filename="test3.pdf",
        )
        shutil.copy(
            Path(__file__).parent / "samples" / "password-is-test.pdf",
            self.d3.source_path,
        )

    def setUp(self) -> None:
        super().setUp()
        self.make_models()

    def test_process_document(self) -> None:
        self.assertIsNotFile(self.d1.thumbnail_path)
        _process_document(self.d1.id)
        job = DocumentJob.objects.get(input__document_id=self.d1.pk)
        self.assertTrue(job.input["thumbnail_only"])
        self.assertEqual(job.status, DocumentJob.Status.QUEUED)
        self.assertIsNotFile(self.d1.thumbnail_path)

    def test_command(self) -> None:
        call_command("document_thumbnails", "--processes", "1", skip_checks=True)
        self.assertEqual(DocumentJob.objects.count(), 3)
        self.assertTrue(
            all(job.input["thumbnail_only"] for job in DocumentJob.objects.all())
        )

    def test_command_documentid(self) -> None:
        call_command(
            "document_thumbnails",
            "--processes",
            "1",
            "-d",
            str(self.d1.pk),
            skip_checks=True,
        )
        self.assertEqual(DocumentJob.objects.get().input["document_id"], self.d1.pk)
