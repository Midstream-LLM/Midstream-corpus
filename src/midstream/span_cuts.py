"""把当前比较表示中的重复证明映射到固定发布的原文裁剪。"""
from bisect import bisect_left
from functools import lru_cache

import midstream_dedup

from . import dedup, textcodec, variants
from .export import ExportError
from .query import where_clause


def merge(intervals):
    result = []
    for start, end in sorted(intervals):
        if result and start <= result[-1][1]:
            result[-1] = (result[-1][0], max(end, result[-1][1]))
        else:
            result.append((start, end))
    return result


def intersect(start, end, intervals):
    for left,right in intervals:
        if max(start,left) < min(end,right):
            yield max(start,left),min(end,right)


def assign(ordered, links, positions, min_cut_chars, minimums):
    """高优先级文档先确定去留；只裁有已保留出处证明的部分。"""
    kept, cuts, proven = [], {}, {}
    surviving = set()
    for item in ordered:
        intervals = []
        for better, better_start, this_start, length in links.get(item.version_id, ()):
            covered = [(better_start,better_start+length)] if better in surviving else \
                intersect(better_start,better_start+length,proven.get(better,()))
            for start,end in covered:
                intervals.append((this_start + start - better_start, this_start + end - better_start))
        raw, normalized = [], []
        if intervals:
            offsets = positions(item.version_id)
            for start, end in merge(intervals):
                left, right = offsets[start], offsets[end - 1] + 1
                if right - left >= min_cut_chars:
                    raw.append((left, right))
                    normalized.append((start, end))
        raw = merge(raw)
        remaining = item.char_count - sum(end-start for start,end in raw)
        if remaining < minimums.get(item.version_id, 1):
            proven[item.version_id] = normalized
            continue
        surviving.add(item.version_id)
        kept.append(item._replace(char_count=remaining))
        if raw:
            cuts[item.version_id] = raw
    return kept, cuts


def plan(conn, recipe, kept):
    snapshots = conn.execute("SELECT p.input_digest=d.input_digest FROM dedup_snapshot p CROSS JOIN dedup_snapshot d"
                             " WHERE p.level='paragraph' AND d.level='document'").fetchone()
    if snapshots is None or not snapshots[0]:
        raise ExportError('段落查重快照已过期，请先运行 midstream dedup --spans')
    if recipe.variant and not variants.info()['length_preserving']:
        raise ExportError('转换不等长，不能复用段落裁剪偏移')
    rank = {name: i for i,name in enumerate(recipe.selection.source_priority)}
    ordered = sorted(kept, key=lambda x: (rank.get(x.source,len(rank)), x.quality!='ok',-x.char_count,x.doc_id))
    order = {item.version_id:i for i,item in enumerate(ordered)}
    by_doc = {item.doc_id:item for item in ordered}
    canonical = {compare_hash:by_doc[doc_id] for doc_id,compare_hash in conn.execute(
        'SELECT v.doc_id,f.compare_hash FROM document_version v JOIN text_fingerprint f USING(text_hash)'
        ' WHERE v.is_current AND v.doc_id=ANY(%s) AND f.rule_version=%s', (list(by_doc),dedup.RULE_VERSION))}

    @lru_cache(maxsize=8)
    def normalized(version_id):
        blob=conn.execute('SELECT b.content FROM document_version v JOIN text_blob b USING(text_hash)'
                          ' WHERE v.version_id=%s',(version_id,)).fetchone()[0]
        return midstream_dedup.span_normalize(textcodec.decompress(bytes(blob)))

    links = {}
    with conn.cursor(name='selected_span_proofs') as cursor:
        cursor.execute(
            'SELECT d.version_id,d.start_char,d.end_char,d.source_version_id,d.source_start_char,'
            ' a.compare_hash,b.compare_hash FROM dup_span d'
            ' JOIN document_version va ON va.version_id=d.version_id'
            ' JOIN document_version vb ON vb.version_id=d.source_version_id'
            ' JOIN text_fingerprint a ON a.text_hash=va.text_hash AND a.rule_version=%s'
            ' JOIN text_fingerprint b ON b.text_hash=vb.text_hash AND b.rule_version=%s'
            ' WHERE a.compare_hash=ANY(%s) AND b.compare_hash=ANY(%s)'
            ' ORDER BY d.version_id,d.start_char', (dedup.RULE_VERSION,dedup.RULE_VERSION,list(canonical),list(canonical)))
        cursor.itersize=128
        for target,start,end,source,source_start,left_hash,right_hash in cursor:
            left,right=canonical[left_hash],canonical[right_hash]
            if left.version_id==right.version_id:
                continue
            target_positions=normalized(target)[1]
            target_start=bisect_left(target_positions,start)
            length=bisect_left(target_positions,end)-target_start
            right_start=bisect_left(normalized(source)[1],source_start)
            if order[left.version_id] < order[right.version_id]:
                better,worse,bs,ws=left,right,target_start,right_start
            else:
                better,worse,bs,ws=right,left,right_start,target_start
            links.setdefault(worse.version_id,[]).append((better.version_id,bs,ws,length))
    minimums = {}
    if any(f.min_chars for f in recipe.include):
        for filters in recipe.include:
            where,params=where_clause(filters)
            params['docs']=list(by_doc)
            for doc_id, in conn.execute('SELECT v.doc_id FROM document_version v JOIN document d USING(doc_id)'
                    ' JOIN source s USING(source_id) WHERE '+where+' AND v.doc_id=ANY(%(docs)s)',params):
                vid=by_doc[doc_id].version_id
                minimums[vid]=min(minimums.get(vid, filters.min_chars or 1),filters.min_chars or 1)
        for doc_id in recipe.documents.get('add',[]):
            if doc_id in by_doc:
                minimums[by_doc[doc_id].version_id]=1
    return assign(ordered,links,lambda vid:normalized(vid)[1],recipe.selection.min_cut_chars,minimums)


def apply(text, cuts):
    parts, start = [], 0
    for left,right in cuts:
        parts.append(text[start:left])
        start=right
    parts.append(text[start:])
    return ''.join(parts)


def load(conn, release_id):
    result={}
    for version,start,end in conn.execute('SELECT version_id,start_char,end_char FROM dataset_member_cut'
                                        ' WHERE release_id=%s ORDER BY version_id,start_char',(release_id,)):
        result.setdefault(version,[]).append((start,end))
    return result
