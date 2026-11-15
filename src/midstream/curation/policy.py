"""Deterministic acceptance policy; model confidence remains explicitly uncalibrated."""
from collections import defaultdict
from ..corpus.metadata import years
from .models import Claim


def step_for(claim):
    if claim.kind == 'text_patch' or claim.field == 'quality_flags':
        return 'text_cleanup'
    if claim.kind == 'segment':
        return 'segmentation'
    if claim.field in ('tags', 'genre', 'category', 'topics', 'tune_pattern'):
        return 'tagging'
    return 'metadata_enrichment'


def decide(claims, metadata, evidence, config):
    known = {e.key for e in evidence}
    values = defaultdict(set)
    import json
    for claim in claims:
        if claim.kind == 'metadata':
            values[claim.field].add(json.dumps(claim.value, sort_keys=True, ensure_ascii=False))
    decisions = []
    for claim in claims:
        if step_for(claim) not in config.steps:
            raise ValueError('模型返回了未请求步骤的结果')
        status = 'applied'
        if len(values.get(claim.field, ())) > 1:
            status = 'conflict'
        elif not claim.evidence_keys or not set(claim.evidence_keys) <= known or claim.confidence < config.confidence_threshold:
            status = 'withheld'
        elif claim.kind == 'text_patch' and not config.allow_text_edits:
            status = 'withheld'
        elif claim.kind == 'metadata':
            old = metadata.get(claim.field)
            if claim.field in ('composed_years', 'edition_years'):
                old = years(old)
            if old not in (None, '', [], {}) and old != claim.value and not config.overwrite:
                status = 'withheld'
            if claim.field == 'contributors' and metadata.get('authors') and not config.overwrite:
                authors = [p['name'] for p in claim.value if p['role'] == 'author'] or None
                if authors != metadata['authors']:
                    status = 'withheld'
        if not config.apply and status == 'applied':
            status = 'proposed'
        decisions.append(status)
    # Overlapping text edits are held as conflicts, never silently reordered.
    edits = [(i, c.value) for i, c in enumerate(claims) if c.kind == 'text_patch' and decisions[i] in ('applied', 'proposed')]
    for pos, (i, a) in enumerate(edits):
        for j, b in edits[pos+1:]:
            if max(a['start_char'], b['start_char']) < min(a['end_char'], b['end_char']):
                decisions[i] = decisions[j] = 'conflict'
    return decisions


def patched_text(text, claims):
    for claim in sorted(claims, key=lambda c: c.value['start_char'], reverse=True):
        p = claim.value
        if text[p['start_char']:p['end_char']] != p['before']:
            raise ValueError('补丁与输入原文不匹配')
        text = text[:p['start_char']] + p['after'] + text[p['end_char']:]
    return text
