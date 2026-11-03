"""Load search records and permission grants through Django persistence."""

from __future__ import annotations

from itertools import islice
from typing import TYPE_CHECKING
from typing import NamedTuple

from documents.search._records import document_to_record
from documents.utils import QuerySetStream

if TYPE_CHECKING:
    from collections.abc import Iterator
    from collections.abc import Sequence

    from django.db.models import QuerySet

    from documents.models import Document
    from documents.search._records import IndexRecord


class ViewerGrant(NamedTuple):
    """Direct user and group view grants for a single document."""

    viewer_ids: list[int]
    viewer_group_ids: list[int]


def _bulk_get_viewer_permissions(
    doc_pks: Sequence[int],
) -> dict[int, ViewerGrant]:
    """Fetch grants without expanding groups into potentially stale memberships."""
    from documents.models import Document
    from documents.permissions import get_permission_grantees

    return {
        pk: ViewerGrant(
            viewer_ids=grantees["view"]["users"],
            viewer_group_ids=grantees["view"]["groups"],
        )
        for pk, grantees in get_permission_grantees(
            Document,
            doc_pks,
            actions=("view",),
        ).items()
    }


class _DocumentViewerStream(QuerySetStream["Document"]):
    """Stream documents with their grants, fetching permissions once per chunk."""

    def __iter__(self) -> Iterator[tuple[Document, ViewerGrant]]:
        documents = self._queryset.iterator(chunk_size=self._chunk_size)
        while chunk := list(islice(documents, self._chunk_size)):
            grants = _bulk_get_viewer_permissions([doc.pk for doc in chunk])
            for document in chunk:
                yield document, grants[document.pk]


def index_records(
    documents: QuerySet[Document],
    chunk_size: int = 1000,
) -> Iterator[IndexRecord]:
    """Map documents lazily so callers can acquire the writer before reading."""
    for document, grant in _DocumentViewerStream(documents, chunk_size=chunk_size):
        yield document_to_record(
            document,
            viewer_ids=grant.viewer_ids,
            viewer_group_ids=grant.viewer_group_ids,
        )


def documents_for_ids(ids: Sequence[int]) -> QuerySet[Document]:
    """Load each existing root once with effective content and index relations."""
    from documents.models import Document
    from documents.versioning import annotate_effective_content
    from documents.versioning import root_document_ids

    return annotate_effective_content(
        Document.objects.filter(pk__in=root_document_ids(ids))
        .select_related("correspondent", "document_type", "storage_path", "owner")
        .prefetch_related(
            "tags",
            "notes__user",
            "custom_fields__field",
            "barcodes",
            "versions__barcodes",
        ),
    )


def record_for_document(document: Document) -> IndexRecord:
    """Resolve a document's root and current grants before mapping its record."""
    if document.root_document_id is not None:
        document = document.root_document
    grant = _bulk_get_viewer_permissions([document.pk])[document.pk]
    return document_to_record(
        document,
        viewer_ids=grant.viewer_ids,
        viewer_group_ids=grant.viewer_group_ids,
    )
