"""没有来源原值的来源（维基文库、殆知阁、古腾堡）按正文和题名推断规范标签与年份。

规则见 Project docs/taxonomy.md §8.3。单篇信号来自正文开头的署名行、四库提要和类目、题名里的年份与发布机关、
现代文献末尾的落款日期；同一作品（维基文库取题名第一段）的信号在不冲突时传给作品其余各篇。
全库先算一遍（prepare），再逐篇写入，因此不在入库时运行，新数据入库后重跑本规则即可。
"""
import collections
import re

from . import classify, variants
from .taxonomy import TAXONOMY, eras_for_years

RULE_VERSION = 'taxonomy-infer-v1'
SOURCES = ['wikisource', 'daizhige', 'gutenberg']
# 这些依据得到的值可以被更细的推断取代；来源、人工、查证给出的不动。
DERIVED = {'rule', 'rule:translation', 'source_map'}

DYN = {
    '商': 'xianqin.shang', '周': 'xianqin.zhou', '春秋': 'xianqin.zhou.chunqiu', '战国': 'xianqin.zhou.zhanguo',
    '秦': 'qinhan.qin', '汉': 'qinhan.han', '西汉': 'qinhan.han.xihan', '前汉': 'qinhan.han.xihan',
    '东汉': 'qinhan.han.donghan', '三国': 'weijin.sanguo', '魏': 'weijin.sanguo',
    # 单字“蜀”可能是蜀汉，也可能是前蜀、后蜀，不用。
    '吴': 'weijin.sanguo', '晋': 'weijin.jin', '西晋': 'weijin.jin.xijin', '东晋': 'weijin.jin.dongjin',
    '刘宋': 'weijin.nanchao', '南朝宋': 'weijin.nanchao', '南齐': 'weijin.nanchao', '齐': 'weijin.nanchao',
    '梁': 'weijin.nanchao', '陈': 'weijin.nanchao', '北魏': 'weijin.beichao', '后魏': 'weijin.beichao',
    '北齐': 'weijin.beichao', '北周': 'weijin.beichao',
    # 十六国没有单列时代，只到魏晋南北朝。
    '后秦': 'weijin', '姚秦': 'weijin', '前秦': 'weijin', '苻秦': 'weijin', '北凉': 'weijin', '西秦': 'weijin', '隋': 'suitang.sui', '唐': 'suitang.tang',
    '五代': 'suitang.wudai', '后唐': 'suitang.wudai', '南唐': 'suitang.wudai', '后周': 'suitang.wudai',
    '后蜀': 'suitang.wudai', '前蜀': 'suitang.wudai', '宋': 'song.song', '北宋': 'song.song.beisong',
    '南宋': 'song.song.nansong', '辽': 'song.liao', '金': 'song.jin', '元': 'yuan', '明': 'ming',
    '清': 'qing', '国朝': 'qing', '民国': 'minguo',
}
_D = '|'.join(sorted(DYN, key=len, reverse=True))
_S = r'[\s　]*'
# 署名行：行首（可带括号）朝代 + 人名 + 撰/著等。朝代后须有括号或空白，免得把姓氏当朝代。
BYLINE = re.compile(
    rf'(?:^|\n|卷[一二三四五六七八九十百上中下0-9]*){_S}(?:[（(〔\[【]({_D})[)）〕\]】]|({_D})[\s　]+){_S}'
    rf'([一-鿿·]{{1,4}}(?:{_S}[一-鿿]{{1,3}})?){_S}(?:等{_S})?'
    rf'(奉敕撰|奉敕编|撰述|撰|著|编|辑|纂|集|注|译|述)(?=[\s　]|$|[一-鿿]{{0,6}}\n)')
