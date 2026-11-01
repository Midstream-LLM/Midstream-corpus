"""重新处理规则，按版本注册。"""

from . import clean, variants
from .reprocess import Change, Rule


def _quality(current: dict) -> Change | None:
    status, flags = clean.assess(current["text"])
    if (status, flags) != (current["quality_status"], current["quality_flags"]):
        return Change(metadata={"quality_status": status, "quality_flags": flags})
    return None


def _is_note(note: str) -> bool:
    """按语偶尔漏了句号，太长又不是「」引文的，多半吃进了正文。"""
    return len(note) <= 60 or ("「" in note and "」" in note)


def _wanli_collation(current: dict) -> Change | None:
    text = current["text"]
    start = 0
    prefixes = ("明史", "起居注", "廣本", "抱本", "北大", "館本", "影印")
    while (position := text.find(":", start)) != -1:
        size = next((n for n in range(min(30, position // 2), 0, -1)
                     if text[position - 2 * n:position - n] == text[position - n:position]), 0)
        end = text.find("。", position + 1)
        while end != -1 and text.startswith(prefixes, end + 1):
            end = text.find("。", end + 1)
        if size and end != -1 and _is_note(text[position:end]):
            text = text[:position - size] + text[end + 1:]
            start = position - size
        else:
            start = position + 1
    if text != current["text"]:
        return Change(text=text)
    return None


RULES = {
    rule.version: rule for rule in (
        Rule("quality-v2", None, _quality),
        Rule("wanli-collation-v1", ["wanli"], _wanli_collation),
        Rule("t2s-v1", None, lambda current: Change(text=variants.simplify(current['text'])),
             variant='simplified', config=variants.info()),
    )
}
