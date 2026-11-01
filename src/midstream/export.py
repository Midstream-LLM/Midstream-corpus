"""数据集导出：先固定成员，再生成文件。

成员、顺序和划分写入数据库后不再变化，文件可以删除后重建。
验证集按组划分：同一作品或同一重复簇的文档落在同一侧。
"""

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Annotated, Literal, NamedTuple

import psycopg
from pydantic import BaseModel, ConfigDict, Field
from psycopg.types.json import Jsonb

from . import dedup, textcodec
from .query import Filters, where_clause

SPLIT_NAMES = ("test", "train", "val")


class ExportError(Exception):
    pass


class TrainingLeak(ExportError):
    pass


class _Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class _ReservedRelease(_Input):
    release_id: int = Field(gt=0)
    split: Literal['train', 'val', 'test']


class _Reserved(_Input):
    doc_ids: list[Annotated[int, Field(gt=0)]] = Field(default_factory=list)
    releases: list[_ReservedRelease] = Field(default_factory=list)


class _Limit(_Input):
    max_chars: int | None = Field(default=None, ge=0)
    max_documents: int | None = Field(default=None, ge=0)


class _RecipeInput(_Input):
    name: str = Field(min_length=1, max_length=200, pattern=r'^[^/\\\x00]+$')
    seed: str
    splits: dict[Literal['train', 'val', 'test'], Annotated[float, Field(ge=0, le=1)]]
    filters: dict = Field(default_factory=dict)
    selection: dict = Field(default_factory=dict)
    include: list[dict] | None = None
    exclude: list[dict] = Field(default_factory=list)
    documents: dict[Literal['add', 'remove'], list[Annotated[int, Field(gt=0)]]] = Field(default_factory=dict)
    split_by: Literal['chars', 'documents'] = 'chars'
    stratify: list[Literal['source', 'category']] = Field(default_factory=list)
    limits: dict[Literal['train', 'val', 'test'], _Limit] = Field(default_factory=dict)
    reserved: dict[Literal['val', 'test'], _Reserved] = Field(default_factory=dict)


@dataclass(frozen=True)
class Selection:
    """重复文本的选用规则；默认不去重，保留筛选结果的全部文档。"""

    dedup: str = "none"
    source_priority: tuple[str, ...] = ()

    @classmethod
    def load(cls, data: dict) -> "Selection":
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ExportError(f"未知的选用字段：{sorted(unknown)}")
        selection = cls(data.get("dedup", "none"), tuple(data.get("source_priority", ())))
        if selection.dedup not in ("none", "document"):
            raise ExportError(f"未知的去重方式：{selection.dedup}")
        return selection


@dataclass(frozen=True)
class Recipe:
    name: str
    filters: Filters
    splits: dict[str, float]
    seed: str
    raw: dict
    selection: Selection = Selection()
    include: tuple[Filters, ...] = ()
    exclude: tuple[Filters, ...] = ()
    documents: dict = field(default_factory=dict)
    split_by: str = 'chars'
    stratify: tuple[str, ...] = ()
    limits: dict = field(default_factory=dict)
    reserved: dict = field(default_factory=dict)

    @classmethod
    def load(cls, data: dict) -> "Recipe":
        body = _RecipeInput.model_validate(data)
        splits = body.splits
        if abs(sum(splits.values()) - 1) > 1e-9:
            raise ExportError("划分比例之和必须为 1")
        if body.name in ('.', '..'):
            raise ExportError('数据集名不能是路径')
        filters = Filters.from_dict({"quality": ["ok"], **body.filters})
        includes = (filters,) if body.include is None else tuple(
            Filters.from_dict({'quality': ['ok'], **item}) for item in body.include)
        excludes = tuple(Filters.from_dict({'quality': ['ok', 'flagged', 'quarantined'], **item})
                         for item in body.exclude)
        return cls(body.name, filters, splits, body.seed, data, Selection.load(body.selection),
                   includes, excludes, body.documents, body.split_by, tuple(body.stratify),
                   {key: value.model_dump(exclude_none=True) for key, value in body.limits.items()},
                   {key: value.model_dump() for key, value in body.reserved.items()})


