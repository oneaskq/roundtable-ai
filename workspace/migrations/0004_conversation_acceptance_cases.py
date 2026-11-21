from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('workspace', '0003_artifact_files_artifact_kind_artifact_review_status_and_more'),
    ]
    operations = [
        migrations.AddField(
            model_name='conversation', name='acceptance_cases',
            field=models.JSONField(default=list),
        ),
    ]
