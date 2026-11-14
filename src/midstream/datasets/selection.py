"""候选查询、去重选用、派生版选用和筛选分面。"""

from typing import NamedTuple

import psycopg

from ..corpus.query import where_clause
from .recipe import ExportError, Recipe, Selection


class Candidate(NamedTuple):
    version_id: int
    doc_id: int
    text_hash: bytes
    char_count: int
    source: str
    quality: str
    category: str | None
    year_from: int | None
    year_to: int | None


def _selection_where(recipe: Recipe):
    params = {}

    def combined(filters, prefix):
        parts = []
        for i, item in enumerate(filters):
            where, values = where_clause(item)
            for key, value in values.items():
                name = f'{prefix}{i}_{key}'
                where = where.replace(f'%({key})s', f'%({name})s')
                params[name] = value
            parts.append(f'({where})')
        return ' OR '.join(parts) or 'false'

    included = combined(recipe.include, 'i')
    excluded = combined(recipe.exclude, 'e')
    params.update(add=recipe.documents.get('add', []), remove=recipe.documents.get('remove', []))
    return (f'v.is_current AND ((({included}) AND NOT ({excluded}))'
            ' OR v.doc_id = ANY(%(add)s)) AND NOT (v.doc_id = ANY(%(remove)s))'), params


def _select(conn: psycopg.Connection, recipe: Recipe) -> list[Candidate]:
    where, params = _selection_where(recipe)
    selected = []
    with conn.cursor(name='plan_candidates') as cursor:
        cursor.execute(
            f"""SELECT v.version_id, v.doc_id, v.text_hash, v.char_count, s.name, v.quality_status,
                   v.category, lower(v.composed_years), upper(v.composed_years) - 1
            FROM document_version v
            JOIN document d USING (doc_id)
            JOIN source s USING (source_id)
            WHERE {where} ORDER BY v.version_id""",
            params,
        )
        while rows := cursor.fetchmany(10000):
            selected.extend(Candidate(*row) for row in rows)
    return selected


def _drop_duplicates(conn, selection: Selection, selected: list[Candidate]) -> list[Candidate]:
    """每个文档簇只留一篇：来源优先，其次质量为 ok、字数多，最后按 doc_id 固定。"""
    rank = {name: index for index, name in enumerate(selection.source_priority)}
    by_document = {item.doc_id: item for item in selected}
    clusters: dict[int, list[Candidate]] = {}
    for cluster_id, doc_id in conn.execute(
        """SELECT m.cluster_id, v.doc_id
           FROM dup_cluster_member m JOIN dup_cluster c USING (cluster_id)
           JOIN document_version v USING(version_id)
           WHERE c.level = 'document' AND v.is_current AND v.doc_id = ANY(%s)""",
        (list(by_document),),
    ):
        clusters.setdefault(cluster_id, []).append(by_document[doc_id])
    dropped = set()
    for members in clusters.values():
        keep = min(members, key=lambda item: (
            rank.get(item.source, len(rank)), item.quality != "ok", -item.char_count, item.doc_id))
        dropped.update(item.version_id for item in members if item is not keep)
    return [item for item in selected if item.version_id not in dropped]


def _variant_selection(conn, variant, selected):
    current = {item.doc_id: item.version_id for item in selected}
    chosen, existing = {}, set()
    for row in conn.execute(
        'SELECT v.version_id,v.doc_id,v.text_hash,v.char_count,s.name,v.quality_status,v.category,'
        ' lower(v.composed_years),upper(v.composed_years)-1,v.parent_version_id'
        ' FROM document_version v JOIN document d USING(doc_id) JOIN source s USING(source_id)'
        ' WHERE v.doc_id=ANY(%s) AND v.variant=%s', (list(current), variant)):
        existing.add(row[1])
        if row[-1] == current[row[1]]:
            chosen[row[1]] = Candidate(*row[:-1])
    missing = set(current) - chosen.keys()
    if missing:
        absent = sorted(missing-existing)[:5]
        stale = sorted(missing & existing)[:5]
        raise ExportError(f'缺少 {variant} 转换版 doc_id={absent}；已过期 doc_id={stale}，请运行 reprocess t2s-v1 --apply')
    return [chosen[item.doc_id] for item in selected]


def facets(conn, recipe):
    where, params = _selection_where(recipe)
    result = {name: [] for name in ('sources', 'categories', 'genres', 'eras', 'quality')}
    for name, value, documents, chars in conn.execute(
        'SELECT f.name, f.value, count(*), sum(v.char_count) FROM document_version v'
        ' JOIN document d USING (doc_id) JOIN source s USING (source_id)'
        " CROSS JOIN LATERAL (VALUES ('sources',s.name),('categories',v.category),"
        " ('genres',v.genre),('eras',v.composed_label),('quality',v.quality_status)) f(name,value)"
        f' WHERE {where} GROUP BY f.name, f.value ORDER BY f.name, f.value NULLS FIRST', params,
    ):
        result[name].append({'value': value, 'documents': documents, 'chars': int(chars)})
    return result
