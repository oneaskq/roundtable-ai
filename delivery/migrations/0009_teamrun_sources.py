from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('delivery', '0008_teamrun_background_teamrun_client_request_id_and_more')]

    operations = [
        migrations.AddField(model_name='teamrun', name='source_revision',
                            field=models.CharField(blank=True, editable=False, max_length=64)),
        migrations.AddField(model_name='teamrun', name='source_snapshot',
                            field=models.JSONField(default=list, editable=False)),
    ]
