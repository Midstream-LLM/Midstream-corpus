"""拼字写法还原。

来源用各种写法表示录入时打不出的字，如“[宁*页]”“[跳-兆+孛]”“■〈谷九〉”“{山亢}”。很多这样的字
其实在 Unicode 扩展区里有。按三档处理，能用前一档就不用后一档：

1. 结构能对应到唯一的 Unicode 字：换成这个字。
2. 查不到单字，但结构明确：写成表意文字描述序列（IDS），如“⿰宁页”。
3. 其余拼字写法原样保留；只有不带信息的“■”“〓”和私用区字符换成“□”。

字形结构来自 CHISE-IDS（GPL-2.0-or-later，见 data/LICENSE-CHISE）。比较时把两边都展开到最小部件，
再忽略同方向结构的分组方式，所以“⿰宁页”能匹配 CHISE 里写成“⿰寧頁”或别的分组的字。
来源常先繁转简再保留拼字写法，所以部件也按 OpenCC 表试一遍繁体写法。
"""
import gzip
import itertools
import re
from functools import cache
from pathlib import Path

from . import variants

MISSING = '□'
RULE_VERSION = 'glyph-restore-v1'
BINARY = '⿰⿱⿴⿵⿶⿷⿸⿹⿺⿻⿼⿽'
TERNARY = '⿲⿳'
UNARY = '⿾⿿'
# 来源记法里的运算：* 左右、/ 上下、@ 包围；- 去掉部件、+ 加上部件；> ^ 含义不明，所有二元结构都试。
_SPLIT = {'*': ['⿰'], '/': ['⿱'], '@': list('⿴⿵⿶⿷⿸⿹⿺'), '>': list(BINARY), '^': list(BINARY)}
_GLYPH = re.compile(r'[⺀-⿟々〇ㄅ-ㄯ㐀-䶿一-鿿豈-﫿\U00020000-\U0003134f]')
_ENTITY = re.compile(r'&[^;]+;')
_VARIANT_IDC = re.compile(r'&U-i\d+\+(2FF[0-9A-F]);')


def _tokens(text):
    position = 0
    while position < len(text):
        entity = _ENTITY.match(text, position)
        if entity:
            yield entity.group(0)
            position = entity.end()
        else:
            yield text[position]
            position += 1


def _tree(tokens):
    head = next(tokens)
    arity = 2 if head in BINARY else 3 if head in TERNARY else 1 if head in UNARY else 0
    return (head, *(_tree(tokens) for _ in range(arity))) if arity else head


def data_info():
    data = (Path(__file__).parents[1] / 'data' / 'chise-ids.tsv.gz').read_bytes()
    import hashlib
    return {'file': 'chise-ids.tsv.gz', 'sha256': hashlib.sha256(data).hexdigest()}


@cache
def structures():
    """字 -> 结构树列表。"""
    result = {}
    with gzip.open(Path(__file__).parents[1] / 'data' / 'chise-ids.tsv.gz', 'rt', encoding='utf-8') as handle:
        for line in handle:
            if line.startswith('#'):
                continue
            char, *forms = line.rstrip('\n').split('\t')
            trees = []
            for form in forms:
                # “&U-i001+2FFB;”是带变体编号的描述符 ⿻；“@apparent=”是外观注释，含“?”的是未定结构，都不当结构用。
                form = _VARIANT_IDC.sub(lambda m: chr(int(m.group(1), 16)), form)
                if not form or form[0] not in BINARY + TERNARY + UNARY or '?' in form or '@' in form:
                    continue
                try:
                    trees.append(_tree(_tokens(form)))
                except StopIteration:
                    pass
            if trees:
                result[char] = trees
    return result


def _expand(node, seen=()):
    """展开到最小部件；部件自身有结构就继续拆（只取第一种结构，防循环）。"""
    if isinstance(node, tuple):
        return (node[0], *(_expand(child, seen) for child in node[1:]))
    trees = structures().get(node)
    if not trees or node in seen:
        return node
    tree = trees[0]
    return node if tree == node else _expand(tree, seen + (node,))


# 偏旁的不同写法视为同一部件（简化偏旁、旁形与本字）。
_RADICAL = {'訁': '言', '讠': '言', '釒': '金', '钅': '金', '飠': '食', '饣': '食', '糹': '糸', '纟': '糸',
            '⻊': '𧾷', '⺮': '竹', '艸': '艹', '⺾': '艹', '⻌': '辶', '⻍': '辶', '⻏': '阝', '⻖': '阝'}


