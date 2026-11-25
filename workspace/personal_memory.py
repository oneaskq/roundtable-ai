from contextlib import contextmanager
from contextvars import ContextVar
from heapq import nlargest
import re

from django.db.models import Q

from .memory_rules import ABOUT, candidates, relevance, terms, topics
from .models import DataPolicy, Memory


active_memories = ContextVar('roundtable_personal_memories', default=())


def enabled(owner_id):
    return not DataPolicy.objects.filter(user_id=owner_id, cross_chat_memory=False).exists()


def learn_turn(conversation, turn):
    facts = list(candidates(turn.content))
    if not facts or not enabled(conversation.owner_id):
        return
    notes = Memory.objects.filter(owner_id=conversation.owner_id)
    for key, fact in facts:
        # A hand-corrected note keeps its key and takes precedence over later copies.
        if notes.filter(Q(auto_key=key) | Q(content=fact), source_turn__isnull=True).exists():
            continue
        notes.update_or_create(owner_id=conversation.owner_id, auto_key=key,
                               defaults={'content': fact, 'source_conversation': conversation, 'source_turn': turn.pk})


def recall(owner_id, query):
    if not query.strip() or not enabled(owner_id):
        return ()
    query_terms, query_topics, about = terms(query), topics(query), bool(ABOUT.search(query))

    def ranked():
        for index, note in enumerate(Memory.objects.filter(owner_id=owner_id).order_by('-updated_at', '-id')
                                     .values_list('content', flat=True).iterator()):
            # For long hand-written notes, select a complete relevant sentence.
            parts = [note] if len(note) <= 320 else re.split(r'(?<=[。！？.!?])\s*|\n+', note)
            excerpts = [(relevance(text, query_terms, query_topics, about), text)
                        for part in parts if 0 < len(text := part.strip()) <= 320]
            if excerpts:
                score, excerpt = max(excerpts, key=lambda row: row[0])
                if score:
                    yield score, -index, excerpt

    return tuple(item[2] for item in nlargest(4, ranked()))


@contextmanager
def memory_context(owner_id, query):
    token = active_memories.set(recall(owner_id, query))
    try:
        yield
    finally:
        active_memories.reset(token)
