"""清洗规则与质量判定。

clean-v3 只修明确的技术问题：换行、控制字符、缺字写法尽量还原（见 corpus.glyphs），OCR 来源合并逐字排开的单字。
繁简、异体字、标点、引号和全角空格都保持原样。
"""

import re

RULE_VERSION = "clean-v3"
QUALITY_RULE_VERSION = "quality-v3"

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_CJK = re.compile(r"[㐀-䶿一-鿿\U00020000-\U0002ffff]")
_SPACE = re.compile(r"\s+")
# 《明实录》校勘记混在正文里，形如“莊皇帝:廣本莊作章”
_COLLATION_NOTE = re.compile(r":[㐀-䶿一-鿿\U00020000-\U0002ffff「]")

MIN_CJK_RATIO = 0.3
# 编码错配的典型残留：UTF-8 当 GBK 读出的“锟斤拷”，未初始化内存的“烫烫烫/屯屯屯”，
# UTF-8 当 Latin-1 读出的 Ã、â€ 等。
_MOJIBAKE = re.compile(r"锟斤拷|烫烫烫|屯屯屯|Ã[\x80-\xbf]|â€|Â[\xa0-\xbf]")
MOJIBAKE_MIN = 3
# 不重复行占比低于此值视为整段重复（思路来自 MNBVC deduplication_mnbvc 的“待查文件”）；
# 只看 4 字以上的行，少于 20 行的短文本不判，避免诗词叠句误报。
# 只作提示、不改质量状态：2026-11-16 抽样，书籍里的重复多是目录与正文同名的回目、“有诗为证”
# 一类套语和本草条目，团队文本 32%、殆知阁 12% 会命中；真正的样板（OCR 期刊的页眉）比例与之重叠。
_LINE_MIN_CHARS, _LINE_MIN_COUNT, MIN_UNIQUE_LINE_RATIO = 4, 20, 0.95
FLAGGED_BY = {"replacement_char", "low_cjk_ratio", "embedded_collation_note", "mojibake"}


def clean_text(text: str, *, ocr: bool = False) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = normalize_glyphs(_CONTROL.sub("", text))
    return collapse_spaced_chars(text) if ocr else text


MISSING = "□"


def normalize_glyphs(text: str) -> str:
    """缺字写法：能还原的换成字或表意文字描述序列，不带信息的“■”“〓”和私用区字符换成“□”（见 corpus.glyphs）。"""
    from .glyphs import restore
    return restore(text)


_GLYPH = r"[\u2e80-\u2fdf\u3005\u3007\u3105-\u312f\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\ue000-\uf8ff\U00020000-\U0003134f]"
# OCR 把逐字排开的标题、表头识别成“天 子 西 征”；只合并连续 3 个以上的单字，
# 人名列表、表格里多字之间的空格是真分隔，保留。
_SPACED = re.compile(rf"(?<!\S){_GLYPH}(?: {_GLYPH}){{2,}}(?!\S)")


def collapse_spaced_chars(text: str) -> str:
    return _SPACED.sub(lambda m: m.group(0).replace(" ", ""), text)


def assess(text: str) -> tuple[str, list[str]]:
    """返回 (quality_status, quality_flags)。"""
    flags = []
    if "�" in text:
        flags.append("replacement_char")
    if _COLLATION_NOTE.search(text):
        flags.append("embedded_collation_note")
    visible = len(_SPACE.sub("", text))
    if visible and len(_CJK.findall(text)) / visible < MIN_CJK_RATIO:
        flags.append("low_cjk_ratio")
    if len(_MOJIBAKE.findall(text)) >= MOJIBAKE_MIN:
        flags.append("mojibake")
    lines = [line.strip() for line in text.split("\n")]
    lines = [line for line in lines if len(line) >= _LINE_MIN_CHARS]
    if len(lines) >= _LINE_MIN_COUNT and len(set(lines)) / len(lines) < MIN_UNIQUE_LINE_RATIO:
        flags.append("repeated_lines")
    status = "flagged" if FLAGGED_BY.intersection(flags) else "ok"
    return status, flags
