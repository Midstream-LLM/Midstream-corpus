import json
import random
import struct
from itertools import combinations

import midstream_dedup

from midstream import clean, eras, textcodec
from midstream.adapters import ADAPTERS
from midstream.adapters.ccc import _title
from midstream.ingest import _cleaned_metadata, _prepare
from midstream.adapters.base import ParsedRecord
from midstream.query import Filters, where_clause
from midstream.rules import RULES
from midstream.server import tuned_settings


def test_tuned_settings():
    settings = tuned_settings(16 * 1024 ** 3, 8)
    assert settings["shared_buffers"] == "3276MB"
    assert settings["effective_cache_size"] == "8192MB"
    assert settings["maintenance_work_mem"] == "1638MB"
    assert settings["work_mem"] == "64MB"
    assert settings["max_parallel_workers"] == 8
    assert settings["max_parallel_workers_per_gather"] == 4
    assert settings["wal_compression"] == "lz4"
    assert settings["io_method"] == "worker"
    assert settings["listen_addresses"] == ""


def test_codec_round_trip():
    text = "齊高祖神武皇帝　姓高名欢\n𠀋"
    assert textcodec.decompress(textcodec.compress(text)) == text
    assert len(textcodec.text_hash(text)) == 32


def test_clean_keeps_text_form():
    assert clean.clean_text("甲\r\n乙\r丙\x07　丁，「戊」") == "甲\n乙\n丙　丁，「戊」"


def test_assess_flags():
    assert clean.assess("齊高祖神武皇帝") == ("ok", [])
    assert clean.assess("莊皇帝:廣本莊作章")[1] == ["embedded_collation_note"]
    assert clean.assess("abc def ghi 甲")[1] == ["low_cjk_ratio"]
    assert clean.assess("甲乙�丙丁")[0] == "flagged"


def test_wanli_collation():
    rule = RULES["wanli-collation-v1"]
    for before, after in (
        ("緊要緊要:抱本作要緊。宜併成造", "緊要宜併成造"),
        ("賈三近參賈三近參:廣本抱本近下有奏字。新推", "賈三近參新推"),
        ("朝埨朝埨:廣本抱本埨作。明史諸王表與館本同。永寧府", "朝埨永寧府"),
    ):
        assert rule.apply({"text": before}).text == after
    assert rule.apply({"text": "緊要:抱本作要緊。宜併成造"}) is None
    assert rule.apply({"text": "緊要緊要:抱本作" + "甲" * 70 + "。"}) is None


def test_era_unknown_is_not_guessed():
    assert eras.era_years("唐") == (618, 907)
    assert eras.era_years("汉") == (-202, 220)
    assert eras.era_years("未知朝代") is None
    assert eras.era_years(None) is None


def test_cleaned_metadata_records_rule_basis():
    derived = _cleaned_metadata({"composed_label": "唐", "field_basis": {"title": "source"}}, "甲乙")
    assert derived["composed_years"] == [618, 907]
    assert derived["field_basis"] == {"title": "source", "composed_years": "rule"}


def test_prepare_rejects_nul_and_empty():
    bad = ParsedRecord("k", {}, {}, text="甲\x00乙")
    assert _prepare(bad)[0].error
    empty = ParsedRecord("k", {}, {}, text="\x07 \n")
    assert _prepare(empty)[0].error


def test_ccc_title_does_not_repeat_book():
    assert _title({"source": "北齐书", "chapter": "北齐书‧卷一"}) == "北齐书‧卷一"
    assert _title({"source": "说文解字", "char": "一"}) == "说文解字 一"


