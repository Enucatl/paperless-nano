"""Storage-path persistence must precede document-update dispatch."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest import mock

import pytest

from documents.models import PaperlessTask
from documents.models import StoragePath
from documents.serialisers import StoragePathSerializer
from paperless_testing.factories import DocumentFactory
from paperless_testing.factories import StoragePathFactory

if TYPE_CHECKING:
    from paperless_testing.dirs import PaperlessDirs

pytestmark = pytest.mark.django_db


def test_document_update_dispatch_sees_saved_storage_path(
    paperless_dirs: PaperlessDirs,
) -> None:
    """An immediately executing worker must read the updated filename template."""
    storage_path = StoragePathFactory(path="old/{{ title }}")
    document = DocumentFactory(storage_path=storage_path)
    serializer = StoragePathSerializer(
        storage_path,
        data={"path": "new/{{ title }}"},
        partial=True,
    )
    serializer.is_valid(raise_exception=True)

    def dispatch(*, kwargs: dict[str, list[int]], headers: dict[str, str]) -> None:
        """Observe the same persisted path that a queued worker would load."""
        assert kwargs == {"document_ids": [document.pk]}
        assert headers == {"trigger_source": PaperlessTask.TriggerSource.SYSTEM}
        assert StoragePath.objects.get(pk=storage_path.pk).path == "new/{{ title }}"

    with mock.patch(
        "documents.serialisers.bulk_edit.bulk_update_documents.apply_async",
        side_effect=dispatch,
    ) as enqueue:
        updated = serializer.save()

    enqueue.assert_called_once()
    assert updated.path == "new/{{ title }}"


def test_failed_storage_path_save_does_not_dispatch_document_update(
    paperless_dirs: PaperlessDirs,
) -> None:
    """Failed persistence must not queue work for a change that never happened."""
    storage_path = StoragePathFactory(path="old/{{ title }}")
    DocumentFactory(storage_path=storage_path)
    serializer = StoragePathSerializer(
        storage_path,
        data={"path": "new/{{ title }}"},
        partial=True,
    )
    serializer.is_valid(raise_exception=True)

    with (
        mock.patch.object(StoragePath, "save", side_effect=RuntimeError("save failed")),
        mock.patch(
            "documents.serialisers.bulk_edit.bulk_update_documents.apply_async",
        ) as enqueue,
        pytest.raises(RuntimeError, match="save failed"),
    ):
        serializer.save()

    enqueue.assert_not_called()
    storage_path.refresh_from_db()
    assert storage_path.path == "old/{{ title }}"