def _canonical(node):
    """同方向的左右（上下）结构不论怎么分组都算同一个：⿰A⿰BC、⿰⿰ABC、⿲ABC 都是 ⿰[A,B,C]。"""
    if not isinstance(node, tuple):
        return _RADICAL.get(node, node)
    head = {'⿲': '⿰', '⿳': '⿱'}.get(node[0], node[0])
    parts = []
    for child in node[1:]:
        child = _canonical(child)
        if head in '⿰⿱' and isinstance(child, tuple) and child[0] == head:
            parts.extend(child[1:])
        else:
            parts.append(child)
    return (head, *parts)


@cache
def _index():
    """展开后的规范结构 -> 字集合。"""
    index = {}
    for char, trees in structures().items():
        for tree in trees:
            key = _canonical(_expand(tree, (char,)))
            index.setdefault(key, set()).add(char)
    return index


@cache
def _traditional():
    """简体 -> 可能的繁体（OpenCC 繁转简表反查）。"""
    reverse = {}
    for source, target in variants.table()[0].items():
        reverse.setdefault(target, []).append(chr(source))
    return reverse


def _forms(leaf):
    return [leaf, *_traditional().get(leaf, [])]


def _variants(node):
    """部件的简繁组合；组合数有上限，避免长写法爆炸。"""
    if not isinstance(node, tuple):
        return _forms(node)
    options = [_variants(child) for child in node[1:]]
    return [(node[0], *combo) for combo in itertools.islice(itertools.product(*options), 64)]


def _replace(node, old, new):
    """把树里等于 old 的部件（含其展开）换成 new；没找到返回 None。"""
    targets = {_canonical(_expand(form)) for form in _forms(old)}
    def walk(n):
        if _canonical(_expand(n)) in targets:
            return new, True
        if isinstance(n, tuple):
            changed, children = False, []
            for child in n[1:]:
                child, hit = walk(child)
                children.append(child)
                changed |= hit
            return (n[0], *children), changed
        return n, False
    for start in [node, *(_expand(node),)]:
        result, hit = walk(start)
        if hit:
            return result
    return None


def _remove(node, old):
    """去掉部件：只处理两部件结构里的一边。"""
    targets = {_canonical(_expand(form)) for form in _forms(old)}
    for start in (node, _expand(node)):
        if isinstance(start, tuple) and len(start) == 3:
            left, right = start[1], start[2]
            if _canonical(_expand(left)) in targets:
                return right
            if _canonical(_expand(right)) in targets:
                return left
    return None


def _parse(text):
    """把记法解析成候选结构树列表。左结合：[跳-兆+孛] = (跳-兆)+孛；括号先算。"""
    tokens = [t for t in text if not t.isspace()]
    position = 0

    def atom():
        nonlocal position
        if position >= len(tokens):
            return []
        token = tokens[position]
        position += 1
        if token == '(':
            inner = expression()
            if position < len(tokens) and tokens[position] == ')':
                position += 1
            return inner
        return [token] if _GLYPH.fullmatch(token) else []

    def expression():
        nonlocal position
        candidates = atom()
        pending = None   # (A-B 之前的候选, B)，等后面的 +C 做替换
        while position < len(tokens) and (tokens[position] in '*/@>^-+' or pending):
            op = tokens[position]
            if op in '*/@>^-+':
                position += 1
            else:
                op = '+'   # “锍-釒”后直接跟部件，如“脊-月目”，表示换成该部件
            right = atom()
            if not right:
                return []
            if op == '-':
                pending = (candidates, right[0])
                candidates = [t for c in candidates if (t := _remove(c, right[0])) is not None]
            elif op == '+' and pending:
                base, old = pending
                candidates = [t for c in base for r in right if (t := _replace(c, old, r)) is not None]
                pending = None
            else:
                ops = ['⿰', '⿱'] if op == '+' else _SPLIT[op]
                candidates = [(idc, c, r) for c in candidates for r in right for idc in ops]
                pending = None
            if not candidates and not pending:
                return []
        return candidates

    parsed = expression()
    return parsed if position >= len(tokens) else []


def _ids(node):
    return node if not isinstance(node, tuple) else node[0] + ''.join(_ids(child) for child in node[1:])


