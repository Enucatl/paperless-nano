"""Consume eligible work once, suitable for supervised periodic execution."""

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.core.management.base import BaseCommand
from filelock import Timeout

from documents.models import DocumentJob
from documents.server_processor import process


class Command(BaseCommand):
    """Run explicitly selected provider operations with a capability account."""

    help = "Process pending operations on server storage using a processor account."

    def add_arguments(self, parser):
        """Require explicit account, operations and provider identities."""
        parser.add_argument("--user", required=True)
        parser.add_argument("--operation", action="append", required=True)
        parser.add_argument("--provider", action="append", required=True)

    def handle(self, *args, **options):
        """Consume a snapshot of eligible pending operations."""
        user = User.objects.get(username=options["user"], is_active=True)
        for job in DocumentJob.objects.filter(
            status=DocumentJob.Status.WAITING
        ).iterator():
            for key, entry in job.result.get("invocations", {}).items():
                if (
                    entry.get("operation") not in options["operation"]
                    or entry.get("plugin") not in options["provider"]
                ):
                    continue
                try:
                    process(job.pk, key, user)
                except (PermissionDenied, Timeout):
                    continue
