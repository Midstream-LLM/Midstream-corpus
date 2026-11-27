"""Deterministic acceptance policy; model confidence remains explicitly uncalibrated."""
import json
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


FACTS = ('authors', 'contributors', 'composed_years', 'composed_label', 'edition_years', 'external_ids')


def supported(claim, evidence):
    """事实字段要有外部证据。只引用正文时，作者须在正文里逐字出现；外部编号须出现在检索结果里。"""
    if claim.kind != 'metadata' or claim.field not in FACTS:
        return True
    cited = [evidence[k] for k in claim.evidence_keys if k in evidence]
    outside = [e for e in cited if e.provider != 'corpus']
    found = json.dumps([e.model_dump() for e in outside], ensure_ascii=False)
    ids = list(claim.value.values()) if claim.field == 'external_ids' else [
        p['entity_id'] for p in claim.value if p.get('entity_id')] if claim.field == 'contributors' else []
    if any(i not in found for i in ids):
        return False
    if outside:
        return True
    if claim.field in ('authors', 'contributors'):
        names = claim.value if claim.field == 'authors' else [p['name'] for p in claim.value]
        text = ''.join(e.excerpt for e in cited)
        return bool(names) and all(name in text for name in names)
    return False


def merge_existing_tags(claims, metadata, overwrite):
    """标签按类别合并：已有类别保留原值，只补上原来没有的类别；overwrite 时整体采用模型结果。

    模型对已有类别的不同意见写进理由，不覆盖规则或人工打的标签。"""
    old = metadata.get('tags') or []
    if overwrite or not old:
        return claims
    have = {t['taxonomy'] for t in old}
    out = []
    for claim in claims:
        if claim.kind == 'metadata' and claim.field == 'tags':
            added = [t for t in claim.value if t['taxonomy'] not in have]
            pairs = lambda tags, tax: sorted(t['tag_id'] for t in tags if t['taxonomy'] == tax)
            differ = [tax for tax in sorted(have) if pairs(claim.value, tax) and pairs(claim.value, tax) != pairs(old, tax)]
            note = ('；已有类别未改，模型另提：' + '，'.join(f"{tax}={'/'.join(pairs(claim.value, tax))}" for tax in differ)) if differ else ''
            claim = claim.model_copy(update={'value': old + added, 'rationale': (claim.rationale + note)[:3000]})
        out.append(claim)
    return out


def decide(claims, metadata, evidence, config):
    known = {e.key for e in evidence}
    by_key = {e.key: e for e in evidence}
    values = defaultdict(set)
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
        elif (not claim.evidence_keys or not set(claim.evidence_keys) <= known
              or claim.confidence < config.confidence_threshold or not supported(claim, by_key)):
            status = 'withheld'
        elif claim.kind == 'text_patch' and not config.allow_text_edits:
            status = 'withheld'
        elif claim.kind == 'metadata':
            old = metadata.get(claim.field)
            if claim.field in ('composed_years', 'edition_years'):
                old = years(old)
            # 标签已按类别合并，只要原有标签都保留就不算覆盖。
            kept = claim.field == 'tags' and all(t in claim.value for t in old or [])
            if old not in (None, '', [], {}) and old != claim.value and not kept and not config.overwrite:
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
