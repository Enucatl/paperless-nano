from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [("documents", "0031_queued_operations")]

    operations = [
        migrations.AlterField(
            model_name="paperlesstask",
            name="task_type",
            field=models.CharField(
                choices=[
                    ("consume_file", "Consume File"),
                    ("train_classifier", "Train Classifier"),
                    ("sanity_check", "Sanity Check"),
                    ("index_optimize", "Index Optimize"),
                    ("mail_fetch", "Mail Fetch"),
                    ("llm_index", "LLM Index"),
                    ("empty_trash", "Empty Trash"),
                    ("check_workflows", "Check Workflows"),
                    ("bulk_update", "Bulk Update"),
                    ("reprocess_document", "Reprocess Document"),
                    ("build_share_link", "Build Share Link"),
                    ("bulk_delete", "Bulk Delete"),
                    ("apply_ai_suggestions", "Apply AI Suggestions"),
                    ("search_maintenance", "Search maintenance"),
                    ("metadata_classify", "Metadata classification"),
                ],
                db_index=True,
                help_text="The kind of work being performed",
                max_length=50,
                verbose_name="Task Type",
            ),
        ),
    ]
