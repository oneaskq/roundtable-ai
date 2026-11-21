import json

from django.db import migrations


def disable_legacy_evidence(apps, schema_editor):
    Evidence = apps.get_model('delivery', 'CapabilityEvidence')
    for record in Evidence.objects.filter(enabled=True).only('id', 'evidence').iterator():
        try:
            details = json.loads(record.evidence)
        except (TypeError, ValueError, RecursionError):
            details = None
        if not isinstance(details, dict) or details.get('streaming_tested') is not True:
            Evidence.objects.filter(pk=record.pk, enabled=True).update(enabled=False)


class Migration(migrations.Migration):
    dependencies = [('delivery', '0010_qualityround_qualitysample')]
    operations = [migrations.RunPython(disable_legacy_evidence, migrations.RunPython.noop)]
