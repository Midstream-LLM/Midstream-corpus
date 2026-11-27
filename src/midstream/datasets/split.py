"""分组、保留集和按字数划分。"""

import hashlib

import psycopg

from .recipe import SPLIT_NAMES, ExportError, TrainingLeak


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
    # 先从留出文档出发找出相关文档，再与训练过的文档求交；逐条相关子查询在训练集上百万篇时要几个小时。
    leaked = conn.execute(
        """WITH held AS MATERIALIZED (SELECT DISTINCT unnest(%s::bigint[]) AS doc_id),
           trained AS MATERIALIZED (
               SELECT DISTINCT v.doc_id
               FROM training_use u
               JOIN dataset_member m ON m.release_id = u.release_id AND m.split = 'train'
               JOIN document_version v USING (version_id)
           ), related AS (
               SELECT doc_id AS held, doc_id AS other FROM held
               UNION ALL
               SELECT a.doc_id, b.doc_id FROM held h
               JOIN document_work a USING (doc_id) JOIN document_work b USING (work_id)
               WHERE a.status <> 'rejected' AND b.status <> 'rejected'
               UNION ALL
               SELECT h.doc_id, w.doc_id FROM held h
               JOIN document_version v USING (doc_id)
               JOIN dup_cluster_member m USING (version_id)
               JOIN dup_cluster c ON c.cluster_id = m.cluster_id AND c.level = 'document'
               JOIN dup_cluster_member n ON n.cluster_id = m.cluster_id
               JOIN document_version w ON w.version_id = n.version_id
           )
           SELECT DISTINCT r.held FROM related r JOIN trained t ON t.doc_id = r.other
           LIMIT 5""",
        (held_out,),
    ).fetchall()
    if leaked:
        raise TrainingLeak(f"验证或测试集包含已训练过的文档，例如 {[row[0] for row in leaked]}")