def test_adapters_parse_fixtures(tmp_path):
    ccc = tmp_path / "corpus.jsonl"
    lines = [
        json.dumps({"id": "a#1", "source": "史记", "author": "司马迁", "era": "汉",
                    "category": "史", "chapter": "史记‧卷一", "content": "黄帝者，少典之子"},
                   ensure_ascii=False),
        "{坏行",
        json.dumps({"id": "a#2", "source": "史记", "content": ""}, ensure_ascii=False),
    ]
    ccc.write_text("\n".join(lines) + "\n", encoding="utf-8")
    records = list(ADAPTERS["ccc_corpus"].parse(ccc))
    assert records[0].metadata["composed_label"] == "汉"
    assert "content" not in records[0].raw_fields
    assert records[1].source_key == "line:2" and records[1].error
    assert records[2].error == "正文为空"

    wanli = tmp_path / "wanli.jsonl"
    wanli.write_text(json.dumps({
        "id": "m1", "year": 1573, "month": 1, "day": 3, "wanli_year": 1,
        "title": "神宗顯皇帝實錄 卷九", "raw_text": "○甲申",
    }, ensure_ascii=False) + "\n", encoding="utf-8")
    record = next(ADAPTERS["wanli"].parse(wanli))
    assert record.metadata["extra"]["event_date"]["year"] == 1573
    assert "composed_years" not in record.metadata


def test_where_clause_escapes_like_and_uses_overlap():
    where, params = where_clause(Filters(title_prefix="50%", year_from=800, year_to=850))
    assert params["title"] == "50\\%%"
    assert "&&" in where and "IS NULL" not in where
    where, _ = where_clause(Filters(year_from=800, include_unknown_years=True))
    assert "IS NULL" in where


def test_dedup_recall():
    rng = random.Random(762)
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    texts = []
    # 独立文本各配一个变体，覆盖短文、长文和多个已知编辑比例。
    for i in range(1000):
        length = (65, 128, 256, 512)[i % 4]
        original = "".join(rng.choices(alphabet, k=length))
        edited = list(original)
        ratio = (0, 0.002, 0.005, 0.01, 0.025, 0.05)[i % 6]
        for position in rng.sample(range(length), round(length * ratio)):
            edited[position] = rng.choice(alphabet.replace(edited[position], ""))
        texts.extend((original, "".join(edited)))
    sets = [{text[i:i + 8] for i in range(len(text) - 7)} for text in texts]
    # 独立用原始字符切片暴力对比，不借用 Rust 的哈希或 Jaccard 实现。
    truth = {}
    for a, left in enumerate(sets):
        for b in range(a + 1, len(sets)):
            intersection = len(left & sets[b])
            if intersection:
                truth[a, b] = intersection / (len(left) + len(sets[b]) - intersection)
    header = b"PGCOPY\n\xff\r\n\0" + bytes(8)

    def encode(rows):
        return header + b"".join(
            struct.pack("!h", len(row)) + b"".join(struct.pack("!i", len(f)) + f for f in row)
            for row in rows
        ) + b"\xff\xff"

    def decode(data):
        offset = 19
        while (count := struct.unpack_from("!h", data, offset)[0]) != -1:
            offset += 2
            fields = []
            for _ in range(count):
                size = struct.unpack_from("!i", data, offset)[0]
                offset += 4
                fields.append(data[offset:offset + size])
                offset += size
            yield fields

    def feed(method, data):
        # 头、行、字段和结束标志都可能跨块。
        parts = [data[:1], *(data[i:min(i + 4093, len(data) - 1)]
                            for i in range(1, len(data) - 1, 4093)), data[-1:]]
        return b"".join(method(part) or b"" for part in parts)

    def run(state, blobs):
        state.group_exact()
        state.build_lsh()
        while state.next_round():
            while batch := state.next_batch(100):
                feed(state.blobs, encode(
                    (version, blobs[int.from_bytes(version, "big")])
                    for [version] in reversed(list(decode(batch)))
                ))
                state.verify_batch()
        count, _, candidates, _ = state.clusters()
        members = feed(state.members, encode((i.to_bytes(8, "big"),) for i in range(count)))
        clusters = {}
        for cluster, version, _ in decode(header + members + b"\xff\xff"):
            clusters.setdefault(cluster, []).append(int.from_bytes(version, "big"))
        return {tuple(sorted(pair)) for group in clusters.values() for pair in combinations(group, 2)}, candidates

    blobs = [textcodec.compress(text) for text in texts]
    state = midstream_dedup.Deduper("recall-test", 32, 4, 0.8)
    fingerprints = feed(state.fingerprints, encode(
        (i.to_bytes(32, "big"), blob) for i, blob in enumerate(blobs)
    ))
    records = list(decode(header + fingerprints + b"\xff\xff"))
    assert state.fingerprinted == len(texts)
    assert all(rule == b"recall-test" for _, rule, _, _ in records)
    feed(state.versions, encode((h[-8:], compare, sig) for h, _, compare, sig in reversed(records)))
    found, _ = run(state, blobs)
    expected = {pair for pair, score in truth.items() if score >= 0.85}
    recall = len(found & expected) / len(expected)
    assert recall >= 0.99, recall
    assert all(truth.get(pair, 0) >= 0.8 for pair in found)

    # a 占住同一个桶却与 b/c 都不重复；第二轮仍须验证真实重复对 b–c。
    state = midstream_dedup.Deduper("recall-test", 32, 4, 0.8)
    base = texts[6]
    blocked = [textcodec.compress(text) for text in
               ("Z" * len(base), base, base[:-1] + ("X" if base[-1] != "X" else "Y"))]
    feed(state.versions, encode(
        (i.to_bytes(8, "big"), i.to_bytes(16, "big"), struct.pack("<128I", *([1] * 128)))
        for i in range(3)
    ))
    assert run(state, blocked) == ({(1, 2)}, 3)


