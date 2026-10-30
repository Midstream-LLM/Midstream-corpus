import json
import random
import struct
from itertools import combinations

import midstream_dedup

from midstream import clean, eras, textcodec
from midstream.adapters import ADAPTERS
from midstream.adapters.ccc import _title
from midstream.export import Recipe, _assign_split
from midstream.ingest import _cleaned_metadata, _prepare
from midstream.adapters.base import ParsedRecord
from midstream.query import Filters, where_clause
from midstream.rules import RULES


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


def test_split_assignment_is_stable_and_rejects_bad_ratio():
    recipe = Recipe.load({"name": "x", "seed": "s", "splits": {"train": 0.9, "val": 0.1}})
    first = [_assign_split(recipe, group) for group in range(200)]
    assert first == [_assign_split(recipe, group) for group in range(200)]
    assert set(first) == {"train", "val"}


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
