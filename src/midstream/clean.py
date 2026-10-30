"""清洗规则与质量判定。

clean-v1 只修明确的技术问题。繁简、异体字、标点和全角空格都保持原样。
"""

import re

RULE_VERSION = "clean-v1"
QUALITY_RULE_VERSION = "quality-v2"

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")
_CJK = re.compile(r"[㐀-䶿一-鿿\U00020000-\U0002ffff]")
# 《明实录》校勘记混在正文里，形如“莊皇帝:廣本莊作章”
_COLLATION_NOTE = re.compile(r":[㐀-䶿一-鿿\U00020000-\U0002ffff「]")

MIN_CJK_RATIO = 0.3
FLAGGED_BY = {"replacement_char", "low_cjk_ratio", "embedded_collation_note"}


def clean_text(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return _CONTROL.sub("", text)


def assess(text: str) -> tuple[str, list[str]]:
    """返回 (quality_status, quality_flags)。"""
    flags = []
    if "�" in text:
        flags.append("replacement_char")
    if _COLLATION_NOTE.search(text):
        flags.append("embedded_collation_note")
    visible = sum(1 for ch in text if not ch.isspace())
    if visible and len(_CJK.findall(text)) / visible < MIN_CJK_RATIO:
        flags.append("low_cjk_ratio")
    status = "flagged" if FLAGGED_BY.intersection(flags) else "ok"
    return status, flags
