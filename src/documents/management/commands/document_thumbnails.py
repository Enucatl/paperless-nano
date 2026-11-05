import logging

from documents.jobs import submit_reprocess
from documents.management.commands.base import PaperlessCommand
from documents.models import Document


def _process_document(doc_id: int) -> None:
    """Queue thumbnail-only work without changing content or archive files."""
    submit_reprocess(doc_id, thumbnail_only=True)


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
            description="Queueing thumbnails...",
        ):
            if result.error:  # pragma: no cover
                self.console.print(
                    f"[red]Failed document {result.item}: {result.error}[/red]",
                )
