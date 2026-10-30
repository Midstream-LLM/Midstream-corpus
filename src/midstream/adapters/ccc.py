"""Chinese Classical Corpus 的 corpus.jsonl。"""

from pathlib import Path

from ..config import JISHUI_RAW
from .base import JsonlAdapter, JsonlChunk, ParsedRecord, iter_jsonl

TEXT_FIELD = "content"


def _title(record: dict) -> str | None:
    book = record.get("source") or ""
    detail = next((record[k] for k in ("chapter", "title", "char", "section") if record.get(k)), "")
    if detail.startswith(book):
        return detail or None
    return f"{book} {detail}".strip() or None


def _unit_type(record: dict) -> str:
    if record.get("char"):
        return "entry"
    if record.get("title"):
        return "poem"
    return "section"


class CccCorpusAdapter(JsonlAdapter):
    name = "ccc_corpus"
    parser_version = "ccc-corpus-v1"
    description = "Chinese Classical Corpus：十三经、说文解字、资治通鉴和二十四史前 15 部"
    license_note = "cc0-1.0（数据集 README 声明）"

    def default_paths(self) -> list[Path]:
        return [JISHUI_RAW / "chinese-classical-corpus/corpus.jsonl"] if JISHUI_RAW else []

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
            author = record.get("author")
            metadata = {
                "title": _title(record),
                "authors": [author] if author else None,
                "composed_label": record.get("era") or None,
                "category": record.get("category") or None,
                "license": "cc0-1.0",
                "unit_type": _unit_type(record),
                "work_title": record.get("source") or None,
                "work_relation": "part",
                "field_basis": {
                    "title": "source",
                    "authors": "source",
                    "composed_label": "source",
                    "category": "source",
                    "license": "source",
                },
            }
            records.append(ParsedRecord(str(key), locator, raw_fields, text=text, metadata=metadata))
        return records