AUTHORED = {'撰', '著', '撰述', '奉敕撰'}
TIYAO = re.compile(rf'谨案.{{0,40}}?[卷巻](?:{_S})?({_D})([一-鿿]{{1,4}}?)(?:等)?(撰|著)')
TAGERA = re.compile(rf'类[一二三四五六七八九十]*{_S}【({_D})】')
SIKU = re.compile(rf'四库全书{_S}([经史子集])部[一二三四五六七八九十]*([\s\S]{{0,60}}?)类')
SIKU_BU = re.compile(rf'四库全书{_S}([经史子集])部')
BU = {'经': 'jing', '史': 'shi', '子': 'zi', '集': 'ji'}
LEI = {
    'jing': {'易': 'yi', '书': 'shu', '诗': 'shi', '礼': 'li', '春秋': 'chunqiu', '孝经': 'xiaojing',
             '五经总义': 'wujing', '四书': 'sishu', '乐': 'yue', '小学': 'xiaoxue'},
    'shi': {'正史': 'zhengshi', '编年': 'biannian', '纪事本末': 'benmo', '别史': 'bieshi', '杂史': 'zashi',
            '诏令奏议': 'zhaoling', '传记': 'zhuanji', '史钞': 'shichao', '载记': 'zaiji', '时令': 'shiling',
            '地理': 'dili', '职官': 'zhiguan', '政书': 'zhengshu', '目录': 'mulu', '史评': 'shiping'},
    'zi': {'儒家': 'rujia', '兵家': 'bingjia', '法家': 'fajia', '农家': 'nongjia', '医家': 'yijia',
           '天文算法': 'tianwen', '术数': 'shushu', '艺术': 'yishu', '谱录': 'pulu', '杂家': 'zajia',
           '类书': 'leishu', '小说家': 'xiaoshuo', '释家': 'shijia', '道家': 'daojia'},
    'ji': {'楚辞': 'chuci', '别集': 'bieji', '总集': 'zongji', '诗文评': 'shiwenping', '词曲': 'ciqu'},
}
# 总集、类书的署名是编者，书中内容跨多个时代，不按署名定时代。
COMPILATIONS = {'ji.zongji', 'zi.leishu'}
# 四库类目与体裁一一对应的才推体裁。
CLASS_GENRE = {'shi.zhengshi': 'shiti.jizhuan', 'shi.biannian': 'shiti.biannian', 'shi.benmo': 'shiti.benmo',
               'zi.leishu': 'gongju.leishu', 'zi.shijia': 'zongjiao.fojing', 'ji.shiwenping': 'sanwen.shihua'}

_ROC = re.compile(r'民[國国]\s*([0-9０-９]+|[〇○零一二三四五六七八九十百]+)\s*年')
_WESTERN = re.compile(r'(?<![0-9])(1[89][0-9]{2}|20[0-9]{2})\s*年')
_CNYEAR = re.compile(r'(一[八九][〇○零一二三四五六七八九]{2}|二[〇○零][〇○零一二三][〇○零一二三四五六七八九])\s*年')
_ROCDU = re.compile(r'(?<![0-9])([0-9]{2,3})年度')
_CN4 = str.maketrans('〇○零一二三四五六七八九', '000123456789')
MODERN_TITLE = re.compile(r'中华人民共和国|中華人民共和國|中華民國|中华民国|国务院|國務院|委员会|委員會|人民政府|立法院|'
                          r'行政院|司法院|法院|联合国|聯合國|人民代表大会|人民代表大會|省|市|县|縣')
HEAD, TAIL = 1500, 300

