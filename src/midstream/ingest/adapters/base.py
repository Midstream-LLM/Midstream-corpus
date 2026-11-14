"""来源适配器的公共定义。"""

import json
from dataclasses import dataclass, field
from io import BytesIO
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

    def iter_chunks(self, path: Path) -> Iterator[object]: ...

    def parse_chunk(self, chunk: object) -> list[ParsedRecord]: ...

    def parse(self, path: Path) -> Iterator[ParsedRecord]: ...


@dataclass(frozen=True)
class JsonlChunk:
    data: bytes
    line: int
    offset: int


class JsonlAdapter:
    def iter_chunks(self, path: Path) -> Iterator[JsonlChunk]:
        with open(path, "rb") as handle:
            line, offset = 1, 0
            while data := handle.read(8 << 20):
                if not data.endswith(b"\n"):
                    data += handle.readline()
                yield JsonlChunk(data, line, offset)
                line += data.count(b"\n")
                offset += len(data)

    def parse(self, path: Path) -> Iterator[ParsedRecord]:
        for chunk in self.iter_chunks(path):
            yield from self.parse_chunk(chunk)


def iter_jsonl(chunk: JsonlChunk) -> Iterator[tuple[int, int, dict | None, str | None]]:
    """逐行产出 (行号, 字节偏移, 记录, 错误)。坏行不中断，留给调用方记录。"""
    with BytesIO(chunk.data) as handle:
        offset = chunk.offset
        for line_no, line in enumerate(handle, chunk.line):
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