class _Groups:
    """整数文档节点，组标识始终是组内最小 doc_id。"""

    def __init__(self) -> None:
        self.parent: dict[int, int] = {}

    def find(self, node: int) -> int:
        parent = self.parent.setdefault(node, node)
        while parent != self.parent[parent]:
            self.parent[parent] = self.parent[self.parent[parent]]
            parent = self.parent[parent]
        self.parent[node] = parent
        return parent

    def union(self, a: int, b: int) -> None:
        a, b = self.find(a), self.find(b)
        self.parent[max(a, b)] = min(a, b)


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
    if not dedup.document_snapshot_current(conn):
        raise ExportError("文档级查重结果已过期，请先运行 midstream dedup")
    rank = {name: index for index, name in enumerate(selection.source_priority)}
    by_version = {item.version_id: item for item in selected}
    clusters: dict[int, list[Candidate]] = {}
    for cluster_id, version_id in conn.execute(
        """SELECT m.cluster_id, m.version_id
           FROM dup_cluster_member m JOIN dup_cluster c USING (cluster_id)
           WHERE c.level = 'document' AND m.version_id = ANY(%s)""",
        (list(by_version),),
    ):
        clusters.setdefault(cluster_id, []).append(by_version[version_id])
    dropped = set()
    for members in clusters.values():
        keep = min(members, key=lambda item: (
            rank.get(item.source, len(rank)), item.quality != "ok", -item.char_count, item.doc_id))
        dropped.update(item.version_id for item in members if item is not keep)
    return [item for item in selected if item.version_id not in dropped]


def _group_keys(conn: psycopg.Connection, doc_ids: list[int]) -> dict[int, int]:
    """返回 doc_id 到组标识（组内最小 doc_id）的映射。"""
    groups = _Groups()
    for doc_id in doc_ids:
        groups.find(doc_id)
    queries = [
        "SELECT doc_id, work_id FROM document_work WHERE status <> 'rejected' AND doc_id = ANY(%s)",
        """SELECT v.doc_id, m.cluster_id
           FROM dup_cluster_member m
           JOIN dup_cluster c USING (cluster_id)
           JOIN document_version v USING (version_id)
           WHERE c.level = 'document' AND v.doc_id = ANY(%s)""",
    ]
    for query in queries:
        first = {}
        with conn.cursor(name='plan_links') as cursor:
            cursor.execute(query, (doc_ids,))
            for doc_id, link in cursor:
                groups.union(doc_id, first.setdefault(link, doc_id))
    return {doc_id: groups.find(doc_id) for doc_id in doc_ids}