def resolve(inner, *, bare=False):
    """inner 是括号里的记法。bare=True 表示没写结构的部件串（如“山亢”），左右、上下都试。

    返回 (kind, text)，kind 为 'char'、'ids' 或 'missing'。
    """
    if bare:
        parts = [p for p in inner if not p.isspace()]
        if not (2 <= len(parts) <= 4) or not all(_GLYPH.fullmatch(p) for p in parts):
            return 'missing', MISSING
        trees = [_nest(idc, parts) for idc in ('⿰', '⿱')]
        exact = False
    else:
        trees = _parse(inner)
        exact = True
    leaves = {t for t in trees if not isinstance(t, tuple) and not t.startswith('&')}
    if exact and len(leaves) == 1 and len(trees) == 1:
        return 'char', leaves.pop()   # 减去部件后正好剩一个现成的字，如“锍-釒”剩“㐬”
    # 先按原部件找；找不到再试部件的繁体写法（来源常先繁转简），否则同一结构会同时命中简繁两字。
    for widen in (False, True):
        found = set()
        for tree in trees:
            for variant in (_variants(tree) if widen else [tree]):
                found |= _index().get(_canonical(_expand(variant)), set())
        if found:
            break
    if len(found) == 1:
        return 'char', next(iter(found))
    # 写明了结构（只有一种树）且部件都是正常汉字，就写成 IDS。
    if exact and len(trees) == 1 and isinstance(trees[0], tuple):
        return 'ids', _ids(trees[0])
    return 'missing', MISSING


def _nest(idc, parts):
    tree = parts[-1]
    for part in reversed(parts[:-1]):
        tree = (idc, part, tree)
    return tree


_NOTATION = re.compile(
    r"■?\[(?P<square>(?=[^\]\n]*[*+\-/>^@])(?:{g}|[*+\-/>^@()]){{2,16}})\]"
    r"|■?\{{(?P<described>(?P<p1>[上左外])(?P<a>{g}{{1,3}}?)(?P<p2>[下右内])(?P<b>{g}{{1,3}}))\}}"
    r"|■?\{{(?P<curly>{g}{{2,4}})\}}"
    r"|■[〈＜](?P<angle>(?:{g}|[*+\-/^@()]){{1,12}})[〉＞]"
    r"|＜(?P<wide>(?=[^＞\n]*[*+\-/^@])(?:{g}|[*+\-/^@()]){{2,12}})＞"
    r"|[■〓]".format(g=r"[\u2e80-\u2fdf\u3005\u3007\u3105-\u312f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\ue000-\uf8ff\U00020000-\U0003134f]"))
_PRIVATE = re.compile(r"[\ue000-\uf8ff\U000f0000-\U0010fffd]")


def _notation(match):
    """还原出字或 IDS 就替换；还原不了的拼字写法原样保留（可能是正常的括号文字，也保留了部件信息）；
    只有不带信息的“■”“〓”换成“□”。"""
    if not any(match.group(n) for n in ('square', 'wide', 'curly', 'described', 'angle')):
        return MISSING
    result = _resolve_match(match)
    return match.group(0) if result == MISSING else result


def _resolve_match(match):
    for name in ('square', 'wide'):
        if match.group(name):
            return resolve(match.group(name))[1]
    if match.group('curly'):
        return resolve(match.group('curly'), bare=True)[1]
    if match.group('described'):
        a, b, c, d = match.group('p1', 'a', 'p2', 'b')
        idc = {('上', '下'): '/', ('左', '右'): '*', ('外', '内'): '@'}.get((a, c))
        return resolve(f'{b}{idc}{d}')[1] if idc else MISSING
    inner = match.group('angle')
    if inner:
        return resolve(inner, bare=not any(op in inner for op in '*+-/^@'))[1]
    return MISSING


def restore(text):
    """把拼字写法换成还原出的字或 IDS，还原不了的和私用区字符换成“□”。"""
    return _PRIVATE.sub(MISSING, _NOTATION.sub(_notation, text))


def notations(text):
    """列出文中的缺字写法及还原结果，供评估：(写法, 种类, 结果)。"""
    out = []
    for match in _NOTATION.finditer(text):
        kind = next((n for n in ('square', 'wide', 'described', 'curly', 'angle') if match.group(n)), 'mark')
        result = _notation(match)
        level = ('missing' if result == MISSING else 'kept' if result == match.group(0)
                 else 'ids' if result[0] in BINARY + TERNARY + UNARY else 'char')
        out.append((match.group(0), kind, level, result))
    out.extend((m.group(0), 'private', 'missing', MISSING) for m in _PRIVATE.finditer(text))
    return out
