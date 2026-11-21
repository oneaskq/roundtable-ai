from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [('delivery', '0013_agentstep_public_order_teaminterjection_public_order_and_more')]

    operations = [
        migrations.AddField(
            model_name='teamrun', name='decision_state',
            field=models.JSONField(default=dict, editable=False),
        ),
    ]
