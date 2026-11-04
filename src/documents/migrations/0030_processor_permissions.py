"""Add processor capabilities without changing retained invocation grants."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("documents", "0029_deferred_operations")]

    operations = [
        migrations.AlterModelOptions(
            name="documentjob",
            options={
                "permissions": [
                    ("process_parser", "Can process external parser work"),
                    ("process_metadata", "Can process external metadata work"),
                    ("process_search", "Can process external search maintenance"),
                    ("process_ingestion", "Can process external ingestion extensions"),
                ]
            },
        ),
    ]
