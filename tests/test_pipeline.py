"""端到端测试，需要 MIDSTREAM_TEST_DSN 指向一个专用空库。"""

import json
import os
import random
from pathlib import Path

import psycopg
import pyarrow.parquet as pq
import pytest

from midstream import dedup, export, ingest, query, reprocess
from midstream.adapters import ADAPTERS
from midstream.rules import RULES

DSN = os.environ.get("MIDSTREAM_TEST_DSN")
pytestmark = pytest.mark.skipif(not DSN, reason="未设置 MIDSTREAM_TEST_DSN")
SCHEMA = Path(__file__).parents[1] / "migrations/versions/0001_initial.sql"


def row(key, text, chapter="史记‧卷一", book="史记", era="汉"):
    return json.dumps(
        {"id": key, "source": book, "author": "司马迁", "era": era, "category": "史",
         "chapter": chapter, "content": text},
        ensure_ascii=False,
    )


@pytest.fixture()
def conn():
    with psycopg.connect(DSN) as connection:
        connection.execute("DROP SCHEMA public CASCADE")
        connection.execute("CREATE SCHEMA public")
        for statement in SCHEMA.read_text(encoding="utf-8").split(";\n"):
            if statement.strip():
                connection.execute(statement)
        for statement in SCHEMA.with_name("0002_fingerprint.sql").read_text(encoding="utf-8").split(";\n"):
            if statement.strip():
                connection.execute(statement)
        for statement in SCHEMA.with_name("0003_dup_span.sql").read_text(encoding="utf-8").split(";\n"):
            if statement.strip():
                connection.execute(statement)
        connection.execute(SCHEMA.with_name("0004_staging_hash.sql").read_text(encoding="utf-8"))
        connection.commit()
        yield connection


def write(path, lines):
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def test_ingest_query_update_export(conn, tmp_path):
    adapter = ADAPTERS["ccc_corpus"]
    first = write(tmp_path / "a.jsonl", [
        row("a#1", "黄帝者，少典之子\r\n姓公孙"),
        row("a#2", "帝颛顼高阳者"),
        row("b#1", "项籍者，下相人也", chapter="汉书‧卷一", book="汉书"),
        "{坏行",
    ])
    report = ingest.ingest(conn, adapter, [first])
    assert (report.records_ok, report.records_failed) == (3, 1)
    assert report.new_documents == 3
    assert report.new_versions == 6
    assert conn.execute("SELECT count(*) FROM staging_text").fetchone()[0] == 0

    # 同内容文件不能再发布一次
    with pytest.raises(ingest.AlreadyImported):
        ingest.ingest(conn, adapter, [first])

    hits = query.search(conn, query.Filters(title_prefix="史记", year_from=100, year_to=150))
    assert [hit["year_from"] for hit in hits] == [-202, -202]
    assert query.search(conn, query.Filters(year_from=1000, year_to=1100)) == []
    document = query.get_text(conn, hits[0]["doc_id"])
    assert document["version_no"] == 2
    assert document["text"] == "黄帝者，少典之子\n姓公孙"
    assert query.get_text(conn, hits[0]["doc_id"], 1)["text"] == "黄帝者，少典之子\r\n姓公孙"

    recipe = export.Recipe.load({
        "name": "t1", "seed": "s", "splits": {"train": 1.0},
        "filters": {"sources": ["ccc_corpus"]},
    })
    release = export.create_release(conn, recipe)
    assert export.verify_release(conn, release)
    files = export.write_parquet(conn, release, tmp_path / "t1")
    table = pq.read_table(files[0])
    assert table.num_rows == 3
    assert sorted(table.column("ordinal").to_pylist()) == [0, 1, 2]

    # 同一来源的改动产生新版本，未变记录不动，已发布数据集不受影响
    second = write(tmp_path / "b.jsonl", [
        row("a#1", "黄帝者，少典之子，改"),
        row("a#2", "帝颛顼高阳者"),
        row("b#1", "项籍者，下相人也", chapter="汉书‧卷一", book="汉书"),
    ])
    report = ingest.ingest(conn, adapter, [second])
    assert report.unchanged == 2 and report.new_versions == 2 and report.new_documents == 0
    assert query.get_text(conn, hits[0]["doc_id"])["version_no"] == 4
    assert conn.execute(
        "SELECT count(*) FROM document_version WHERE is_current"
    ).fetchone()[0] == 3
    assert export.verify_release(conn, release)

    # 同一作品的文档必须落在同一侧
    splits = conn.execute(
        """SELECT w.work_id, count(DISTINCT m.split)
           FROM dataset_member m
           JOIN document_version v USING (version_id)
           JOIN document_work w USING (doc_id)
           GROUP BY w.work_id"""
    ).fetchall()
    assert all(count == 1 for _, count in splits)


