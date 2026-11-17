"""时代、体裁、部类、地域、语体、主题的受控词表（data/taxonomy/vocabulary.json）。

编号稳定，中文名只用于显示。设计见 Project docs/taxonomy.md。
时代带年份区间（无公元 0 年，两端都含）；`end` 为 None 表示至今。
"""
import json
from datetime import date
from pathlib import Path

_DATA = Path(__file__).parents[1] / 'data' / 'taxonomy'
_VOCABULARY = json.loads((_DATA / 'vocabulary.json').read_text(encoding='utf-8'))

TAXONOMY_VERSION = _VOCABULARY['version']
DIMENSIONS = ('era', 'genre', 'class', 'region', 'register', 'topic')
TAXONOMY = {name: {item['id']: item['name'] for item in _VOCABULARY[name]} for name in DIMENSIONS}
ERA_YEARS = {item['id']: (item['start'], item['end'] if item['end'] is not None else date.today().year)
             for item in _VOCABULARY['era']}


def label(dimension, tag_id):
    """带上级的中文名，如“宋辽金·宋·北宋”。"""
    parts = tag_id.split('.')
    return '·'.join(TAXONOMY[dimension].get('.'.join(parts[:i + 1]), parts[i]) for i in range(len(parts)))


def era_envelope(ids):
    """多个时代的年份覆盖范围。"""
    spans = [ERA_YEARS[i] for i in ids if i in ERA_YEARS]
    return (min(a for a, _ in spans), max(b for _, b in spans)) if spans else None


def eras_for_years(start, end):
    """年份区间对应的时代：落在某个时代之内就取最细的那一个；跨时代就取覆盖到的各个最上层时期。"""
    inside = [i for i, (a, b) in ERA_YEARS.items() if a <= start and end <= b]
    if inside:
        return [max(inside, key=lambda i: (i.count('.'), -(ERA_YEARS[i][1] - ERA_YEARS[i][0])))]
    return [i for i, (a, b) in ERA_YEARS.items() if '.' not in i and a <= end and start <= b]


def data_file(*parts):
    return _DATA.joinpath(*parts)
