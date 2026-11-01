"""仅从本地 JSON 加载的并行分词器，不依赖数据库。"""

import hashlib
from dataclasses import dataclass
from pathlib import Path

from tokenizers import Tokenizer as Backend


@dataclass(frozen=True)
class Tokenizer:
    name: str
    fingerprint: str
    vocab_size: int
    _backend: Backend

    def count_batch(self, texts: list[str]) -> list[int]:
        return [len(item.ids) for item in self._backend.encode_batch(texts, add_special_tokens=False)]

    def encode_batch(self, texts: list[str]) -> list[list[int]]:
        return [item.ids for item in self._backend.encode_batch(texts, add_special_tokens=False)]


def load(path: str | Path) -> Tokenizer:
    path = Path(path).expanduser()
    if path.is_dir():
        path = path / 'tokenizer.json'
    data = path.read_bytes()
    backend = Backend.from_str(data.decode('utf-8'))
    backend.no_padding()
    backend.no_truncation()
    if getattr(backend.model, 'dropout', None):
        raise ValueError('缓存统计不接受启用随机 dropout 的分词器')
    size = backend.get_vocab_size(with_added_tokens=True)
    if max(backend.get_vocab(with_added_tokens=True).values(), default=-1) >= size:
        raise ValueError('分词器 token ID 必须位于 vocab_size 范围内')
    name = path.parent.name if path.name == 'tokenizer.json' else path.stem
    return Tokenizer(name, hashlib.sha256(data).hexdigest(), size, backend)
