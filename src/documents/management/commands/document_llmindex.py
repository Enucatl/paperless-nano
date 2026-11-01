from typing import Any

from django.conf import settings

from documents.management.commands.base import PaperlessCommand
from documents.tasks import llmindex_index


class Command(PaperlessCommand):
    help = "Manages the LLM-based vector index for Paperless."

    supports_progress_bar = True
    supports_multiprocessing = False

    def add_arguments(self, parser: Any) -> None:
        super().add_arguments(parser)
        parser.add_argument(
            "command",
            choices=["rebuild", "update", "compact", "migrate"],
        )

    def handle(self, *args: Any, **options: Any) -> None:
        if not settings.AI_ALLOWED:
            return
        if options["command"] == "compact":
            from paperless_ai.indexing import llm_index_compact

            llm_index_compact()
            return
        if options["command"] == "migrate":
            from paperless_ai.indexing import llm_index_migrate

            llm_index_migrate()
            return
        llmindex_index(
            rebuild=options["command"] == "rebuild",
            iter_wrapper=lambda docs: self.track(
                docs,
                description="Indexing documents...",
            ),
        )
