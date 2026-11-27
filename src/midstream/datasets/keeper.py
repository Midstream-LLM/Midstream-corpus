"""重复时保留哪一份。

文档级去重在同一簇里选一篇，段落级去重决定共有段落留在哪一篇，两处用同一个排序，依次比较：
配方 source_priority → 质量状态为 ok → 来源档次（data/quality/source_tiers.json）
→ 缺字密度分档（每万字 □ 与 � 的个数）→ 字数多 → doc_id 小。
缺字数按正文预先统计在 text_quality，随文档查重增量更新；导出试算只读元数据。
"""
import json
from functools import cache
from pathlib import Path

from ..store import reader
from ..store import textcodec

KEEPER_RULE = 'keeper-v2'
MISSING_RULE = 'missing-v1'
MARKS = ('□', '�')
# 每万字缺字数的分档上限：0 档不到 1 个，1 档不到 10 个，2 档不到 100 个，其余 3 档。
DENSITY_LIMITS = (1, 10, 100)


@cache
def _tiers():
    data = json.loads((Path(__file__).parents[1] / 'data' / 'quality' / 'source_tiers.json').read_text(encoding='utf-8'))
    return data['version'], data['tiers'], data['default']


def describe():
    """写进发布报告，便于复现。"""
    return {'rule': KEEPER_RULE, 'source_tiers': _tiers()[0], 'missing_rule': MISSING_RULE,
            'density_limits_per_10k': list(DENSITY_LIMITS)}


def density_class(missing, chars):
    per_10k = missing * 10000 / chars if chars else 0
    return sum(per_10k >= limit for limit in DENSITY_LIMITS)


def sort_key(rank):
    _, tiers, default = _tiers()

    def key(item):
        return (rank.get(item.source, len(rank)), item.quality != 'ok', tiers.get(item.source, default),
                density_class(item.missing or 0, item.char_count), -item.char_count, item.doc_id)
    return key


def count_missing(text):
    return sum(text.count(mark) for mark in MARKS)


def refresh(conn, progress=None):
    """给当前版本里还没有统计（或规则已变）的正文补缺字数；返回新统计的条数。"""
    conn.execute('DELETE FROM text_quality WHERE rule_version <> %s', (MISSING_RULE,))
    added = 0
    with reader(conn) as source, source.cursor(name='text_quality_refresh') as rows, \
            conn.cursor() as cursor, cursor.copy(
                'COPY text_quality (text_hash, rule_version, missing_chars) FROM STDIN') as copy:
        rows.itersize = 2000
        rows.execute("""SELECT b.text_hash, b.content FROM text_blob b
                        WHERE EXISTS (SELECT 1 FROM document_version v WHERE v.text_hash = b.text_hash AND v.is_current)
                          AND NOT EXISTS (SELECT 1 FROM text_quality q WHERE q.text_hash = b.text_hash)""")
        for text_hash, content in rows:
            copy.write_row((text_hash, MISSING_RULE, count_missing(textcodec.decompress(bytes(content)))))
            added += 1
            if progress and added % 100000 == 0:
                progress(0, 0, f'缺字统计：已补 {added} 篇')
    conn.commit()
    return added
