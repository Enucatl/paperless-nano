"""Select explicitly placed server work for the shared Celery runners."""

from django.conf import settings
from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied

CAPABILITIES = ("parser", "metadata", "search", "ingestion")


def capability_for(operation: str) -> str:
    """Map preparation and provider operations to their execution capability."""
    return (
        "ingestion"
        if operation.startswith("prepare:") or operation == "parser.thumbnail"
        else operation.split(".", 1)[0]
    )


def selected_user(entry: dict) -> User | None:
    """Require explicit server placement and a current capability grant."""
    from documents.deferred_operations import authorize

    if capability_for(entry["operation"]) not in settings.PROCESSOR_CAPABILITIES:
        return None
    user = User.objects.filter(username=settings.PROCESSOR_USER, is_active=True).first()
    if user is None:
        return None
    try:
        authorize(entry, user)
    except PermissionDenied:
        return None
    return user
