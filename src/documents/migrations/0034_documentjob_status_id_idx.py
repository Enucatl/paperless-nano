from django.db import migrations
from django.db import models


class Migration(migrations.Migration):
    dependencies = [("documents", "0033_document_language")]
    operations = [
        migrations.AddIndex(
            model_name="documentjob",
            index=models.Index(
                fields=["status", "id"], name="documentjob_status_id_idx"
            ),
        ),
    ]
