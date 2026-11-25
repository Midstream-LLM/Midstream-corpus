"""净增量预估：批次发布前，抽样与库内当前版本比对，估算去重后能新增多少字。

与段落查重同一套比较表示和 64 字下限；只读，不写查重结果。样本内部、样本与批次其余部分的重复不计入。
"""

import hashlib

import midstream_dedup

from .. import store
from .engine import _read

MIN_CHARS = 64


def _sample(conn, batch_id, sample_chars, seed):
    """按 (种子, source_key) 的哈希排序取整篇，直到字数达到上限；第一篇过长时只取开头。"""
    rows = conn.execute(
        "SELECT source_key, char_count FROM staging_text WHERE batch_id = %s AND step = 'cleaned'",
        (batch_id,)).fetchall()
    if not rows:
        raise ValueError('批次没有已清洗的暂存正文，先完成暂存')
    order = sorted(rows, key=lambda row: hashlib.sha256(f'{seed}:{row[0]}'.encode()).digest())
    picked, total = [], 0
    for key, chars in order:
        if picked and total + chars > sample_chars:
            continue
        picked.append(key)
        total += chars
        if total >= sample_chars:
            break
    return rows, picked


def estimate(conn, batch_id, *, sample_chars=5_000_000, seed='estimate-v1', progress=None):
    rows, picked = _sample(conn, batch_id, sample_chars, seed)
    probe = midstream_dedup.CoverageProbe()
    texts = {}
    # 清洗版与原始版相同时清洗行的 content 为 NULL，取原始版。
    for key, text in conn.execute(
            "SELECT c.source_key, coalesce(c.content, d.content) FROM staging_text c"
            " JOIN staging_text d ON d.batch_id = c.batch_id AND d.source_key = c.source_key AND d.step = 'decoded'"
            " WHERE c.batch_id = %s AND c.step = 'cleaned' AND c.source_key = ANY(%s)", (batch_id, picked)):
        texts[key] = (text or '')[:sample_chars]
    keys = [key for key in picked if key in texts]
    for key in keys:
        probe.add(texts[key])
    if progress:
        progress(0, 1, '净增量预估：扫描库内正文')
    with store.reader(conn) as source:
        source.execute('SET TRANSACTION READ ONLY')
        _read(source,
              """COPY (SELECT v.version_id, b.content FROM document_version v JOIN text_blob b USING (text_hash)
                 WHERE v.is_current AND v.char_count >= %s) TO STDOUT (FORMAT BINARY)""",
              (MIN_CHARS,), probe.scan)
    results = probe.result()
    sample_total = sum(len(texts[key]) for key in keys)
    covered = sum(raw for raw, _ in results)
    by_version = {}
    for _, items in results:
        for version, chars in items:
            by_version[version] = by_version.get(version, 0) + chars
    by_source, top = {}, []
    if by_version:
        info = {row[0]: row[1:] for row in conn.execute(
            'SELECT v.version_id, s.name, v.doc_id, v.title FROM document_version v'
            ' JOIN document d USING (doc_id) JOIN source s USING (source_id) WHERE v.version_id = ANY(%s)',
            (list(by_version),))}
        for version, chars in by_version.items():
            by_source[info[version][0]] = by_source.get(info[version][0], 0) + chars
        top = [{'doc_id': info[v][1], 'title': info[v][2], 'source': info[v][0], 'chars': c}
               for v, c in sorted(by_version.items(), key=lambda item: -item[1])[:10]]
    ratios = [raw / len(texts[key]) if texts[key] else 0 for key, (raw, _) in zip(keys, results)]
    batch_chars = sum(chars for _, chars in rows)
    ratio = covered / sample_total if sample_total else 0
    normalized = sum(by_source.values()) or 1
    return {
        'batch_id': batch_id, 'seed': seed, 'min_chars': MIN_CHARS,
        'batch_documents': len(rows), 'batch_chars': batch_chars,
        'sample_documents': len(keys), 'sample_chars': sample_total,
        'covered_chars': covered, 'overlap_ratio': ratio,
        'estimated_new_chars': round(batch_chars * (1 - ratio)),
        'sample_distribution': {
            'mostly_duplicate': sum(r >= 0.9 for r in ratios),
            'partly_duplicate': sum(0.1 <= r < 0.9 for r in ratios),
            'mostly_new': sum(r < 0.1 for r in ratios)},
        # 按来源分摊的是规范化后的字数比例，换算到原文覆盖字数。
        'by_source': sorted(({'source': name, 'chars': round(covered * chars / normalized)}
                             for name, chars in by_source.items()), key=lambda item: -item['chars']),
        'top_documents': top,
    }