JURISDICTION = [
    ('prc', re.compile(r'中华人民共和国|中華人民共和國|国务院|全国人民代表大会|全國人民代表大會|全国人大|人民政府|'
                       r'最高人民法院|最高人民检察院|中国人民银行|中央军委|中国人民政治协商会议|'
                       r'(?:部|总局|管理局|委员会)(?:办公厅|办公室)?\s*关于')),
    # 中华民国机关；按年代再分为 roc（1949 年以前）和 tw（1949 年以后），见 infer()。
    ('roc', re.compile(r'中華民國|中华民国|民[國国]\s*[0-9０-９〇零一二三四五六七八九十百]+\s*年|立法院|行政院|司法院|'
                       r'考試院|考试院|監察院|监察院|國民政府|国民政府|總統府|总统府|釋字')),
    ('hk', re.compile(r'香港')),
    ('mo', re.compile(r'澳門|澳门')),
    ('intl', re.compile(r'联合国|聯合國|安理会|安理會|上海合作组织|世界卫生组织|世界衛生組織|世界贸易组织|世界貿易組織|'
                        r'国际劳工组织|國際勞工組織|日内瓦公约|日內瓦公約|海牙公约|海牙公約')),
]
# 发布机关能确定时代的：人民共和国国家机关在 1949 年后（“人民政府”1949 年前已有，不算）；
# 司法院院字、院解字解释在民国；释字解释在 1949 年后。
_DANGDAI_PRC = re.compile(r'中华人民共和国|中華人民共和國|国务院|全国人民代表大会|全國人民代表大會|全国人大|'
                          r'最高人民法院|最高人民检察院|中央军委|(?:部|总局|管理局|委员会)(?:办公厅|办公室)?\s*关于')
_MINGUO_ISSUER = re.compile(r'院字第|院解字第')
_DANGDAI_ISSUER = re.compile(r'釋字|释字')

_PAREN_TAIL = re.compile(r'\s*[（(][^（()）]*[)）]\s*$')
_NUMBER_TAIL = re.compile(r'\s*[〔\[［(（]?(?:[0-9]{4}|[一二][〇○零一二三四五六七八九]{3})[〕\]］)）]?\s*年?\s*'
                          r'第?\s*[0-9０-９一二三四五六七八九十百千〇○零]+\s*[号號]$'
                          r'|\s*第\s*[0-9０-９一二三四五六七八九十百千〇○零]+\s*[号號]$')
# 现代文献的题名结尾 -> 体裁。只用于判定为现代的文献。
MODERN_GENRE = [
    (re.compile(r'(判决|判決|裁定|解释|解釋)(书|書)?$|院字第|院解字第|釋字第|释字第'), 'fazhi.pandu'),
    (re.compile(r'(宪法|憲法|法|法律|法规|法規|条例|條例|细则|細則|办法|辦法|规则|規則|规定|規定|规程|規程|通则|通則|'
                r'准则|準則|章程|公约|公約|条约|條約|协定|協定|议定书|議定書|宪章|憲章)$'), 'fazhi.fagui'),
    (re.compile(r'(通知|决议|決議|决议案|決議案|公报|公報|决定|決定|批复|批覆|批示|公告|命令|令|意见|意見|通告|声明|聲明|'
                r'报告|報告|复函|覆函|函|通报|通報|布告|佈告|指示|讲话|講話|谈话|談話|宣言|方案|纲要|綱要|规划|規劃|'
                r'请示|請示|答复|答覆|汇报|彙報)$|决议案|決議案'), 'gongdu.gongwen'),
    (re.compile(r'(书|書|信)$'), 'sanwen.shuxin'),
]
CLASSICAL_GENRE = [
    (re.compile(r'(诗|詩|绝句|絕句|律诗|律詩|古诗|古詩|吟|歌行)$|[二三四五六七八九十]首$|其[一二三四五六七八九十]+$'), 'yunwen.shi'),
    (re.compile(r'賦$|赋$'), 'yunwen.fu'),
    (re.compile(r'(疏|奏|表|劄子|札子|奏議|奏议|上書|上书)$'), 'gongdu.zouyi'),
    (re.compile(r'(诏|詔|制|敕|册文|冊文)$'), 'gongdu.zhaoling'),
    (re.compile(r'(序|跋|後序|后序)$'), 'sanwen.xuba'),
    (re.compile(r'(書|书|啟|启|尺牘|尺牍)$'), 'sanwen.shuxin'),
    (re.compile(r'(記|记)$'), 'sanwen.zaji'),
    (re.compile(r'(傳|传|碑|銘|铭|墓誌|墓志|行狀|行状|神道碑)$'), 'sanwen.zhuanzhuang'),
    (re.compile(r'(祭文|誄|诔|哀辭|哀辞)$'), 'sanwen.aiji'),
    (re.compile(r'(論|论|說|说|辨|議|议|解)$'), 'sanwen.lunshuo'),
]
# 公报类作品的各篇是独立公文，题名可以单独判断体裁。
GAZETTE = re.compile(r'公[报報]$')
_AUTHOR_SUFFIX = re.compile(r'\s*[（(]([一-鿿·]{2,5})[)）]\s*$')
_EDITION = re.compile(r'\s*[（(](四庫全書本|四库全书本)[)）]\s*$')


