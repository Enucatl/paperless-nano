"""Consume explicitly selected server work once or during an event-driven burst."""

import json
import logging
import time

from django.contrib.auth.models import User
from django.core.exceptions import PermissionDenied
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from filelock import Timeout
from redis.exceptions import RedisError

from documents.deferred_operations import authorize
from documents.models import DocumentJob
from documents.server_processor import process
from paperless_services.processor_queue import CAPABILITIES
from paperless_services.processor_queue import Queue
from paperless_services.processor_queue import capability_for
from paperless_services.processor_queue import idle_timeout

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
        parser.add_argument("--events", action="store_true")
        parser.add_argument("--reconcile", action="store_true")
        parser.add_argument("--notification")

    def handle(self, *args, **options) -> None:
        """Consume a snapshot; core runners validate current provider identities."""
        if not options["operation"] and not options["capability"]:
            raise CommandError("Select --operation or --capability explicitly")
        try:
            user = User.objects.get(username=options["user"], is_active=True)
        except User.DoesNotExist as error:
            raise CommandError(
                "Processor account is missing or inactive", returncode=2
            ) from error
        if options["events"]:
            if (
                not options["configured"]
                or not options["capability"]
                or options["diagnose"]
            ):
                raise CommandError(
                    "Event mode requires configured capabilities", returncode=2
                )
            if not all(
                user.has_perm(f"documents.process_{capability}")
                for capability in options["capability"]
            ):
                raise CommandError(
                    "Processor lacks configured capability grants; provision its account",
                    returncode=2,
                )
            try:
                self.consume_events(user, options)
            except RedisError as error:
                raise CommandError(
                    "Processor Redis connection lost", returncode=3
                ) from error
            return
        # Task publication can close the connection; retain IDs, not a database cursor.
        identifiers = list(
            DocumentJob.objects.filter(status=DocumentJob.Status.WAITING).values_list(
                "pk", flat=True
            )
        )
        for identifier in identifiers:
            self.process_job(identifier, user, options)

    def process_job(self, identifier, user, options, key_filter=None) -> bool:
        """Recheck current state and grants before executing selected work."""
        worked = False
        job = DocumentJob.objects.filter(
            pk=identifier, status=DocumentJob.Status.WAITING
        ).first()
        if job is None:
            return False
        for key, entry in job.result.get("invocations", {}).items():
            if key_filter is not None and key != key_filter:
                continue
            if entry.get("status") != "pending":
                continue
            operation = entry.get("operation", "")
            capability = capability_for(operation)
            selected = (
                operation in options["operation"] or capability in options["capability"]
            ) and (options["configured"] or entry.get("plugin") in options["provider"])
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
                    worked = process(job.pk, key, user) or worked
            except PermissionDenied:
                if options["events"]:
                    raise
                self.stderr.write(
                    f"{job.pk} {key}: processor lacks {entry.get('policy', {}).get('permission', 'required permission')}; check account grants"
                )
            except Timeout:
                if options["events"]:
                    raise
                continue
            except Exception:
                # One unavailable endpoint must not strand unrelated operations.
                logger.exception(
                    "Processor failed for job %s operation %s; inspect Tasks",
                    job.pk,
                    key,
                )
                job.refresh_from_db(fields=["result"])
                worked = worked or job.result.get("invocations", {}).get(key, {}).get(
                    "attempts", 0
                ) > entry.get("attempts", 0)
        return worked

    def consume_events(self, user, options) -> None:
        """Reuse Django during a burst, sleeping on Redis between notifications."""
        from documents.jobs import recover_document_jobs

        try:
            quiet = idle_timeout()
        except ValueError as error:
            raise CommandError(str(error), returncode=2) from error
        queue = Queue(
            options["capability"], user=user.username, socket_timeout=quiet + 5
        )
        deadline = time.monotonic() + quiet
        notification = (
            json.loads(options["notification"]) if options["notification"] else None
        )
        try:
            if options["reconcile"]:
                recover_document_jobs(include_recent=True)
            while True:
                if notification is None:
                    notification = queue.pop(max(0, deadline - time.monotonic()))
                if notification is None:
                    return
                worked = False
                if notification.get("reconcile"):
                    recover_document_jobs(include_recent=True)
                else:
                    try:
                        worked = self.process_job(
                            notification["job_id"],
                            user,
                            options,
                            key_filter=notification["key"],
                        )
                    except Timeout:
                        # Retry only known busy work; never scan idle queues.
                        time.sleep(0.1)
                        queue.push(notification)
                        if quiet == 0:
                            return
                    except PermissionDenied as error:
                        queue.push(notification)
                        raise CommandError(
                            "Processor grant revoked; provision its account",
                            returncode=2,
                        ) from error
                notification = None
                if worked:
                    deadline = time.monotonic() + quiet
                elif quiet and time.monotonic() >= deadline:
                    return
        finally:
            queue.close()
