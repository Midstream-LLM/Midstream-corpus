"""端到端测试，需要 MIDSTREAM_TEST_DSN 指向一个专用空库。"""

import json
import os
from pathlib import Path

import psycopg
import pyarrow.parquet as pq
import pytest

from midstream import export, ingest, query
from midstream.adapters import ADAPTERS

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
