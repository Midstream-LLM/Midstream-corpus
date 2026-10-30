"""比较压缩方案。只读数据库，不改动任何数据。

字典用 80% 的文本训练，压缩率在留出的 20% 上计算。
"""

import hashlib
import json
import sys
from pathlib import Path

import psycopg
import zstandard

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))
from midstream import config, textcodec  # noqa: E402

BUCKETS = [(0, 50), (50, 500), (500, 5000), (5000, 10**9)]
DICT_SIZES = [32 * 1024, 112 * 1024, 512 * 1024]
LEVELS = [3, 9, 19]


def held_out(text_hash: bytes) -> bool:
    return hashlib.sha256(text_hash).digest()[0] < 51


def main() -> None:
    train: list[bytes] = []
    test: list[bytes] = []
    with psycopg.connect(config.dsn()) as conn:
        rows = conn.execute("SELECT text_hash, content FROM text_blob").fetchall()
    for text_hash, content in rows:
        data = textcodec.decompress(bytes(content)).encode("utf-8")
        (test if held_out(bytes(text_hash)) else train).append(data)

    results = []
    for level in LEVELS:
        compressor = zstandard.ZstdCompressor(level=level)
        results.append(("none", level, measure(compressor, test)))
    for dict_size in DICT_SIZES:
        dictionary = zstandard.train_dictionary(dict_size, train, threads=-1)
        for level in LEVELS:
            compressor = zstandard.ZstdCompressor(level=level, dict_data=dictionary)
            results.append((f"dict{dict_size // 1024}k", level, measure(compressor, test)))
    print(json.dumps(
        {"train_docs": len(train), "test_docs": len(test),
         "test_plain_bytes": sum(map(len, test)), "results": [
             {"dict": name, "level": level, **values} for name, level, values in results]},
        ensure_ascii=False, indent=1,
    ))


def measure(compressor, texts: list[bytes]) -> dict:
    plain = [0] * len(BUCKETS)
    stored = [0] * len(BUCKETS)
    for data in texts:
        chars = len(data.decode("utf-8"))
        index = next(i for i, (low, high) in enumerate(BUCKETS) if low <= chars < high)
        plain[index] += len(data)
        stored[index] += len(compressor.compress(data))
    return {
        "ratio": round(sum(plain) / sum(stored), 3),
        "by_chars": {
            f"{low}-{high}": round(p / s, 3)
            for (low, high), p, s in zip(BUCKETS, plain, stored) if s
        },
    }


if __name__ == "__main__":
    main()
