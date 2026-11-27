"""正文字形：繁体本还是简体本。

查重的比较表示不分繁简，同一部书的繁体本和简体本会互判重复。导出时按字形分开去重，两份都留。
繁体字指 OpenCC 繁转简字符表里会被改写的字；繁体字占汉字的比例达到 TRADITIONAL_RATIO 记为繁体。
计数按正文预先统计在 text_script，随文档查重增量更新；判断只用计数，阈值改了不必重算。
"""
import re
from functools import cache

from ..corpus import variants
from ..store import reader, textcodec

SCRIPT_RULE = 'script-v1'
TRADITIONAL_RATIO = 0.05
# 筛选条件用：与 classify 同一判断，表别名 v 为版本。
CLAUSE = ("EXISTS (SELECT 1 FROM text_script ts WHERE ts.text_hash = v.text_hash AND ts.rule_version = %(script_rule)s"
          " AND (CASE WHEN ts.han_chars > 0 AND ts.traditional_chars >= %(script_ratio)s * ts.han_chars"
          " THEN 'hant' ELSE 'hans' END) = ANY(%(scripts)s))")
NON_HAN = re.compile('[^㐀-䶿一-鿿豈-﫿\U00020000-\U0003ffff]+')


@cache
def _traditional():
    """删去全部繁体字的翻译表；映射到自身的字不算繁体。"""
    mapping = variants.table()[0]
    return {code: None for code, target in mapping.items() if target != chr(code)}


def counts(text):
    han = NON_HAN.sub('', text)
    return len(han), len(han) - len(han.translate(_traditional()))


def classify(han, traditional):
    if han is None:
        return None
    return 'hant' if han and traditional / han >= TRADITIONAL_RATIO else 'hans'


def describe():
    return {'rule': SCRIPT_RULE, 'traditional_ratio': TRADITIONAL_RATIO,
            'table_sha256': variants.info()['table_sha256']}


def refresh(conn, progress=None):
    """给当前版本里还没有统计（或规则已变）的正文补字形计数；返回新统计的条数。"""
    conn.execute('DELETE FROM text_script WHERE rule_version <> %s', (SCRIPT_RULE,))
    added = 0
    with reader(conn) as source, source.cursor(name='text_script_refresh') as rows, \
            conn.cursor() as cursor, cursor.copy(
                'COPY text_script (text_hash, rule_version, han_chars, traditional_chars) FROM STDIN') as copy:
        rows.itersize = 2000
        rows.execute("""SELECT b.text_hash, b.content FROM text_blob b
                        WHERE EXISTS (SELECT 1 FROM document_version v WHERE v.text_hash = b.text_hash AND v.is_current)
                          AND NOT EXISTS (SELECT 1 FROM text_script s WHERE s.text_hash = b.text_hash)""")
        for text_hash, content in rows:
            copy.write_row((text_hash, SCRIPT_RULE, *counts(textcodec.decompress(bytes(content)))))
            added += 1
            if progress and added % 100000 == 0:
                progress(0, 0, f'字形统计：已补 {added} 篇')
    conn.commit()
    return added
