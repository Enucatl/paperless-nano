from documents.caching import invalidate_llm_suggestions_cache
from documents.caching import invalidate_suggestions_cache
from documents.file_handling import reconcile_document_files
from documents.management.commands.base import PaperlessCommand
from documents.models import Document


class Command(PaperlessCommand):
    help = "Rename all documents"

    supports_progress_bar = True
    supports_multiprocessing = False

    def handle(self, *args, **options):
        for document in self.track(Document.objects.all(), description="Renaming..."):
            reconcile_document_files(document)
            invalidate_suggestions_cache(document.pk)
            invalidate_llm_suggestions_cache(document.pk)
