"""重新处理规则，按版本注册。"""

from datetime import date

from ..corpus import classify, clean, glyphs, infer, variants
from ..corpus.metadata import years
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


def headword(record: dict) -> str | None:
    """字书条目的字头在来源的 char 字段里，正文只有释文。"""
    char = record.get("char")
    return char if isinstance(char, str) and char.strip() else None


def _ccc_headword(current: dict) -> Change | None:
    char = headword(current["raw_fields"])
    if char and not current["text"].startswith(char + "，"):
        return Change(text=f"{char}，{current['text']}")
    return None


def _restore(current: dict) -> Change | None:
    """缺字写法能还原就还原（见 corpus.glyphs）。出自 glyph-normalize-v1 的版本从它之前的正文重做。"""
    text = glyphs.restore(current["parent_text"] or current["text"])
    if current["source"].endswith("_ocr"):
        text = clean.collapse_spaced_chars(text)
    return Change(text=text) if text != current["text"] else None


def _taxonomy(current: dict) -> Change | None:
    """按来源映射表补规范标签（时代、体裁、部类、地域）和年份，只填空维度。"""
    current = {**current, "composed_years": years(current["composed_years"])}
    patch = classify.merge(current, classify.classify(current["source"], current))
    return Change(metadata=patch) if patch else None


_INFERRED = {}


def _prepare_infer(conn) -> None:
    _INFERRED.clear()
    _INFERRED.update(infer.load(conn, date.today().year))


def _infer(current: dict) -> Change | None:
    """无来源原值的来源按正文和题名推断规范标签与年份（见 corpus.infer）。"""
    found = _INFERRED.get(current["doc_id"])
    if not found:
        return None
    current = {**current, "composed_years": years(current["composed_years"])}
    patch = infer.merge(current, found)
    return Change(metadata=patch) if patch else None


RULES = {
    rule.version: rule for rule in (
        Rule("quality-v3", None, _quality, step="quality_check"),
        Rule("wanli-collation-v1", ["wanli"], _wanli_collation, step="collation_clean"),
        Rule("ccc-headword-v1", ["ccc_corpus"], _ccc_headword, step="rule_reprocess"),
        # glyph-normalize-v1（2026-11-16，一律换成“□”）已由 glyph-restore-v1 取代，不再提供。
        Rule(glyphs.RULE_VERSION, None, _restore, step="rule_reprocess", redo_after="glyph-normalize-v1",
             config={"chise_ids": glyphs.data_info()}),
        Rule(classify.MAP_VERSION, None, _taxonomy, step="tagging", config={"maps": classify.versions()}),
        Rule(infer.RULE_VERSION, infer.SOURCES, _infer, step="tagging", prepare=_prepare_infer),
        Rule("t2s-v1", None, lambda current: Change(text=variants.simplify(current['text'])),
             variant='simplified', config=variants.info(), step='t2s'),
    )
}