def _fraction(seed: str, value: int) -> float:
    digest = hashlib.sha256(f"{seed}:{value}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _reserved_docs(conn, recipe):
    reserved = {}
    for target, spec in recipe.reserved.items():
        docs = set(spec['doc_ids'])
        for release in spec['releases']:
            if not conn.execute('SELECT 1 FROM dataset_release WHERE release_id = %s',
                                (release['release_id'],)).fetchone():
                raise ExportError(f"保留划分的发布 {release['release_id']} 不存在")
            docs.update(row[0] for row in conn.execute(
                'SELECT v.doc_id FROM dataset_member m JOIN document_version v USING (version_id)'
                ' WHERE m.release_id = %s AND m.split = %s',
                (release['release_id'], release['split'])))
        for doc_id in docs:
            if doc_id in reserved and reserved[doc_id] != target:
                raise ExportError('同一文档不能同时保留到 val 和 test')
            reserved[doc_id] = target
    return reserved


def _assign_splits(recipe, kept, groups, reserved):
    grouped = {}
    cross_layer = set()
    for item in kept:
        group = groups[item.doc_id]
        layer = tuple(getattr(item, name) for name in recipe.stratify)
        if group not in grouped:
            grouped[group] = [0, 0, item.doc_id, layer]
        total = grouped[group]
        total[0] += item.char_count
        total[1] += 1
        if layer != total[3]:
            cross_layer.add(group)
        if item.doc_id < total[2]:
            total[2], total[3] = item.doc_id, layer
    forced = {}
    for doc_id, split in reserved.items():
        group = groups[doc_id]
        if group in forced and forced[group] != split:
            raise ExportError('同一作品或重复组不能同时保留到 val 和 test')
        forced[group] = split
    layers = {}
    for group, total in grouped.items():
        layers.setdefault(total[3], []).append(group)
    used = {name: [0, 0] for name in SPLIT_NAMES}
    assigned, oversized = {}, 0
    weight_index = 0 if recipe.split_by == 'chars' else 1

    def fits_limit(split, total):
        limit = recipe.limits.get(split, {})
        return all(used[split][i] + total[i] <= limit[key]
                   for i, key in enumerate(('max_chars', 'max_documents')) if key in limit)

    for group in sorted(forced):
        if group not in grouped:
            continue
        split, total = forced[group], grouped[group]
        if not fits_limit(split, total):
            raise ExportError(f'保留组超过 {split} 的 limits')
        assigned[group] = split
        used[split][0] += total[0]
        used[split][1] += total[1]
    for layer in sorted(layers, key=lambda value: tuple((x is not None, x or '') for x in value)):
        layer_groups = layers[layer]
        weight = sum(grouped[group][weight_index] for group in layer_groups)
        targets = {split: weight * ratio for split, ratio in recipe.splits.items() if ratio > 0}
        allocated = {name: 0 for name in SPLIT_NAMES}
        ordered = sorted(layer_groups, key=lambda group: (_fraction(recipe.seed, group), group))
        for group in layer_groups:
            if group in assigned:
                allocated[assigned[group]] += grouped[group][weight_index]
        for group in ordered:
            if group in assigned:
                continue
            total = grouped[group]
            size = total[weight_index]
            held_out = [targets[s] for s in ('val', 'test') if s in targets]
            if held_out and size > min(held_out):
                oversized += 1
            choices = [split for split in SPLIT_NAMES if split in targets and fits_limit(split, total)
                       and (split == 'train' or size <= targets[split] - allocated[split] + 1e-9)]
            if not choices:
                raise ExportError('整组无法放入任何划分；请调整配比或 limits')
            split = max(choices, key=lambda name: (targets[name] - allocated[name]) / targets[name])
            assigned[group] = split
            allocated[split] += size
            used[split][0] += total[0]
            used[split][1] += total[1]
    warnings = []
    if oversized:
        warnings.append(f'{oversized} 个超大组超过所在层的 val/test 目标容量，整组保留')
    if cross_layer:
        warnings.append(f'{len(cross_layer)} 个组跨分层，以组内最小 doc_id 的来源/类别归层')
    return assigned, used, warnings


def _check_training_history(conn: psycopg.Connection, held_out: list[int]) -> None:
    """进过训练的文档不能进验证或测试，经文档、作品和重复簇反查。"""
    if not held_out:
        return
    leaked = conn.execute(
        """WITH trained AS (
               SELECT DISTINCT v.doc_id
               FROM training_use u
               JOIN dataset_member m ON m.release_id = u.release_id AND m.split = 'train'
               JOIN document_version v USING (version_id)
           ), trained_clusters AS (
               SELECT DISTINCT m.cluster_id FROM trained t
               JOIN document_version v ON v.doc_id = t.doc_id
               JOIN dup_cluster_member m USING (version_id)
               JOIN dup_cluster c USING (cluster_id) WHERE c.level = 'document'
           )
           SELECT h.doc_id FROM unnest(%s::bigint[]) AS h(doc_id)
           WHERE h.doc_id IN (SELECT doc_id FROM trained)
              OR EXISTS (
                   SELECT 1 FROM document_work a
                   JOIN document_work b USING (work_id)
                   JOIN trained t ON t.doc_id = b.doc_id
                   WHERE a.doc_id = h.doc_id
                     AND a.status <> 'rejected' AND b.status <> 'rejected')
              OR EXISTS (
                   SELECT 1 FROM document_version v JOIN dup_cluster_member m USING (version_id)
                   JOIN trained_clusters c USING (cluster_id) WHERE v.doc_id = h.doc_id)
           LIMIT 5""",
        (held_out,),
    ).fetchall()
    if leaked:
        raise TrainingLeak(f"验证或测试集包含已训练过的文档，例如 {[row[0] for row in leaked]}")


def _digest(members: list[tuple[int, int, str, bytes]]) -> bytes:
    digest = hashlib.sha256()
    for ordinal, version_id, split, text_hash in members:
        digest.update(f"{ordinal}:{version_id}:{split}:{text_hash.hex()}\n".encode())
    return digest.digest()


@dataclass
class ReleasePlan:
    recipe: Recipe
    members: list[tuple[int, int, str, bytes]]
    report: dict


def _distributions(kept, groups, assigned):
    totals = {'source': {}, 'category': {}, 'century': {}}
    for item in kept:
        split = assigned[groups[item.doc_id]]
        centuries = [None]
        if item.year_from is not None and item.year_to is not None:
            first = (item.year_from - 1) // 100 if item.year_from > 0 else item.year_from // 100
            last = (item.year_to - 1) // 100 if item.year_to > 0 else item.year_to // 100
            centuries = list(range(first, last + 1))
        for dimension, values in (('source', [item.source]), ('category', [item.category]),
                                   ('century', centuries)):
            for value in values:
                counts = totals[dimension].setdefault((value, split), [0, 0])
                counts[0] += 1
                counts[1] += item.char_count
    return {dimension: [{'value': value, 'split': split, 'documents': counts[0], 'chars': counts[1]}
                        for (value, split), counts in sorted(items.items(), key=lambda x: (
                            x[0][0] is not None, str(x[0][0]), x[0][1]))]
            for dimension, items in totals.items()}


def plan_release(conn: psycopg.Connection, recipe: Recipe, *, tokenizer=None) -> ReleasePlan:
    """仅用元数据试算；不生成发布、文件或正文解压。"""
    selected = _select(conn, recipe)
    if not selected:
        raise ExportError("配方没有选中任何文档")
    # 分组用去重前的全集：被舍弃的文档仍可能连接作品和重复簇。
    reserved = _reserved_docs(conn, recipe)
    groups = _group_keys(conn, list({item.doc_id for item in selected} | reserved.keys()))
    kept = selected
    if recipe.selection.dedup == "document":
        kept = _drop_duplicates(conn, recipe.selection, selected)
    assigned, totals, warnings = _assign_splits(recipe, kept, groups, reserved)
    _check_training_history(
        conn, [doc_id for doc_id, group in groups.items() if assigned.get(group, 'train') != 'train']
    )
    ordered = sorted(kept, key=lambda item: (_fraction(recipe.seed + ':order', item.version_id), item.version_id))
    members = [
        (ordinal, item.version_id, assigned[groups[item.doc_id]], item.text_hash)
        for ordinal, item in enumerate(ordered)
    ]
    selection_report = {
        "candidates": len(selected),
        "dropped_documents": len(selected) - len(kept),
        "dropped_chars": sum(item.char_count for item in selected) - sum(x[0] for x in totals.values()),
    }
    chars = sum(x[0] for x in totals.values())
    splits = {}
    for split, (nchars, ndocs) in totals.items():
        ratio = nchars / chars if recipe.split_by == 'chars' else ndocs / len(kept)
        splits[split] = {'documents': ndocs, 'chars': nchars, 'ratio': ratio,
                         'document_ratio': ndocs / len(kept), 'char_ratio': nchars / chars}
        if abs(ratio - recipe.splits.get(split, 0)) > .01:
            warnings.append(f'{split} 实际比例 {ratio:.2%}，与目标偏差超过 1 个百分点')
    plan = ReleasePlan(recipe, members, {
        **selection_report, 'documents': len(kept), 'chars': chars, 'split_by': recipe.split_by,
        'splits': splits, 'distributions': _distributions(kept, groups, assigned), 'warnings': warnings,
    })
    if tokenizer is not None:
        from . import token_stats
        plan.report['token_stats'] = token_stats.estimate(conn, kept, groups, assigned, tokenizer)
    return plan


def write_release(conn: psycopg.Connection, plan: ReleasePlan) -> int:
    recipe, report = plan.recipe, plan.report
    char_counts = {name: value['chars'] for name, value in report['splits'].items()}
    selection_report = {key: report[key] for key in ('candidates', 'dropped_documents', 'dropped_chars')}
    release_id = conn.execute(
        "INSERT INTO dataset_release (name, recipe, content_digest, char_count,"
        " train_char_count, val_char_count, test_char_count, selection_report)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
        " RETURNING release_id",
        (recipe.name, json.dumps(recipe.raw, ensure_ascii=False), _digest(plan.members),
         sum(char_counts.values()), char_counts["train"], char_counts["val"], char_counts["test"],
         json.dumps(selection_report)),
    ).fetchone()[0]
    with conn.cursor() as cursor, cursor.copy(
        "COPY dataset_member (release_id, version_id, ordinal, split) FROM STDIN"
    ) as copy:
        for ordinal, version_id, split, _ in plan.members:
            copy.write_row((release_id, version_id, ordinal, split))
    conn.commit()
    return release_id


def create_release(conn: psycopg.Connection, recipe: Recipe) -> int:
    return write_release(conn, plan_release(conn, recipe))


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


def list_releases(conn, *, limit=30, before=None):
    return [row[0] for row in conn.execute(
        'SELECT to_jsonb(r) - \'content_digest\' || jsonb_build_object(\'content_digest\','
        " encode(content_digest, 'hex'), 'token_stats', coalesce((SELECT jsonb_agg("
        " jsonb_build_object('tokenizer_name',t.tokenizer_name,'tokenizer_fingerprint',"
        " encode(t.tokenizer_fingerprint,'hex'),'total',t.total) ORDER BY t.created_at)"
        " FROM release_token_stats t WHERE t.release_id=r.release_id AND t.unit='document'), '[]'::jsonb))"
        " FROM dataset_release r"
        ' WHERE (%s::bigint IS NULL OR release_id < %s) ORDER BY release_id DESC LIMIT %s',
        (before, before, limit))]


def get_release(conn, release_id):
    rows = list_releases(conn, limit=1, before=release_id + 1)
    if not rows or rows[0]['release_id'] != release_id:
        from .management import ManagementError
        raise ManagementError('not_found', f'数据集 {release_id} 不存在')
    result = rows[0]
    result.pop('token_stats')
    result['splits'] = {split: {'documents': count, 'chars': int(chars)}
                        for split, count, chars in conn.execute(
                            'SELECT split, count(*), sum(v.char_count) FROM dataset_member m'
                            ' JOIN document_version v USING (version_id)'
                            ' WHERE release_id = %s GROUP BY split', (release_id,))}
    return result


def list_recipes(conn):
    return [row[0] for row in conn.execute('SELECT to_jsonb(r) FROM dataset_recipe r ORDER BY name')]


def get_recipe(conn, name):
    from .management import ManagementError
    row = conn.execute('SELECT to_jsonb(r) FROM dataset_recipe r WHERE name = %s', (name,)).fetchone()
    if row is None:
        raise ManagementError('not_found', f'配方 {name} 不存在')
    return row[0]


def save_recipe(conn, recipe, *, previous=None):
    from .management import ManagementError
    if previous is None:
        row = conn.execute('INSERT INTO dataset_recipe (name, body) VALUES (%s, %s)'
                           ' ON CONFLICT DO NOTHING RETURNING name', (recipe.name, Jsonb(recipe.raw))).fetchone()
    else:
        row = conn.execute('UPDATE dataset_recipe SET name = %s, body = %s, updated_at = now()'
                           ' WHERE name = %s RETURNING name', (recipe.name, Jsonb(recipe.raw), previous)).fetchone()
    if row is None:
        raise ManagementError('conflict', '配方已存在或待修改配方不存在')
    conn.commit()
    return get_recipe(conn, recipe.name)


def delete_recipe(conn, name):
    from .management import ManagementError
    if not conn.execute('DELETE FROM dataset_recipe WHERE name = %s RETURNING name', (name,)).fetchone():
        raise ManagementError('not_found', f'配方 {name} 不存在')
    conn.commit()
    return {'name': name, 'deleted': True}


def release_stats(conn: psycopg.Connection, release_id: int) -> dict:
    """读取发布时固定的字符数和选用结果；空划分为 0，旧发布没有选用结果。"""
    row = conn.execute(
        "SELECT name, char_count, train_char_count, val_char_count, test_char_count,"
        " selection_report FROM dataset_release WHERE release_id = %s", (release_id,),
    ).fetchone()
    if row is None:
        raise ExportError(f"数据集 {release_id} 不存在")
    return {
        "release_id": release_id,
        "name": row[0],
        "char_count": row[1],
        "split_char_counts": {"train": row[2], "val": row[3], "test": row[4]},
        "selection": row[5],
    }


def write_parquet(conn: psycopg.Connection, release_id: int, out_dir: Path) -> list[Path]:
    """兼容库入口；每次导出仍使用独立 export_id 目录。"""
    from . import exporters
    result = exporters.export_release(conn, release_id, directory=out_dir)
    return [Path(name) for name in result['files']]


def verify_release(conn: psycopg.Connection, release_id: int, *, hash_files: bool = False) -> bool:
    """核对成员元数据及产物存在、大小、行数；仅显式要求时重算文件哈希。"""
    expected = conn.execute(
        "SELECT content_digest, char_count, train_char_count, val_char_count, test_char_count"
        " FROM dataset_release WHERE release_id = %s", (release_id,)
    ).fetchone()
    if expected is None:
        raise ExportError(f"数据集 {release_id} 不存在")
    members = []
    char_counts = {split: 0 for split in SPLIT_NAMES}
    for ordinal, version_id, split, text_hash, char_count in conn.execute(
        """SELECT m.ordinal, m.version_id, m.split, v.text_hash, v.char_count
           FROM dataset_member m JOIN document_version v USING (version_id)
           WHERE m.release_id = %s ORDER BY m.ordinal""",
        (release_id,),
    ):
        members.append((ordinal, version_id, split, bytes(text_hash)))
        char_counts[split] += char_count
    if _digest(members) != bytes(expected[0]):
        return False
    if (sum(char_counts.values()), char_counts["train"], char_counts["val"],
            char_counts["test"]) != expected[1:]:
        return False
    from . import exporters
    return exporters.verify_files(conn, release_id, hash_files=hash_files)


def record_training(conn: psycopg.Connection, release_name: str, run_name: str) -> None:
    release = conn.execute(
        "SELECT release_id FROM dataset_release WHERE name = %s", (release_name,)
    ).fetchone()
    if release is None:
        raise ExportError(f"数据集不存在：{release_name}")
    conn.execute(
        "INSERT INTO training_use (release_id, run_name, used_at) VALUES (%s, %s, now())",
        (release[0], run_name),
    )
    conn.commit()