def test_dedup_spans_exact_offsets(tmp_path):
    rng = random.Random(762)

    def fragment(n):
        value = "".join(rng.choices("abcdefghijklmnopqrstuvwxyz", k=n))
        return "，。\n".join(value[i:i + 11] for i in range(0, n, 11))

    chunks = [fragment(n) for n in (64, 65, 127, 256, 63)]
    texts = [
        "😀！" + "A" * 91 + chunks[0] + "A" * 89 + chunks[2] + "A" * 79,
        "B" * 33 + chunks[1] + "B" * 19 + chunks[3] + "B" * 41,
        "C" * 37 + chunks[0] + "C" * 49 + chunks[1] + "C" * 21 + chunks[3] + "C" * 43,
        "D" * 71 + chunks[2] + "D" * 83 + chunks[3] + "D" * 12 + chunks[0] + "D" * 71,
        "E" * 27 + chunks[4] + "E" * 31,
        "F" * 53 + chunks[4] + "F" * 17,
        "G" * 63,
        # 标点不同，仍按 compare-v1 匹配，偏移必须指向各自原文。
        "😀H！" + chunks[0].replace("，。\n", "；") + "H",
    ]
    overlap = fragment(180)
    offsets = [i for i, c in enumerate(overlap) if c.isascii() and c.isalnum()]
    texts.extend(("I" * 30 + overlap[:offsets[99] + 1] + "I" * 11,
                  "J" * 50 + overlap[offsets[80]:] + "J" * 27,
                  "K" * 15 + overlap + "K" * 11))
    header = midstream_dedup.Deduper.copy_header()

    def encode(rows):
        return header + b"".join(
            struct.pack("!h", len(row)) + b"".join(struct.pack("!i", len(f)) + f for f in row)
            for row in rows
        ) + b"\xff\xff"

    def decode(data):
        pos = 19
        while struct.unpack_from("!h", data, pos)[0] != -1:
            fields = []
            pos += 2
            for _ in range(6):
                size = struct.unpack_from("!i", data, pos)[0]
                pos += 4
                fields.append(int.from_bytes(data[pos:pos + size], "big"))
                pos += size
            yield fields

    def normalized(text):
        return "".join(c for c in text if c.isascii() and c.isalnum())

    # 独立逐条对角线扫描原文字符，建立所有 >=64 字匹配的覆盖真值。
    positions = [[i for i, c in enumerate(t) if c.isascii() and c.isalnum()] for t in texts]
    normalized_texts = [normalized(t) for t in texts]
    expected = [set() for _ in texts]
    for a, b in combinations(range(len(texts)), 2):
        left, right = normalized_texts[a], normalized_texts[b]
        for diagonal in range(1 - len(right), len(left)):
            i, j = max(diagonal, 0), max(-diagonal, 0)
            start = j
            while i < len(left) and j < len(right):
                if left[i] != right[j]:
                    if j - start >= 64:
                        expected[b].update(range(positions[b][start], positions[b][j - 1] + 1))
                    start = j + 1
                i += 1
                j += 1
            if j - start >= 64:
                expected[b].update(range(positions[b][start], positions[b][j - 1] + 1))

    state = midstream_dedup.SpanDeduper(str(tmp_path))
    data = encode((struct.pack("!q", i + 100), textcodec.compress(t)) for i, t in enumerate(texts))
    for i in range(0, len(data), 137):
        state.fingerprints(data[i:i + 137])
    assert state.counts()[:2] == (len(texts), len(texts) - 1)
    assert state.seeds()[0] > 0
    matches, spans = state.verify()
    output = bytearray(header)
    while block := state.next_output(42):
        output.extend(block)
    output.extend(b"\xff\xff")
    found = [set() for _ in texts]
    rows = list(decode(output))
    assert matches >= spans == len(rows) > 0
    assert len({(v, start) for _, v, start, _, _, _ in rows}) == len(rows)
    for run, version, start, end, source, source_start in rows:
        a, b = source - 100, version - 100
        assert run == 42 and a < b
        raw = texts[b][start:end]
        value = normalized(raw)
        assert len(value) >= 64
        source_positions = [p for p in positions[a] if p >= source_start]
        source_end = source_positions[len(value) - 1] + 1
        original = texts[a][source_start:source_end]
        assert value == normalized(original)  # 每一条均逐字核验，无哈希误报。
        if b != 7:
            assert raw == original  # 含标点的原文切片和偏移也完全一致。
        found[b].update(range(start, end))
    assert found == expected
    for b, indices in ((2, (0, 1, 3)), (3, (0, 2, 3))):
        for i in indices:
            start = texts[b].index(chunks[i])
            assert set(range(start, start + len(chunks[i]))) <= found[b]
    assert not found[4] and not found[5] and not found[6]