def test_training_history_blocks_held_out_work(conn, tmp_path):
    adapter = ADAPTERS["ccc_corpus"]
    path = write(tmp_path / "a.jsonl", [row("a#1", "甲文"), row("a#2", "乙文")])
    ingest.ingest(conn, adapter, [path])
    used = export.create_release(conn, export.Recipe.load({
        "name": "used", "seed": "s", "splits": {"train": 1.0},
    }))
    export.record_training(conn, "used", "run-1")
    assert used
    with pytest.raises(export.TrainingLeak):
        export.create_release(conn, export.Recipe.load({
            "name": "held", "seed": "s", "splits": {"val": 1.0},
        }))
    conn.rollback()


def test_failed_stage_leaves_no_partition(conn, tmp_path):
    adapter = ADAPTERS["ccc_corpus"]
    path = write(tmp_path / "a.jsonl", [row("a#1", "甲"), row("a#1", "乙")])
    with pytest.raises(psycopg.errors.UniqueViolation):
        ingest.ingest(conn, adapter, [path])
    conn.rollback()
    assert conn.execute(
        "SELECT status FROM source_batch ORDER BY batch_id DESC LIMIT 1"
    ).fetchone()[0] == "failed"
    assert conn.execute("SELECT count(*) FROM document").fetchone()[0] == 0


def test_reprocess(conn, tmp_path):
    path = write(tmp_path / "wanli.jsonl", [json.dumps({
        "id": "m1", "title": "神宗顯皇帝實錄 卷九",
        "raw_text": "朝埨朝埨:廣本抱本埨作。明史諸王表與館本同。永寧府",
    }, ensure_ascii=False)])
    ingest.ingest(conn, ADAPTERS["wanli"], [path])
    # 模拟旧质量规则发布的版本。
    conn.execute(
        "UPDATE document_version SET quality_status = 'ok', quality_flags = '{}' WHERE is_current"
    )
    conn.commit()

    def current():
        return conn.execute(
            "SELECT doc_id, version_id, version_no, parent_version_id, text_hash"
            " FROM document_version WHERE is_current"
        ).fetchone()

    def counts():
        return conn.execute(
            "SELECT (SELECT count(*) FROM document_version),"
            " (SELECT count(*) FROM processing_run), (SELECT count(*) FROM text_blob)"
        ).fetchone()

    original = current()
    before_counts = counts()
    quality = RULES["quality-v2"]
    preview = reprocess.reprocess(conn, quality, dry_run=True)
    assert preview["changed"] == 1
    assert counts() == before_counts and current() == original
    assert reprocess.reprocess(conn, quality)["changed"] == 1
    metadata_only = current()
    assert metadata_only[2] == original[2] + 1
    assert metadata_only[3] == original[1]
    assert metadata_only[4] == original[4]
    assert reprocess.reprocess(conn, quality)["changed"] == 0

    collation = RULES["wanli-collation-v1"]
    before_counts = counts()
    preview = reprocess.reprocess(conn, collation, dry_run=True)
    assert preview == reprocess.reprocess(conn, collation, dry_run=True)
    assert preview["changed"] == 1
    assert preview["samples"][0]["after"] == "朝埨永寧府"
    assert counts() == before_counts and current() == metadata_only
    assert reprocess.reprocess(conn, collation)["changed"] == 1
    updated = current()
    assert updated[2] == metadata_only[2] + 1
    assert updated[3] == metadata_only[1]
    assert updated[4] != metadata_only[4]
    document = query.get_text(conn, updated[0])
    assert document["text"] == "朝埨永寧府"
    assert document["quality_status"] == "ok" and document["quality_flags"] == []
    assert reprocess.reprocess(conn, collation)["changed"] == 0