def _cn_int(s):
    s = s.translate(str.maketrans('０１２３４５６７８９', '0123456789'))
    if s.isdigit():
        return int(s)
    digits = dict(zip('〇○零一二三四五六七八九', [0, 0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9]))
    total = current = 0
    for ch in s:
        if ch in digits:
            current = digits[ch]
        elif ch in '十百':
            total += (current or 1) * (10 if ch == '十' else 100)
            current = 0
    return total + current


def _years_in(text):
    found = [1911 + _cn_int(m.group(1)) for m in _ROC.finditer(text)]
    found += [int(m.group(1)) for m in _WESTERN.finditer(text)]
    found += [int(m.group(1).translate(_CN4)) for m in _CNYEAR.finditer(text)]
    return found


def _year(title, head, tail, upper):
    """题名里的年份；否则现代文献的落款（末尾最晚的年份）；否则开头最晚的年份。"""
    if (m := _ROC.search(title)):
        return 1911 + _cn_int(m.group(1))
    if (m := _WESTERN.search(title)) or (m := _CNYEAR.search(title)):
        return int(m.group(1).translate(_CN4))
    if (m := _ROCDU.search(title)):
        return 1911 + int(m.group(1))
    for part in (tail, head[:600]):
        found = [y for y in _years_in(part) if 1850 <= y <= upper]
        if found:
            return max(found)
    return None


def jurisdiction(title):
    found = {name for name, pattern in JURISDICTION if pattern.search(title)}
    return found.pop() if len(found) == 1 else None


def _strip_title(title):
    title = title.split('/')[-1].strip()
    while (stripped := _PAREN_TAIL.sub('', title)) != title:
        title = stripped
    return _NUMBER_TAIL.sub('', title).strip()


def title_genre(title, modern):
    title = _strip_title(title)
    for pattern, genre in (MODERN_GENRE if modern else CLASSICAL_GENRE):
        if pattern.search(title):
            return genre
    return None


def signals(title, text, poet_era, upper):
    """单篇信号 {维度: (值, 规则)}；era_translated 只在没有其他时代时使用。"""
    found = {}
    head = text[:HEAD]
    simple = variants.simplify(head)
    leaf = title.split('/')[-1]
    where = jurisdiction(title)
    if where:
        found['jurisdiction'] = (where, 'issuer')
    year = None
    if where or MODERN_TITLE.search(title) or _ROC.search(title) or _ROCDU.search(title) \
            or _WESTERN.search(title) or _CNYEAR.search(title):
        year = _year(title, head, text[-TAIL:], upper)
    if year and 1800 <= year <= upper:
        found['years'] = ((year, year), 'year')
    if (m := SIKU.search(simple)):
        run = re.split(r'[^一-鿿]', m.group(2))[-1]
        table = LEI[BU[m.group(1)]]
        lei = next((table[run[-n:]] for n in (4, 3, 2, 1) if len(run) >= n and run[-n:] in table), None)
        found['class'] = (f'{BU[m.group(1)]}.{lei}', 'siku_lei') if lei else (BU[m.group(1)], 'siku_bu')
    elif (m := SIKU_BU.search(simple)):
        found['class'] = (BU[m.group(1)], 'siku_bu')
    if (m := TIYAO.search(simple)):
        found['era'] = (DYN[m.group(1)], 'tiyao')
    elif (m := BYLINE.search(simple)) and m.group(4) in AUTHORED:
        found['era'] = (DYN[m.group(1) or m.group(2)], 'byline')
    elif (m := TAGERA.search(simple[:400])):
        found['era'] = (DYN[m.group(1)], 'tag')
    elif (a := _AUTHOR_SUFFIX.search(leaf)) and (c := poet_era.get(variants.simplify(a.group(1)))):
        era, n = c.most_common(1)[0]
        if n >= 0.9 * sum(c.values()) and sum(c.values()) >= 3:
            found['era'] = (era, 'poet')
    elif (m := BYLINE.search(simple)) and m.group(4) == '译':
        found['era_translated'] = (DYN[m.group(1) or m.group(2)], 'translation')
    return found


