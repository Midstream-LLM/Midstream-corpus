"""扫描件 OCR：PDF 逐页转图，交给可替换的识别引擎，结果按页缓存在 OCR_ROOT。

缓存键是 (引擎, 文件 SHA-256, DPI, 页码)，与文件路径无关；重跑、换目录都不重复识别。
入库时映射方案的 scan 格式只读缓存，不会触发识别或付费调用。
"""

import base64
import hashlib
import json
import os
import re
import subprocess
import tempfile
import time
import tomllib
from pathlib import Path

import httpx

from .. import config

PDF = {'.pdf'}
IMAGES = {'.png', '.jpg', '.jpeg', '.tif', '.tiff', '.webp', '.bmp'}
SUFFIXES = PDF | IMAGES
DPI = 300
RETRY_WAITS = (15, 60, 180)


class OcrError(Exception):
    pass


def _settings():
    try:
        with config.OCR_CONFIG.open('rb') as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return {}


class Kandian:
    """看典古籍 OCR API（https://kandianguji.com/api_documentation），单图同步识别。"""
    id = 'kandian-v2'
    label = '看典古籍 API'
    url = 'https://ocr.kandianguji.com/ocr_api'
    price_note = '超出免费额度后约 0.015 元/页；扫描件会上传到看典服务器'

    def __init__(self, transport=None):
        self.client = httpx.Client(timeout=180, transport=transport)

    def available(self):
        account = _settings().get('kandian', {})
        return bool(account.get('token') and account.get('email'))

    def recognize(self, image: bytes) -> dict:
        account = _settings().get('kandian', {})
        if not (account.get('token') and account.get('email')):
            raise OcrError(f'未配置看典 token：在 {config.OCR_CONFIG} 的 [kandian] 下填写 email 和 token')
        payload = {'token': account['token'], 'email': account['email'],
                   'image': base64.b64encode(image).decode(), 'det_mode': 'auto', 'version': 'v2',
                   'det_layout': True, 'return_position': True}
        # 看典文档说明硬件资源有限、会间歇 502，建议稍后重试。
        for wait in (*RETRY_WAITS, None):
            try:
                response = self.client.post(self.url, json=payload)
                if response.status_code not in (502, 503, 504) or wait is None:
                    break
            except httpx.TransportError:
                if wait is None:
                    raise
            time.sleep(wait)
        response.raise_for_status()
        body = response.json()
        if body.get('message') != 'success':
            raise OcrError(f"看典返回错误：{body.get('info') or body}")
        data = body['data']
        if isinstance(data, list):
            return {'lines': [{'text': text} for text in data]}
        # layout_classes_id 为 1 是页眉、页脚、版心等，组装正文时跳过。
        return {'vertical': data.get('text_angel') == 1,
                'lines': [{'text': line['text'], 'box': line.get('position'),
                           'layout': line.get('layout_classes_id')} for line in data.get('text_lines', [])]}


class Rapid:
    """本地 RapidOCR（onnxruntime，Apache-2.0）。竖排按列从右到左、列内从上到下排序。"""
    id = 'rapidocr-v1'
    label = '本地 RapidOCR'
    price_note = '在服务器本地运行，不上传'

    def __init__(self):
        self.engine = None

    def available(self):
        try:
            import rapidocr_onnxruntime  # noqa: F401
        except ImportError:
            return False
        return True

    def recognize(self, image: bytes) -> dict:
        if self.engine is None:
            from rapidocr_onnxruntime import RapidOCR
            self.engine = RapidOCR()
        result, _ = self.engine(image)
        lines = [{'text': text, 'box': [[float(x), float(y)] for x, y in box], 'score': float(score)}
                 for box, text, score in result or []]
        return order(lines)


