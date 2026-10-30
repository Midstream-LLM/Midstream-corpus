"""发布层正文的哈希与 zstd 压缩。"""

import hashlib

import zstandard

ZSTD_LEVEL = 9
CODEC = "zstd"

_compressor = None
_decompressor = None


def text_hash(text: str) -> bytes:
    return hashlib.sha256(text.encode("utf-8")).digest()


def compress(text: str) -> bytes:
    global _compressor
    if _compressor is None:
        _compressor = zstandard.ZstdCompressor(level=ZSTD_LEVEL)
    return _compressor.compress(text.encode("utf-8"))


def decompress(data: bytes) -> str:
    global _decompressor
    if _decompressor is None:
        _decompressor = zstandard.ZstdDecompressor()
    return _decompressor.decompress(data).decode("utf-8")


def compress_many(texts: list[str]) -> list[bytes]:
    """进程池的工作函数。"""
    return [compress(text) for text in texts]
