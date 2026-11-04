"""Expose durable waiting and cancellation without replacing the job journal."""

from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [("documents", "0028_documentjob")]

    operations = [
        migrations.AlterField(
            model_name="documentjob",
            name="status",
            field=models.CharField(
                max_length=16,
                default="queued",
                choices=[
                    (value, value.capitalize())
                    for value in (
                        "staging",
                        "attention",
                        "queued",
                        "waiting",
                        "cancelled",
                        "running",
                        "prepared",
                        "committed",
                        "succeeded",
                        "failed",
                        "rejected",
                    )
                ],
            ),
        ),
    ]