def test_document_dedup(conn, tmp_path):
    rng = random.Random(762)
    alphabet = "天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏"
    base = "齐" + "".join(rng.choices(alphabet, k=1000))
    texts = [base, "齊，" + base[1:] + "！", base[:500] + "山川" + base[502:],
             "".join(rng.choices(alphabet, k=1000)), "齊，國！", "齐国"]
    path = write(tmp_path / "dedup.jsonl", [row(f"d#{i}", text) for i, text in enumerate(texts)])
    first = ingest.ingest(conn, ADAPTERS["ccc_corpus"], [path]).dedup
    # 本例只检查重复簇对导出的影响。
    conn.execute("DELETE FROM document_work")
    conn.commit()
    assert first["fingerprinted"] == 6
    assert first["clusters"] == 2 and first["members"] == 5
    members = conn.execute(
        """SELECT d.source_key, m.kind, v.is_current FROM dup_cluster_member m
           JOIN dup_cluster c USING (cluster_id) JOIN document_version v USING (version_id)
           JOIN document d USING (doc_id) WHERE c.run_id = %s ORDER BY d.source_key""",
        (first["run_id"],),
    ).fetchall()
    assert members == [("d#0", "exact", True), ("d#1", "exact", True),
                       ("d#2", "near_candidate", True), ("d#4", "exact", True),
                       ("d#5", "exact", True)]
    second = dedup.dedup(conn)
    assert second["fingerprinted"] == 0
    assert second["clusters"] == first["clusters"]
    docs = dict(conn.execute("SELECT source_key, doc_id FROM document"))
    before = export._group_keys(conn, list(docs.values()))
    assert before[docs["d#0"]] == before[docs["d#1"]] == before[docs["d#2"]]
    assert before[docs["d#4"]] == before[docs["d#5"]]
    replacements = {doc_id: "".join(rng.choices(alphabet, k=1000)) for doc_id in docs.values()}
    reprocess.reprocess(conn, reprocess.Rule(
        "dedup-test-v1", None, lambda doc: reprocess.Change(text=replacements[doc["doc_id"]])
    ))
    third = dedup.dedup(conn)
    assert third["clusters"] == 0 and third["fingerprinted"] == 6
    assert export._group_keys(conn, list(docs.values())) == {doc_id: doc_id for doc_id in docs.values()}
    release = export.create_release(conn, export.Recipe.load({
        "name": "dedup-latest", "seed": "s", "splits": {"train": 0.5, "val": 0.5},
    }))
    assert export.verify_release(conn, release)
    assert conn.execute(
        "SELECT count(*) FROM dup_cluster WHERE run_id = %s", (first["run_id"],)
    ).fetchone()[0] == 0


def test_span_dedup_pipeline(conn, tmp_path):
    rng = random.Random(762)
    shared = "，".join("".join(rng.choices("abcdefghijklmnopqrstuvwxyz", k=16)) for _ in range(8))
    texts = ["甲" * 91 + shared + "甲" * 79,
             "乙" * 37 + shared + "乙" * 43,
             "乙" * 37 + shared + "乙" * 43,
             "丙" * 150]
    path = write(tmp_path / "spans.jsonl", [row(f"s#{i}", text) for i, text in enumerate(texts)])
    report = ingest.ingest(conn, ADAPTERS["ccc_corpus"], [path])
    assert report.dedup_spans["rule_version"] == "span-v1"
    assert report.dedup_spans["representatives"] == 3
    assert report.dedup_spans["spans"] == 1
    versions = dict(conn.execute(
        "SELECT d.source_key, v.version_id FROM document d"
        " JOIN document_version v USING (doc_id) WHERE v.is_current"
    ))
    source, target = sorted((versions["s#0"], min(versions["s#1"], versions["s#2"])))
    starts = {versions["s#0"]: 91, versions["s#1"]: 37, versions["s#2"]: 37}

    def snapshot():
        return conn.execute(
            "SELECT run_id, version_id, start_char, end_char, source_version_id, source_start_char"
            " FROM dup_span ORDER BY version_id, start_char"
        ).fetchall()

    assert snapshot() == [(report.dedup_spans["run_id"], target, starts[target],
                           starts[target] + len(shared), source, starts[source])]
    first_run = report.dedup_spans["run_id"]
    second = dedup.dedup_spans(conn)
    assert second["compare_fingerprinted"] == 0 and second["spans"] == 1
    assert snapshot()[0][0] == second["run_id"] != first_run
    assert conn.execute(
        "SELECT finished_at IS NOT NULL, config->>'spans' FROM processing_run WHERE run_id = %s",
        (second["run_id"],),
    ).fetchone() == (True, "1")
    # 文档查重独立运行，不覆盖段落快照。
    dedup.dedup(conn)
    assert snapshot()[0][0] == second["run_id"]
    reprocess.reprocess(conn, reprocess.Rule(
        "span-test-v1", None, lambda doc: reprocess.Change(text=str(doc["doc_id"]) * 40)
    ))
    assert dedup.dedup_spans(conn)["spans"] == 0
    assert snapshot() == []