def order(lines):
    """按文本框排阅读顺序：多数框瘦高视为竖排。"""
    if not lines:
        return {'vertical': False, 'lines': []}
    boxes = [(min(x for x, _ in l['box']), min(y for _, y in l['box']),
              max(x for x, _ in l['box']), max(y for _, y in l['box'])) for l in lines]
    vertical = sum((b[3] - b[1]) > 1.5 * (b[2] - b[0]) for b in boxes) * 2 > len(boxes)
    # 竖排：列按中心 x 从右到左分组，组内按 y；横排：行按中心 y 分组，组内按 x。
    across, along = (0, 1) if vertical else (1, 0)
    centers = [((b[across] + b[across + 2]) / 2, b[along]) for b in boxes]
    size = sorted(b[across + 2] - b[across] for b in boxes)[len(boxes) // 2] or 1
    ranked = sorted(range(len(lines)), key=lambda i: -centers[i][0] if vertical else centers[i][0])
    groups, last = [], None
    for i in ranked:
        if last is None or abs(centers[i][0] - last) > size / 2:
            groups.append([])
        groups[-1].append(i)
        last = centers[i][0]
    return {'vertical': vertical,
            'lines': [lines[i] for group in groups for i in sorted(group, key=lambda i: centers[i][1])]}


ENGINES = {engine.id: engine for engine in (Kandian(), Rapid())}


def engines():
    return [{'id': e.id, 'label': e.label, 'available': e.available(), 'note': e.price_note}
            for e in ENGINES.values()]


def files(paths):
    found = []
    for name in paths:
        path = Path(name)
        if path.is_dir():
            found.extend(p for p in path.rglob('*') if p.is_file() and p.suffix.lower() in SUFFIXES)
        elif path.suffix.lower() in SUFFIXES:
            found.append(path)
    return sorted(set(found))


def _sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b''):
            digest.update(chunk)
    return digest.hexdigest()


def page_count(path):
    if path.suffix.lower() not in PDF:
        return 1
    output = subprocess.run(['pdfinfo', str(path)], capture_output=True, text=True, check=True).stdout
    return int(re.search(r'^Pages:\s+(\d+)', output, re.M).group(1))


def _render(path, page, dpi):
    if path.suffix.lower() not in PDF:
        return path.read_bytes()
    config.TMP_ROOT.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='midstream-ocr-', dir=config.TMP_ROOT) as directory:
        target = Path(directory) / 'page'
        subprocess.run(['pdftoppm', '-f', str(page), '-l', str(page), '-r', str(dpi), '-png', '-singlefile',
                        str(path), str(target)], check=True, capture_output=True)
        return target.with_suffix('.png').read_bytes()


def _directory(engine_id, sha, path):
    dpi = DPI if path.suffix.lower() in PDF else 0
    return config.OCR_ROOT / engine_id / sha[:2] / f'{sha}-{dpi}'


def _write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    os.replace(temporary, path)


def status(paths, engine_id):
    """各文件页数与已缓存页数；只读，不识别。"""
    items = []
    for path in files(paths):
        sha = _sha256(path)
        directory = _directory(engine_id, sha, path)
        pages = page_count(path)
        done = sum(1 for page in range(1, pages + 1) if (directory / f'{page:05d}.json').exists())
        items.append({'path': str(path), 'sha256': sha, 'pages': pages, 'cached': done})
    return {'engine': engine_id, 'files': items, 'pages': sum(i['pages'] for i in items),
            'cached': sum(i['cached'] for i in items),
            'remaining': sum(i['pages'] - i['cached'] for i in items)}


def run(paths, engine_id, *, max_pages=None, progress=None):
    """识别缺缓存的页，最多 max_pages 页；已缓存的页不计入上限、不重复调用。"""
    engine = ENGINES[engine_id]
    plan = status(paths, engine_id)
    recognized, done, stopped = 0, 0, False
    for item in plan['files']:
        path = Path(item['path'])
        directory = _directory(engine_id, item['sha256'], path)
        for page in range(1, item['pages'] + 1):
            target = directory / f'{page:05d}.json'
            if target.exists():
                continue
            if max_pages is not None and recognized >= max_pages:
                stopped = True
                break
            if progress:
                progress(done, plan['remaining'], f'OCR：{path.name} 第 {page}/{item["pages"]} 页')
            result = engine.recognize(_render(path, page, DPI))
            _write_json(target, {'engine': engine_id, 'page': page, **result})
            recognized += 1
            done += 1
        if stopped:
            break
    after = status(paths, engine_id)
    return {'engine': engine_id, 'files': len(after['files']), 'pages': after['pages'],
            'recognized': recognized, 'cached': after['cached'], 'remaining': after['remaining'],
            'stopped_at_limit': stopped}


