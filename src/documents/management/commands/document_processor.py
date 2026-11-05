"""Consume one finite sweep of explicitly selected server work."""

import logging

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from filelock import Timeout

from documents.deferred_operations import authorize
from documents.models import DocumentJob
from documents.server_processor import process

CAPABILITIES = ("parser", "metadata", "search", "ingestion")
logger = logging.getLogger("paperless.processor")


class Command(BaseCommand):
    """Run selected configured providers with a capability account."""

    help = "Process pending operations on server storage using a processor account."

    def add_arguments(self, parser) -> None:
        """Require explicit account and placement; retain exact identity filters."""
        parser.add_argument("--user", required=True)
        parser.add_argument("--operation", action="append", default=[])
        parser.add_argument(
            "--capability", action="append", choices=CAPABILITIES, default=[]
        )
        providers = parser.add_mutually_exclusive_group(required=True)
        providers.add_argument("--provider", action="append")
        providers.add_argument("--configured", action="store_true")
        parser.add_argument("--diagnose", action="store_true")

    def handle(self, *args, **options) -> None:
        """Consume a snapshot; core runners validate current provider identities."""
        if not options["operation"] and not options["capability"]:
            raise CommandError("Select --operation or --capability explicitly")
        try:
            user = User.objects.get(username=options["user"], is_active=True)
        except User.DoesNotExist as error:
            raise CommandError("Processor account is missing or inactive") from error
        # Task publication can close the connection; retain IDs, not a database cursor.
        identifiers = list(
            DocumentJob.objects.filter(status=DocumentJob.Status.WAITING).values_list(
                "pk", flat=True
            )
        )
        for identifier in identifiers:
            job = DocumentJob.objects.filter(
                pk=identifier, status=DocumentJob.Status.WAITING
            ).first()
            if job is None:
                continue
            for key, entry in job.result.get("invocations", {}).items():
                if entry.get("status") != "pending":
                    continue
                operation = entry.get("operation", "")
                capability = (
                    "ingestion"
                    if operation.startswith("prepare:")
                    else operation.split(".", 1)[0]
                )
                selected = (
                    operation in options["operation"]
                    or capability in options["capability"]
                ) and (
                    options["configured"] or entry.get("plugin") in options["provider"]
                )
                if not selected:
                    if options["diagnose"]:
                        self.stdout.write(
                            f"{job.pk} {key}: not selected on this host (intentional waiting)"
                        )
                    continue
                try:
                    if options["diagnose"]:
                        authorize(entry, user)
                        self.stdout.write(
                            f"{job.pk} {key}: eligible; provider/configuration checked at execution"
                        )
                    else:
                        process(job.pk, key, user)
                except PermissionDenied:
                    self.stderr.write(
                        f"{job.pk} {key}: processor lacks {entry.get('policy', {}).get('permission', 'required permission')}; check account grants"
                    )
                except Timeout:
                    continue
                except Exception:
                    # One unavailable endpoint must not strand unrelated operations.
                    logger.exception(
                        "Processor failed for job %s operation %s; inspect Tasks",
                        job.pk,
                        key,
                    )