def infer(documents, poet_era, borrowed, upper):
    """documents: [(来源, doc_id, 题名, 正文)]；poet_era: {诗人: Counter(时代)}；borrowed: {简体书名: 映射标签或 None}。

    返回 {doc_id: {'tags': {维度: ([编号], 依据)}, 'years': (起, 止) 或 None}}，只含有结果的文档。
    """
    works = collections.defaultdict(list)
    for source, doc_id, title, text in documents:
        title = title or ''
        work = title.split('/')[0] if source == 'wikisource' else title
        works[(source, work)].append((doc_id, title, signals(title, text, poet_era, upper)))
    results = {}
    for (source, work), members in works.items():
        shared = {}
        for dim in ('era', 'class'):
            values = {m[2][dim][0] for m in members if dim in m[2]}
            if dim == 'class' and len({v.split('.')[0] for v in values}) == 1:
                values = {max(values, key=len)}
            if len(values) != 1:
                continue
            support = [m[2][dim][1] for m in members if dim in m[2]]
            if any(r in ('tiyao', 'tag', 'siku_lei', 'siku_bu') for r in support) or len(support) >= 2:
                shared[dim] = values.pop()
        name = variants.simplify(_EDITION.sub('', work))
        lent = borrowed.get(name) if source != 'daizhige' else None
        gazette = bool(GAZETTE.search(work))
        for doc_id, title, found in members:
            values = {dim: found[dim][0] for dim in ('era', 'class', 'jurisdiction') if dim in found}
            for dim in ('era', 'class'):
                values.setdefault(dim, shared.get(dim))
            if not values['class'] and lent and lent.get('class'):
                values['class'] = lent['class'][0]
            if values['class'] in COMPILATIONS and found.get('era', (None, ''))[1] != 'poet':
                values['era'] = None
            basis = {dim: 'rule' for dim, v in values.items() if v}
            span = found.get('years', (None,))[0]
            if not values['era'] and span:
                values['era'] = 'dangdai' if values.get('jurisdiction') == 'prc' and span[0] >= 1949 \
                    else eras_for_years(*span)[0]
                basis['era'] = 'rule'
            if not values['era']:
                if values.get('jurisdiction') == 'prc' and _DANGDAI_PRC.search(title) or _DANGDAI_ISSUER.search(title):
                    values['era'] = 'dangdai'
                elif _MINGUO_ISSUER.search(title):
                    values['era'] = 'minguo'
                elif 'era_translated' in found and values['class'] not in COMPILATIONS:
                    values['era'] = found['era_translated'][0]
                    basis['era'] = 'rule:translation'
                if values['era']:
                    basis.setdefault('era', 'rule')
            # 中华民国只用于 1949 年以前；1949 年以后台湾地区的文献标“中国台湾”。年代不明、或正当 1949 年的不标。
            if values.get('jurisdiction') == 'roc':
                year = span[0] if span else None
                if year is not None:
                    values['jurisdiction'] = 'roc' if year < 1949 else 'tw' if year > 1949 else None
                else:
                    values['jurisdiction'] = {'minguo': 'roc', 'dangdai': 'tw'}.get(values['era'])
            if values.get('jurisdiction') == 'prc' and span and span[0] < 1949:
                values['jurisdiction'] = None
            modern = bool(span or values.get('jurisdiction') or values['era'] in ('minguo', 'dangdai'))
            if lent and lent.get('genre'):
                values['genre'] = lent['genre'][0]
            elif source == 'wikisource' and (len(members) == 1 or gazette):
                values['genre'] = title_genre(title, modern)
            if not values.get('genre') and values['class'] in CLASS_GENRE:
                values['genre'] = CLASS_GENRE[values['class']]
            if not values['class'] and modern and (values.get('genre') or '').startswith('fazhi'):
                values['class'] = 'ztf.d'
            tags = {dim: ([v], basis.get(dim, 'rule')) for dim, v in values.items() if v}
            for dim, (ids, _) in tags.items():
                if ids[0] not in TAXONOMY[dim]:
                    raise ValueError(f'{RULE_VERSION} 给出未登记的 {dim} 编号：{ids[0]}')
            if tags or span:
                results[doc_id] = {'tags': tags, 'years': span}
    return results


