from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('delivery', '0006_teamrun_plan_teamrun_workflow')]
    operations = [
        migrations.AddField(
            model_name='teamrun', name='acceptance_cases',
            field=models.JSONField(default=list, editable=False),
        ),
    ]
