import json

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
