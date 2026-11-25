from heapq import merge

from django.db import migrations
from django.db.models import Q

from workspace.memory_rules import candidates


def learn_existing_preferences(apps, schema_editor):
    alias = schema_editor.connection.alias
    messages = apps.get_model('workspace', 'Message').objects.using(alias)
    interjections = apps.get_model('delivery', 'TeamInterjection').objects.using(alias)
    memories = apps.get_model('workspace', 'Memory').objects.using(alias)
    disabled = apps.get_model('workspace', 'DataPolicy').objects.using(alias).filter(
        cross_chat_memory=False).values_list('user_id', flat=True)
    turns = messages.filter(role='user', superseded_by__isnull=True).exclude(conversation__owner_id__in=disabled)
    notes = interjections.filter(run__task__superseded_by_message__isnull=True).exclude(
        run__task__owner_id__in=disabled).exclude(status='forwarded')
    rows = merge(turns.order_by('created_at', 'id').values_list('created_at', 'id', 'conversation__owner_id',
                 'conversation_id', 'content').iterator(), notes.order_by('created_at', 'id').values_list(
                 'created_at', 'id', 'run__task__owner_id', 'run__task__conversation_id', 'content').iterator())
    for _, turn_id, owner_id, conversation_id, content in rows:
        for key, fact in candidates(content):
            existing = memories.filter(owner_id=owner_id)
            if existing.filter(Q(content=fact) | Q(auto_key=key), source_turn__isnull=True).exists():
                continue
            memories.update_or_create(owner_id=owner_id, auto_key=key, defaults={
                'content': fact, 'source_conversation_id': conversation_id, 'source_turn': turn_id})


class Migration(migrations.Migration):
    dependencies = [('workspace', '0010_personal_memory'), ('delivery', '0015_interjection_reply_to_step')]
    operations = [migrations.RunPython(learn_existing_preferences, migrations.RunPython.noop)]
