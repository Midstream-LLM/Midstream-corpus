"""来源适配器的公共定义。"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Protocol


@dataclass(frozen=True)
class ParsedRecord:
    source_key: str
    locator: dict
    raw_fields: dict
    text: str | None = None
    error: str | None = None
    metadata: dict = field(default_factory=dict)


class Adapter(Protocol):
    name: str
    parser_version: str
    description: str
    license_note: str

    def default_paths(self) -> list[Path]: ...

    def parse(self, path: Path) -> Iterator[ParsedRecord]: ...


def iter_jsonl(path: Path) -> Iterator[tuple[int, int, dict | None, str | None]]:
    """逐行产出 (行号, 字节偏移, 记录, 错误)。坏行不中断，留给调用方记录。"""
    with open(path, "rb") as handle:
        offset = 0
        for line_no, line in enumerate(handle, 1):
            start = offset
            offset += len(line)
            if not line.strip():
                continue
            try:
                record = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                yield line_no, start, None, f"{type(exc).__name__}: {exc}"
                continue
            if not isinstance(record, dict):
                yield line_no, start, None, "记录不是 JSON 对象"
                continue
            yield line_no, start, record, None
