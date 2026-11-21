"""Presentation-only equivalence of completed, cross-checked findings."""
import hashlib
import json


def finding_groups(state):
    claims = {c.get('id'): c for a in state.get('analyses', []) if isinstance(a, dict)
              for c in a.get('claims', []) if isinstance(c, dict)}
    evidence = {e.get('id') for e in state.get('evidence', []) if isinstance(e, dict)}
    groups = {}
    for phase, record in [('review', state), ('extra_review', state.get('extra_round') or {})]:
        responses = {r.get('finding_id'): r for r in record.get('responses', []) if isinstance(r, dict)}
        for index, finding in enumerate(record.get('reviews', [])):
            if not isinstance(finding, dict):
                continue
            response = responses.get(finding.get('id'))
            claim = claims.get(finding.get('claim_id'))
            key = (phase, index)
            # Missing legacy fields cannot establish equivalence. Keep those
            # findings individually readable, even when the display text matches.
            if (claim and response and claim.get('author') == finding.get('author') == response.get('author')
                    and finding.get('id') in claim.get('verification_ids', [])
                    and {'text', 'proposal', 'metric', 'verification_status', 'evidence_ids'} <= claim.keys()
                    and {'reason', 'impact', 'categories', 'important', 'verdict', 'evidence_ids'} <= finding.keys()
                    and {'reason', 'decision', 'changed', 'new_evidence_ids', 'change_basis'} <= response.keys()
                    and all(ref in evidence for ref in (*claim['evidence_ids'], *finding['evidence_ids'], *response['new_evidence_ids']))):
                # Compare all semantic fields, including any future additions.
                # IDs below are links to the already-resolved records.
                values = [
                    {k:v for k,v in claim.items() if k not in ('id', 'author', 'verification_ids')},
                    {k:v for k,v in finding.items() if k not in ('id', 'author', 'claim_id', 'reviewer')},
                    {k:v for k,v in response.items() if k not in ('author', 'finding_id')},
                ]
                key = (phase, json.dumps(values, sort_keys=True, ensure_ascii=False))
            groups.setdefault(key, []).append((finding, response))
    return list(groups.values())


def matching_steps(state, steps):
    """A chat bubble may merge only when every entry has an equivalent checked counterpart."""
    grouped = {finding['id']: group[0][0]['id'] for group in finding_groups(state) if len(group) > 1
               for finding, _response in group}
    if not grouped:
        return {}
    keys = {}
    for step in steps:
        phase, _, part = step.role.partition('_')
        if step.status != 'completed' or phase not in ('review', 'response') or not part.startswith('p'):
            continue
        review = phase == 'review'
        rows = [row for row in state['reviews'] if row['reviewer' if review else 'author'] == part]
        if not rows or any(row['id'] not in grouped for row in rows):
            continue
        link = 'claim_id' if review else 'finding_id'
        normalized = {row['claim_id' if review else 'id']: grouped[row['id']] for row in rows}
        if len(normalized) != len(rows):  # Multiple findings on one claim need separate pairing.
            continue
        try:
            value = json.loads(step.output)
            entries = value['findings' if review else 'responses']
            if len(entries) != len(rows) or {item[link] for item in entries} != set(normalized):
                continue
            for item in entries:
                item[link] = normalized[item[link]]
        except (ValueError, KeyError, TypeError):
            continue
        # Keep any raw differences in scope, evidence, stance or response.
        data = json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
        keys[step.role] = hashlib.sha256(data).hexdigest()
    return keys
