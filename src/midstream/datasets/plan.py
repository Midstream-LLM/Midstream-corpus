"""试算：选用、分组、划分和分布统计。纯计算，不写库。"""

import hashlib
from dataclasses import dataclass, field

import psycopg

from ..dedup import engine as dedup
from . import keeper, script
from .recipe import ExportError, Recipe
from .selection import _drop_duplicates, _reference, _select, _variant_selection
from .split import _assign_splits, _check_training_history, _fraction, _group_keys, _reserved_docs


def _digest(members: list[tuple[int, int, str, bytes]], cuts=None) -> bytes:
    digest = hashlib.sha256()
    for ordinal, version_id, split, text_hash in members:
        digest.update(f"{ordinal}:{version_id}:{split}:{text_hash.hex()}\n".encode())
        for start,end in (cuts or {}).get(version_id,()):
            digest.update(f'cut:{version_id}:{start}:{end}\n'.encode())
    return digest.digest()


@dataclass
class ReleasePlan:
    recipe: Recipe
    members: list[tuple[int, int, str, bytes]]
    report: dict
    cuts: dict = field(default_factory=dict)


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


def plan_release(conn: psycopg.Connection, recipe: Recipe, *, estimate=None) -> ReleasePlan:
    """仅用元数据试算；不生成发布、文件或正文解压。"""
    selected = _select(conn, recipe)
    if recipe.variant and selected:
        selected = _variant_selection(conn, recipe.variant, selected)
    if not selected:
        raise ExportError("配方没有选中任何文档")
    if recipe.selection.dedup != 'none' or conn.execute('SELECT EXISTS(SELECT 1 FROM dedup_override)').fetchone()[0]:
        if not dedup.document_snapshot_current(conn):
            raise ExportError('文档级查重结果已过期，请先运行 midstream dedup')
        if any(item.missing is None for item in selected):
            raise ExportError('部分正文还没有缺字统计，请先运行 midstream dedup')
        if recipe.selection.scripts == 'separate' and any(item.script is None for item in selected):
            raise ExportError('部分正文还没有字形统计，请先运行 midstream dedup')
    # 分组用去重前的全集：被舍弃的文档仍可能连接作品和重复簇。
    reserved = _reserved_docs(conn, recipe)
    groups = _group_keys(conn, list({item.doc_id for item in selected} | reserved.keys()))
    kept = selected
    if recipe.selection.dedup in ("document", "span"):
        reference = _reference(conn, recipe.selection.dedup_against)
        if recipe.selection.scripts == 'separate' and any(item.script is None for item in reference):
            raise ExportError('参照发布中部分正文还没有字形统计，请先运行 midstream dedup')
        kept = _drop_duplicates(conn, recipe.selection, selected, reference)
    cuts = {}
    if recipe.selection.dedup == 'span':
        from . import cuts as span_cuts
        kept,cuts = span_cuts.plan(conn,recipe,kept,reference)
        if not kept:
            raise ExportError('裁剪后没有满足 min_chars 的文档')
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
        **selection_report, 'keeper': keeper.describe(),
        'scripts': {'mode': recipe.selection.scripts, **script.describe()}, 'documents': len(kept), 'chars': chars, 'split_by': recipe.split_by,
        'splits': splits, 'distributions': _distributions(kept, groups, assigned), 'warnings': warnings,
    }, cuts)
    if estimate is not None:
        plan.report['token_stats'] = estimate(conn, kept, groups, assigned, cuts)
    return plan
