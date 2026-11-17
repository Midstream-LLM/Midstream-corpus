"""来源原值 -> 规范标签（时代、体裁、部类、地域）与年份。

映射表在 data/taxonomy/：era_labels.json 是各来源共用的时代标签表，sources/<来源>.json 是各来源的
体裁、部类映射。只给出能确定的维度；空维度留给规则推断、查证或人工。设计见 Project docs/taxonomy.md。
"""
import json
import re
from functools import cache

from .taxonomy import TAXONOMY, era_envelope, eras_for_years, data_file

MAP_VERSION = 'taxonomy-map-v1'
# 只有空年份或由规则、映射推出的年份可以重算；来源、人工、查证给出的年份不动。
_DERIVED_YEARS = {'rule', 'source_map'}
_CHAPTER = re.compile(r'第[一二三四五六七八九十百千零〇0-9]+回')


@cache
def _era_labels():
    return json.loads(data_file('era_labels.json').read_text(encoding='utf-8'))['labels']


@cache
def _source_map(source):
    path = data_file('sources', f'{source}.json')
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def versions():
    """映射用到的各文件版本，写进运行记录以便复现。"""
    found = {'era_labels': json.loads(data_file('era_labels.json').read_text(encoding='utf-8'))['version']}
    for path in sorted(data_file('sources').glob('*.json')):
        found[path.stem] = json.loads(path.read_text(encoding='utf-8'))['version']
    return found


def _value(document, key):
    if key is None:
        return None
    if key.startswith('raw.'):
        return (document.get('raw_fields') or {}).get(key[4:])
    return document.get(key)


def _lookup(table, value, prefix=False):
    if value is None:
        return None
    if not prefix:
        return table.get(value)
    best = None
    for key, entry in table.items():
        if (value == key or value.startswith(key + '/')) and (best is None or len(key) > len(best[0])):
            best = (key, entry)
    return best[1] if best else None


def _source_entry(source, document):
    spec = _source_map(source)
    if spec is None:
        return {}
    if 'all' in spec:
        return dict(spec['all'])
    entry = _lookup(spec['map'], _value(document, spec['key']), spec.get('match') == 'prefix')
    if entry is None and 'by_raw' in spec:
        raw = document.get('raw_fields') or {}
        for condition, candidate in spec['by_raw'].items():
            field, _, expected = condition.partition('=')
            if field in raw and (not expected or raw[field] == expected):
                entry = candidate
                break
    if entry is None:
        entry = spec.get('default')
    entry = dict(entry or {})
    # 后备映射补主映射没给出的维度。
    for dim, ids in spec.get('fallback', {}).get(_value(document, spec.get('fallback_key')), {}).items():
        entry.setdefault(dim, ids)
    return entry


def classify(source, document):
    """document 里用到：composed_label、category、work_title、raw_fields、text、composed_years 及其依据。

    返回 {'tags': {维度: [编号]}, 'years': (起, 止) 或 None}。
    """
    entry = _source_entry(source, document)
    tags = {dim: list(entry.get(dim, [])) for dim in ('era', 'genre', 'class', 'region')}
    years = tuple(entry['years']) if 'years' in entry else None
    if not tags['era'] and document.get('composed_label'):
        label = _era_labels().get(document['composed_label'].strip())
        if label:
            tags['era'] = list(label['era'])
            years = years or (tuple(label['years']) if 'years' in label else None)
    # 小说只到一级时，正文有 3 处以上“第X回”回目的定为章回。
    if tags['genre'] == ['xiaoshuo'] and len(_CHAPTER.findall(document.get('text') or '')) >= 3:
        tags['genre'] = ['xiaoshuo.zhanghui']
    known = document.get('composed_years') or {}
    if not tags['era'] and known.get('start_year') and known.get('end_year'):
        tags['era'] = eras_for_years(known['start_year'], known['end_year'])
    if not years and tags['era'] and not known:
        years = era_envelope(tags['era'])
    for dim, ids in tags.items():
        unknown = [i for i in ids if i not in TAXONOMY[dim]]
        if unknown:
            raise ValueError(f'{source} 映射里有未登记的 {dim} 编号：{unknown}')
    return {'tags': {dim: ids for dim, ids in tags.items() if ids}, 'years': years}


def merge(current, result):
    """把映射结果并进元数据：只填规范值为空的维度；年份只在没有或由规则推出时更新。

    current 用 metadata 的结构：tags 为 [{"taxonomy","tag_id"}]，composed_years 为 {"start_year","end_year"}
    或 None，field_basis 为字典。返回要写的字段（没有变化时为空字典）。
    """
    basis = dict(current.get('field_basis') or {})
    tags = list(current.get('tags') or [])
    present = {t['taxonomy'] for t in tags}
    patch = {}
    added = False
    for dim, ids in result['tags'].items():
        if dim in present:
            continue
        tags.extend({'taxonomy': dim, 'tag_id': i} for i in ids)
        basis[f'tags.{dim}'] = 'source_map'
        added = True
    if added:
        patch['tags'] = tags
    years = result['years']
    old = current.get('composed_years')
    if years and (not old or basis.get('composed_years') in _DERIVED_YEARS):
        new = {'start_year': years[0], 'end_year': years[1]}
        if old != new:
            patch['composed_years'] = new
            basis['composed_years'] = 'source_map'
    if patch:
        patch['field_basis'] = basis
    return patch
