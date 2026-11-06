"""Provision a non-login account with only explicit processor capabilities."""

from django.contrib.auth.models import Permission
from django.contrib.auth.models import User
from django.core.management.base import BaseCommand
from django.core.management.base import CommandError
from django.db import transaction

from documents.processor_notifications import wake_processor
from documents.server_processor import CAPABILITIES


class Command(BaseCommand):
    """Create or update a dedicated capability-only server account."""

    help = (
        "Provision an unusable-password processor account with explicit capabilities."
    )

    def add_arguments(self, parser) -> None:
        """Require the account name and exact desired grants."""
        parser.add_argument("--user", required=True)
        parser.add_argument(
            "--capability", action="append", choices=CAPABILITIES, required=True
        )

    @transaction.atomic
    def handle(self, *args, **options) -> None:
        """Refuse to repurpose a login, administrative or disabled account."""
        user, created = User.objects.get_or_create(username=options["user"])
        if created:
            user.set_unusable_password()
            user.save(update_fields=["password"])
        elif (
            user.has_usable_password()
            or user.is_staff
            or user.is_superuser
            or not user.is_active
            or user.groups.exists()
            or user.user_permissions.exclude(
                content_type__app_label="documents",
                codename__in=[f"process_{name}" for name in CAPABILITIES],
            ).exists()
        ):
            raise CommandError(
                "Choose a dedicated active processor account with no login, groups or other permissions"
            )
        permissions = Permission.objects.filter(
            content_type__app_label="documents",
            codename__in=[f"process_{name}" for name in set(options["capability"])],
        )
        if permissions.count() != len(set(options["capability"])):
            raise CommandError("Processor permissions are missing; run migrate first")
        user.user_permissions.set(permissions)
        wake_processor(user.username)
        self.stdout.write(f"Processor account {user.username} configured")
