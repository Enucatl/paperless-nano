"""Ordered processing extensions shared by ingestion and legacy consumers."""

from documents.data_models import ConsumableDocument
from documents.plugins.base import ConsumeTaskPlugin


def consumption_plugins(input_doc: ConsumableDocument) -> list[type[ConsumeTaskPlugin]]:
    """Return the existing ordered ingestion extension points."""
    from documents.barcodes import BarcodePlugin
    from documents.consumer import AsnCheckPlugin
    from documents.consumer import ConsumerPlugin
    from documents.consumer import ConsumerPreflightPlugin
    from documents.consumer import WorkflowTriggerPlugin
    from documents.double_sided import CollatePlugin

    return (
        [
            ConsumerPreflightPlugin,
            ConsumerPlugin,
        ]
        if input_doc.root_document_id is not None
        else [
            ConsumerPreflightPlugin,
            AsnCheckPlugin,
            CollatePlugin,
            BarcodePlugin,
            AsnCheckPlugin,  # Re-run ASN check after barcode reading
            WorkflowTriggerPlugin,
            ConsumerPlugin,
        ]
    )