def merge(current, result):
    """把推断结果并进元数据。空维度直接填；已有值由映射或规则给出、且新值更细时替换。年份只在没有或由规则推出时更新。"""
    basis = dict(current.get('field_basis') or {})
    tags = list(current.get('tags') or [])
    patch = {}
    for dim, (ids, how) in result['tags'].items():
        old = [t['tag_id'] for t in tags if t['taxonomy'] == dim]
        if old:
            finer = all(any(i.startswith(o + '.') for o in old) for i in ids)
            if basis.get(f'tags.{dim}') not in DERIVED or not finer:
                continue
            tags = [t for t in tags if t['taxonomy'] != dim]
        tags.extend({'taxonomy': dim, 'tag_id': i} for i in ids)
        basis[f'tags.{dim}'] = how
        patch['tags'] = tags
    span = result['years']
    old = current.get('composed_years')
    if span and (not old or basis.get('composed_years') in DERIVED):
        new = {'start_year': span[0], 'end_year': span[1]}
        if old != new:
            patch['composed_years'] = new
            basis['composed_years'] = 'rule'
    if patch:
        patch['field_basis'] = basis
    return patch


def load(conn, upper):
    """从数据库读全部相关文档并推断。conn 只读即可。"""
    from ..store import textcodec
    poet_era = collections.defaultdict(collections.Counter)
    for author, tags in conn.execute(
            "SELECT unnest(v.authors), v.tags FROM document_version v JOIN document d USING (doc_id)"
            " JOIN source s USING (source_id) WHERE v.is_current AND s.name IN ('chinese_poetry', 'greathangpt')"
            " AND v.authors IS NOT NULL"):
        eras = [t['tag_id'] for t in tags if t['taxonomy'] == 'era']
        if len(eras) == 1:
            poet_era[variants.simplify(author)][eras[0]] += 1
    borrowed = {}
    for title, category in conn.execute(
            "SELECT v.title, v.category FROM document_version v JOIN document d USING (doc_id)"
            " JOIN source s USING (source_id) WHERE v.is_current AND s.name = 'daizhige'"):
        found = classify.classify('daizhige', {'category': category})['tags']
        key = variants.simplify(title or '')
        borrowed[key] = found if key not in borrowed or borrowed[key] == found else None
    documents = []
    with conn.cursor(name='infer_documents') as cursor:
        cursor.itersize = 5000
        cursor.execute(
            "SELECT s.name, v.doc_id, v.title, b.content FROM document_version v JOIN document d USING (doc_id)"
            " JOIN source s USING (source_id) JOIN text_blob b USING (text_hash)"
            " WHERE v.is_current AND s.name = ANY(%s)", (SOURCES,))
        for source, doc_id, title, content in cursor:
            text = textcodec.decompress(bytes(content))
            documents.append((source, doc_id, title, text[:HEAD] + '\n' + text[-TAIL:] if len(text) > HEAD + TAIL else text))
    return infer(documents, poet_era, borrowed, upper)