def page_text(result):
    return '\n'.join(line['text'] for line in result['lines'] if line.get('layout') != 1)


# 正文页判别（ocr-scan-pages-v1，2026-11-25 按《新青年》两期 224 页逐页核对）。
# 价格只认完整写法；横排招牌被竖排读反成“價定”“行發”“街盤棋”，只出现在广告里。
_PRICE = re.compile(r'定價|價定|大洋|精裝|洋裝|郵費|發行所|行發|行印|街盤棋|[一二三四五六七八九十百]+[元角]')
_ARTICLE_END = re.compile(r'[（︵(]\s*(?:未完|完|續完)\s*[）︶)]')
_FRONT = re.compile(r'目次|目錄|正誤表')
_AD_WORDS = re.compile(r'本書|是書|此書|是編|本社|著者|原著')


def _median(values):
    values = sorted(values)
    return values[len(values) // 2] if values else 0


def body_lines(lines):
    """去掉封面、目录、正误表和广告，返回正文行。

    行长中位数不超过 4 的页（目录、封面、读乱的页）和页首三行有“目次/目錄/正誤表”的页整页去掉。
    一页有两处以上价格写法即含广告，广告在页底：从第一处价格往前找文章结束标记“（未完）”“（完）”，
    有就从标记后截断；没有时往前最多 12 行找广告的书名行（不超过 9 字，连同紧挨着的作者、译者短行），
    从那里截断；找不到时，价格前的行像正文（行长中位数 ≥ 24）就保留到价格前，否则整页都是广告。
    留下的部分若有“本書”“是書”“著者”等广告措辞，说明整页都是广告。
    """
    if not lines or _median(len(line) for line in lines) <= 4:
        return []
    if any(_FRONT.search(line) for line in lines[:3]):
        return []
    if sum(len(_PRICE.findall(line)) for line in lines) < 2:
        return lines
    first = next(i for i, line in enumerate(lines) if _PRICE.search(line))
    ends = [i for i in range(first) if _ARTICLE_END.search(lines[i])]
    if ends:
        return lines[:ends[-1] + 1]
    kept = None
    for i in range(first - 1, max(-1, first - 13), -1):
        if len(lines[i]) <= 9:
            while i > 0 and len(lines[i - 1]) <= 9:
                i -= 1
            kept = lines[:i]
            break
    if kept is None:
        kept = lines[:first] if _median(len(line) for line in lines[:first]) >= 24 else []
    # 整页几则广告时，截断点是后一则的书名，前一则的说明还在：留下的部分有广告措辞就整页去掉。
    return [] if any(_AD_WORDS.search(line) for line in kept) else kept


def body_text(result):
    return '\n'.join(body_lines([line['text'] for line in result['lines'] if line.get('layout') != 1]))


def cached_pages(meta):
    """按入库时记下的 raw_fields.ocr（引擎、文件哈希、DPI、页数）读回逐页结果。"""
    directory = config.OCR_ROOT / meta['engine'] / meta['sha256'][:2] / f"{meta['sha256']}-{meta.get('dpi') or 0}"
    return [json.loads((directory / f'{page:05d}.json').read_text(encoding='utf-8'))
            for page in range(1, meta['pages'] + 1)]


def load(path, engine_id):
    """入库读取：全部页都有缓存时返回正文和页起点；否则返回缺页信息。"""
    path = Path(path)
    sha = _sha256(path)
    directory = _directory(engine_id, sha, path)
    pages = page_count(path)
    texts, missing = [], 0
    for page in range(1, pages + 1):
        target = directory / f'{page:05d}.json'
        if not target.exists():
            missing += 1
            continue
        texts.append(page_text(json.loads(target.read_text(encoding='utf-8'))))
    meta = {'engine': engine_id, 'sha256': sha, 'pages': pages}
    if missing:
        return None, {**meta, 'error': f'尚未完成 OCR：{pages - missing}/{pages} 页已识别'}
    starts, offset = [], 0
    for text in texts:
        starts.append(offset)
        offset += len(text) + 1
    return '\n'.join(texts), {**meta, 'dpi': DPI if path.suffix.lower() in PDF else None, 'page_starts': starts}
