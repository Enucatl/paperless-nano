"""Quarantine unfinished legacy work without replaying uncertain effects."""

from django.db import migrations


def quarantine_legacy(apps, schema_editor):
    """Preserve outcomes and require explicit review before upgrade recovery."""
    jobs = apps.get_model("documents", "DocumentJob")
    jobs.objects.exclude(status__in=["succeeded", "cancelled"]).update(
        status="attention",
        error="Legacy operation lifecycle: inspect retained outcomes, reconcile effects, then cancel and resubmit unfinished work.",
    )


class Migration(migrations.Migration):
    dependencies = [("documents", "0030_processor_permissions")]
    operations = [migrations.RunPython(quarantine_legacy, migrations.RunPython.noop)]