def test_dedup_reuses_positive_and_negative_scores():
    rng = random.Random(38)
    base = "".join(rng.choices("abcdefghijklmnopqrstuvwxyz", k=1000))
    texts = ["Z" * 1000, base, base[:-1] + "甲"]
    header = midstream_dedup.Deduper.copy_header()

    def encode(rows):
        return header + b"".join(
            struct.pack("!h", len(row)) + b"".join(struct.pack("!i", len(f)) + f for f in row)
            for row in rows
        ) + b"\xff\xff"

    def decode(data):
        offset = 19
        while (count := struct.unpack_from("!h", data, offset)[0]) != -1:
            offset += 2
            fields = []
            for _ in range(count):
                size = struct.unpack_from("!i", data, offset)[0]
                offset += 4
                fields.append(data[offset:offset + size])
                offset += size
            yield fields

    def run(threshold, cache, version_offset):
        state = midstream_dedup.Deduper("cache-test", 32, 4, threshold)
        # 同桶中先安排一个不相似的中心，确保未通过的候选也会被缓存。
        state.versions(encode(
            (struct.pack("!q", i + version_offset), i.to_bytes(16, "big"), struct.pack("<128I", *([1] * 128)))
            for i in range(3)
        ))
        state.group_exact()
        state.build_lsh()
        scores = {}
        while state.next_round():
            while state.next_batch(10) is not None:
                state.cached_pairs(encode(
                    (a, b, struct.pack("!d", cache[a, b]))
                    for a, b in decode(state.candidate_pairs()) if (a, b) in cache
                ))
                missing = state.uncached_batch()
                if missing is not None:
                    state.blobs(encode(
                        (version, textcodec.compress(texts[int.from_bytes(version, "big") - version_offset]))
                        for [version] in decode(missing)
                    ))
                    scores.update({(a, b): struct.unpack("!d", score)[0]
                                   for a, b, score in decode(state.verify_batch())})
        clusters, members, _, _ = state.clusters()
        return clusters, members, state.cache_hits, state.computed_pairs, scores

    cold = run(0.8, {}, 0)
    assert cold[:4] == (1, 2, 0, 3)
    assert sorted(cold[4].values())[:2] == [0.0, 0.0]
    # 换版本 ID 后仍可按比较正文哈希复用；阈值变化重新判定，不继承旧簇。
    assert run(0.8, cold[4], 100)[:4] == (1, 2, 3, 0)
    assert run(1.0, cold[4], 200)[:4] == (0, 0, 3, 0)
