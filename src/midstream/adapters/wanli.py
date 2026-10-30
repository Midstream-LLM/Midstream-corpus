"""万历朝语料 wanli_shilu_corpus.jsonl，按来源条目入库。

记录的年月日是事件日期，不是成书时间，所以不写入 composed_years。
"""

from pathlib import Path

from ..config import JISHUI_RAW
from .base import JsonlAdapter, JsonlChunk, ParsedRecord, iter_jsonl

TEXT_FIELD = "raw_text"
WORK_TITLE = "神宗顯皇帝實錄"


class WanliAdapter(JsonlAdapter):
    name = "wanli"
    parser_version = "wanli-v1"
    description = "万历朝语料，条目标题为《神宗顯皇帝實錄》"
    license_note = "cc-by-4.0（击水清单标注，原始目录无许可证文件，待核实）"

    def default_paths(self) -> list[Path]:
        return [JISHUI_RAW / "wanli-dibao-corpus/wanli_shilu_corpus.jsonl"] if JISHUI_RAW else []

    def parse_chunk(self, chunk: JsonlChunk) -> list[ParsedRecord]:
        records = []
        for line_no, offset, record, error in iter_jsonl(chunk):
            locator = {"line": line_no, "offset": offset}
            if record is None:
                records.append(ParsedRecord(f"line:{line_no}", locator, {}, error=error))
                continue
            key = record.get("id")
            if not key:
                records.append(ParsedRecord(f"line:{line_no}", locator, record, error="缺少 id"))
                continue
            text = record.get(TEXT_FIELD)
            raw_fields = {k: v for k, v in record.items() if k != TEXT_FIELD}
            if not isinstance(text, str) or not text:
                records.append(ParsedRecord(str(key), locator, raw_fields, error="正文为空"))
                continue
            metadata = {
                "title": record.get("title") or None,
                "license": "cc-by-4.0",
                "unit_type": "article",
                "work_title": WORK_TITLE,
                "work_relation": "part",
                "extra": {
                    "event_date": {
                        "year": record.get("year"),
                        "month": record.get("month"),
                        "day": record.get("day"),
                    },
                    "wanli_year": record.get("wanli_year"),
                },
                "field_basis": {"title": "source", "license": "legacy_manifest"},
            }
            records.append(ParsedRecord(str(key), locator, raw_fields, text=text, metadata=metadata))
        return records
