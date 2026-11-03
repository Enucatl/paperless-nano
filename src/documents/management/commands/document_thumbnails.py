import logging
import shutil
from uuid import uuid4

from django.conf import settings
from filelock import FileLock

from documents.file_handling import validate_path_in_root
from documents.jobs import RejectedResult
from documents.jobs import _copy
from documents.jobs import _revision
from documents.jobs import job_directory
from documents.management.commands.base import PaperlessCommand
from documents.models import Document
from documents.parser_operations import configured_parser
from documents.parser_operations import effective_configuration
from documents.parser_operations import run_parser
from documents.utils import compute_checksum
from paperless.parsers.contract import resolve_reference
from paperless.parsers.registry import get_parser_registry

logger = logging.getLogger("paperless.management.thumbnails")


def _process_document(doc_id: int) -> None:
    """Regenerate only a thumbnail and publish it against the unchanged source."""
    document: Document = Document.objects.get(id=doc_id)
    parser_class = get_parser_registry().get_parser_for_file(
        document.mime_type,
        document.original_filename or "",
        document.source_path,
        allow_remote=False,
        required_capabilities=("thumbnail",),
    )

    if parser_class is None:
        logger.warning(
            "%s: No parser for mime type %s",
            document,
            document.mime_type,
        )
        return

    revision = _revision(document)
    source = document.source_path
    checksum = compute_checksum(source)
    configuration = effective_configuration()
    directory = job_directory(uuid4())
    try:
        with configured_parser(parser_class, configuration) as parser:
            result = run_parser(
                parser,
                source,
                document.mime_type,
                document.original_filename or source.name,
                configuration,
                directory,
                required_outputs=["thumbnail"],
            )
        with FileLock(settings.MEDIA_LOCK, timeout=0):
            document.refresh_from_db()
            if (
                _revision(document) != revision
                or compute_checksum(source) != checksum
                or effective_configuration() != configuration
            ):
                raise RejectedResult(
                    "Document or configuration changed during thumbnail generation"
                )
            thumbnail = resolve_reference(directory, result["thumbnail"])
            destination = settings.THUMBNAIL_DIR / f"{document.pk:07}.webp"
            validate_path_in_root(destination, settings.THUMBNAIL_DIR)
            if destination.is_symlink():
                raise RejectedResult("Thumbnail destination is a symbolic link")
            _copy(thumbnail, destination)
    finally:
        # Retain the private input if the permanent source vanished or was replaced.
        if (
            source.is_file()
            and compute_checksum(source) == checksum
            and directory.exists()
        ):
            shutil.rmtree(directory)


class Command(PaperlessCommand):
    help = "This will regenerate the thumbnails for all documents."

    supports_progress_bar = True
    supports_multiprocessing = True

    def add_arguments(self, parser) -> None:
        super().add_arguments(parser)
        parser.add_argument(
            "-d",
            "--document",
            default=None,
            type=int,
            required=False,
            help=(
                "Specify the ID of a document, and this command will only "
                "run on this specific document."
            ),
        )

    def handle(self, *args, **options):
        logging.getLogger().handlers[0].level = logging.ERROR

        if options["document"]:
            documents = Document.objects.filter(pk=options["document"])
        else:
            documents = Document.objects.all()

        ids = list(documents.values_list("id", flat=True))

        for result in self.process_parallel(
            _process_document,
            ids,
            description="Regenerating thumbnails...",
        ):
            if result.error:  # pragma: no cover
                self.console.print(
                    f"[red]Failed document {result.item}: {result.error}[/red]",
                )
