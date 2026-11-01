"""端到端测试，需要 MIDSTREAM_TEST_DSN 指向一个专用空库。"""

import json
import multiprocessing
import os
import random
import runpy
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import psycopg
import pyarrow.parquet as pq
import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import create_engine

from midstream import batches, dedup, export, ingest, management, query, reprocess
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
        connection.execute(SCHEMA.with_name("0005_release_char_count.sql").read_text(encoding="utf-8"))
        connection.execute(SCHEMA.with_name("0006_incremental_dedup.sql").read_text(encoding="utf-8"))
        connection.execute(SCHEMA.with_name("0007_manual_batch.sql").read_text(encoding="utf-8"))
        connection.execute(SCHEMA.with_name("0008_import_plan.sql").read_text(encoding="utf-8"))
        connection.execute(SCHEMA.with_name("0009_release_selection.sql").read_text(encoding="utf-8"))
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
    stats = export.release_stats(conn, release)
    assert stats["char_count"] == sum(len(text) for text in table.column("text").to_pylist())
    assert stats["split_char_counts"] == {"train": stats["char_count"], "val": 0, "test": 0}

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
    assert export.release_stats(conn, release) == stats

    # 同一作品的文档必须落在同一侧
    splits = conn.execute(
        """SELECT w.work_id, count(DISTINCT m.split)
           FROM dataset_member m
           JOIN document_version v USING (version_id)
           JOIN document_work w USING (doc_id)
           GROUP BY w.work_id"""
    ).fetchall()
    assert all(count == 1 for _, count in splits)


@pytest.mark.parametrize("splits", [
    {"train": 1.0}, {"val": 1.0}, {"test": 1.0},
    {"train": 0.4, "val": 0.3, "test": 0.3},
])
def test_release_char_counts(conn, tmp_path, splits):
    # 补充平面字、组合字符、全角空格和换行均按 Unicode 码点计数；重复成员分别计。
    texts = ["甲　乙，\n𠀋😀e\u0301", "甲　乙，\n𠀋😀e\u0301"]
    texts.extend(f"第{i}篇：古文。" for i in range(20))
    path = write(tmp_path / "chars.jsonl", [
        row(f"chars#{i}", text, book=f"书{i}") for i, text in enumerate(texts)
    ])
    ingest.ingest(conn, ADAPTERS["ccc_corpus"], [path])
    release = export.create_release(conn, export.Recipe.load({
        "name": "chars", "seed": "chars", "splits": splits,
        "filters": {"quality": ["ok", "flagged"]},
    }))
    files = export.write_parquet(conn, release, tmp_path / "export")
    exported = []
    expected = {"train": 0, "val": 0, "test": 0}
    for path in files:
        values = pq.read_table(path).column("text").to_pylist()
        exported.extend(values)
        expected[path.stem] = sum(map(len, values))
    assert sorted(exported) == sorted(texts)
    assert all(expected[split] > 0 for split in splits)
    assert export.release_stats(conn, release) == {
        "release_id": release, "name": "chars",
        "char_count": sum(map(len, texts)), "split_char_counts": expected,
        "selection": {"candidates": len(texts), "dropped_documents": 0, "dropped_chars": 0},
    }
    assert export.verify_release(conn, release)
    with pytest.raises(export.ExportError, match="不存在"):
        export.release_stats(conn, release + 1)

    # 总数及划分和仍一致的统计错误也必须被 verify 发现。
    conn.execute(
        "UPDATE dataset_release SET char_count = char_count + 1,"
        " train_char_count = train_char_count + 1 WHERE release_id = %s", (release,),
    )
    assert not export.verify_release(conn, release)


def test_release_char_count_migration(conn, tmp_path):
    path = write(tmp_path / "old.jsonl", [row("old#1", "古文，\n𠀋"), row("old#2", "古文，\n𠀋")])
    ingest.ingest(conn, ADAPTERS["ccc_corpus"], [path])
    releases = [export.create_release(conn, export.Recipe.load({
        "name": split, "seed": "old", "splits": {split: 1.0},
    })) for split in ("train", "val", "test")]
    before = [export.release_stats(conn, release) for release in releases]
    conn.commit()
    # 当前正文长度变化后，迁移仍须按旧数据集的固定版本回填。
    reprocess.reprocess(conn, reprocess.Rule(
        "chars-test-v1", None, lambda doc: reprocess.Change(text=doc["text"] + "新增"),
    ), dry_run=False)
    revision = runpy.run_path(str(SCHEMA.with_name("0005_release_char_count.py")))
    engine = create_engine("postgresql+psycopg://", creator=lambda: psycopg.connect(DSN))
    try:
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                revision["downgrade"]()
                connection.exec_driver_sql(
                    "INSERT INTO dataset_release (name, recipe, content_digest)"
                    " VALUES ('empty', '{}', %s)", (export._digest([]),),
                )
                revision["upgrade"]()
    finally:
        engine.dispose()
    assert [export.release_stats(conn, release) for release in releases] == before
    assert all(export.verify_release(conn, release) for release in releases)
    empty = conn.execute("SELECT release_id FROM dataset_release WHERE name = 'empty'").fetchone()[0]
    assert export.release_stats(conn, empty)["split_char_counts"] == {"train": 0, "val": 0, "test": 0}
    assert export.release_stats(conn, empty)["char_count"] == 0
    assert export.verify_release(conn, empty)
    for update in ("char_count = -1", "char_count = char_count + 1",
                   "char_count = 0, train_char_count = -1, val_char_count = 1, test_char_count = 0"):
        with pytest.raises(psycopg.errors.CheckViolation), conn.transaction():
            conn.execute(f"UPDATE dataset_release SET {update} WHERE release_id = %s", (empty,))


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


def test_manual_document_and_work_review(conn, tmp_path):
    path = write(tmp_path / "review.jsonl", [row("review#1", "甲乙丙")])
    ingest.ingest(conn, ADAPTERS["ccc_corpus"], [path])
    doc_id, old_id, old_no = conn.execute(
        "SELECT doc_id, version_id, version_no FROM document_version WHERE is_current"
    ).fetchone()
    old_meta = conn.execute(
        "SELECT title, quality_status, quality_flags, field_basis, text_hash"
        " FROM document_version WHERE version_id = %s", (old_id,),
    ).fetchone()
    # 保留一个非当前派生版本；后续人工修正仍应以当前版为父版、取最大版本号。
    conn.execute(
        "INSERT INTO document_version"
        " (doc_id, version_no, parent_version_id, text_hash, run_id, is_current,"
        " quality_status, quality_flags, char_count, field_basis, extra)"
        " SELECT doc_id, version_no + 1, version_id, text_hash, run_id, false,"
        " quality_status, quality_flags, char_count, field_basis, extra"
        " FROM document_version WHERE version_id = %s", (old_id,),
    )
    conn.commit()
    result = management.review_document(
        conn, doc_id, old_id,
        {"title": "人工核定标题", "quality_status": "flagged", "quality_flags": ["人工待核"],
         "composed_years": "[618,908)"},
        actor="test-operator", reason="合成测试人工核对",
    )
    assert result["status"] == "applied" and result["version_no"] == old_no + 2
    assert result["changed_fields"] == [
        "composed_years", "quality_flags", "quality_status", "title",
    ]
    assert conn.execute(
        "SELECT title, quality_status, quality_flags, field_basis, text_hash"
        " FROM document_version WHERE version_id = %s", (old_id,),
    ).fetchone() == old_meta
    current = conn.execute(
        "SELECT parent_version_id, title, quality_status, quality_flags, field_basis, text_hash"
        " FROM document_version WHERE version_id = %s", (result["version_id"],),
    ).fetchone()
    assert current[0] == old_id and current[1:4] == (
        "人工核定标题", "flagged", ["人工待核"],
    )
    assert all(current[4][field] == "human" for field in result["changed_fields"])
    assert current[5] == old_meta[4]
    assert str(conn.execute(
        "SELECT composed_years FROM document_version WHERE version_id = %s",
        (result["version_id"],),
    ).fetchone()[0]) == "[618, 908)"
    with pytest.raises(management.ManagementError) as exc:
        management.review_document(
            conn, doc_id, old_id, {"title": "又改"}, actor="test", reason="冲突",
        )
    assert exc.value.code == "conflict"
    with pytest.raises(management.ManagementError) as exc:
        management.review_document(
            conn, doc_id, result["version_id"], {"title": "人工核定标题"},
            actor="test", reason="无变化",
        )
    assert exc.value.code == "no_change"
    with pytest.raises(management.ManagementError) as exc:
        management.review_document(
            conn, doc_id, result["version_id"], {"foo": "x"},
            actor="test", reason="非法字段",
        )
    assert exc.value.code == "invalid_input"
    work_id = conn.execute(
        "INSERT INTO work (canonical_title) VALUES ('待核作品') RETURNING work_id"
    ).fetchone()[0]
    conn.execute(
        "INSERT INTO document_work (doc_id, work_id, relation, basis, status)"
        " VALUES (%s, %s, 'whole', 'rule', 'candidate')", (doc_id, work_id),
    )
    conn.commit()
    assert management.work_links(conn, doc_id, status="candidate") == [{
        "work_id": work_id, "canonical_title": "待核作品", "relation": "whole",
        "basis": "rule", "status": "candidate",
    }]
    decision = management.review_work(
        conn, doc_id, work_id, "whole", "confirmed",
        actor="test-operator", reason="依据源目录核对",
    )
    assert decision["decision"] == "confirmed"
    assert conn.execute(
        "SELECT basis, status FROM document_work WHERE doc_id = %s AND work_id = %s",
        (doc_id, work_id),
    ).fetchone() == ("rule", "confirmed")
    assert management.work_links(conn, doc_id, status="candidate") == []
    with pytest.raises(management.ManagementError) as exc:
        management.review_work(
            conn, doc_id, work_id, "whole", "rejected",
            actor="test", reason="状态冲突",
        )
    assert exc.value.code == "conflict"
    assert [event["action"] for event in management.events(conn, doc_id=doc_id)] == [
        "work_review", "document_review",
    ]


def test_batch_resume_and_abandon(conn, tmp_path, monkeypatch):
    adapter = ADAPTERS["ccc_corpus"]
    path = write(tmp_path / "resume.jsonl", [row("resume#1", "甲乙丙")])
    batch_id = ingest.register_batch(conn, adapter, [path])
    assert batches.inspect(conn, batch_id)["status"] == "received"
    # 原路径、大小、哈希任一变化都不能继续旧批次。
    path.write_text(path.read_text(encoding="utf-8") + "\n", encoding="utf-8")
    with pytest.raises(management.ManagementError) as exc:
        batches.resume(conn, batch_id, actor="test", reason="恢复")
    assert exc.value.code == "input_changed"
    assert batches.inspect(conn, batch_id)["status"] == "received"
    path.write_text(path.read_text(encoding="utf-8").rstrip("\n") + "\n", encoding="utf-8")
    original = path.read_text(encoding="utf-8")
    path.write_text(original.replace("甲", "乙"), encoding="utf-8")
    assert path.stat().st_size == len(original.encode("utf-8"))
    with pytest.raises(management.ManagementError) as exc:
        batches.resume(conn, batch_id, actor="test", reason="恢复")
    assert exc.value.code == "input_changed"
    path.write_text(original, encoding="utf-8")
    conn.execute(
        "UPDATE source_batch SET parser_version = 'obsolete-parser' WHERE batch_id = %s",
        (batch_id,),
    )
    conn.commit()
    with pytest.raises(management.ManagementError) as exc:
        batches.resume(conn, batch_id, actor="test", reason="恢复")
    assert exc.value.code == "not_recoverable"
    conn.execute(
        "UPDATE source_batch SET parser_version = %s WHERE batch_id = %s",
        (adapter.parser_version, batch_id),
    )
    conn.commit()
    # 发布后查重失败不会让恢复操作再次发布。
    real_dedup = batches.dedup.dedup
    monkeypatch.setattr(batches.dedup, "dedup", lambda _conn: (_ for _ in ()).throw(RuntimeError("dedup failed")))
    with pytest.raises(RuntimeError, match="dedup failed"):
        batches.resume(conn, batch_id, actor="test", reason="恢复")
    monkeypatch.setattr(batches.dedup, "dedup", real_dedup)
    assert batches.inspect(conn, batch_id)["status"] == "published"
    count = conn.execute("SELECT count(*) FROM document_version").fetchone()[0]
    assert batches.resume(conn, batch_id, actor="test", reason="重试")["status"] == "already_published"
    assert conn.execute("SELECT count(*) FROM document_version").fetchone()[0] == count
    assert len(management.events(conn, batch_id=batch_id)) == 1

    bad = write(tmp_path / "failed.jsonl", [row("bad#1", "甲"), row("bad#1", "乙")])
    bad_id = ingest.register_batch(conn, adapter, [bad])
    with pytest.raises(psycopg.errors.UniqueViolation):
        batches.resume(conn, bad_id, actor="test", reason="尝试恢复")
    assert batches.inspect(conn, bad_id)["status"] == "failed"
    abandoned = batches.abandon(conn, bad_id, actor="test", reason="输入有重复键")
    assert abandoned["status"] == "abandoned"
    assert not batches.inspect(conn, bad_id)["has_partition"]
    assert batches.abandon(conn, bad_id, actor="test", reason="重复请求")["status"] == "already_abandoned"
    with pytest.raises(management.ManagementError) as exc:
        batches.resume(conn, bad_id, actor="test", reason="不可恢复")
    assert exc.value.code == "not_recoverable"
    with pytest.raises(management.ManagementError) as exc:
        batches.abandon(conn, batch_id, actor="test", reason="不可放弃")
    assert exc.value.code == "not_recoverable"


def test_batch_staged_validated_and_needs_review(conn, tmp_path):
    adapter = ADAPTERS["ccc_corpus"]
    good = write(tmp_path / "staged.jsonl", [row("staged#1", "甲乙丙")])
    staged_id = ingest.register_batch(conn, adapter, [good])
    with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as pool:
        ingest.stage(conn, staged_id, adapter, pool)
    assert batches.inspect(conn, staged_id)["has_partition"]
    staged_view = batches.inspect(conn, staged_id)
    assert staged_view["staged"] == {"decoded": 1, "cleaned": 1}
    assert staged_view["validation_ready"] and staged_view["failure_samples"] == []
    assert batches.resume(conn, staged_id, actor="test", reason="恢复暂存批次")["status"] == "published"
    assert not batches.inspect(conn, staged_id)["has_partition"]

    validated = write(tmp_path / "validated.jsonl", [row("validated#1", "丁戊己")])
    validated_id = ingest.register_batch(conn, adapter, [validated])
    with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as pool:
        ingest.stage(conn, validated_id, adapter, pool)
    assert ingest.validate(conn, validated_id) == "validated"
    assert batches.resume(conn, validated_id, actor="test", reason="恢复已验证批次")["status"] == "published"

    invalid = write(tmp_path / "review.jsonl", ["{坏行"])
    review_id = ingest.register_batch(conn, adapter, [invalid])
    with ProcessPoolExecutor(max_workers=1, mp_context=multiprocessing.get_context("spawn")) as pool:
        ingest.stage(conn, review_id, adapter, pool)
    assert ingest.validate(conn, review_id) == "needs_review"
    review_view = batches.inspect(conn, review_id)
    assert not review_view["validation_ready"]
    assert len(review_view["failure_samples"]) == 1
    assert batches.resume(conn, review_id, actor="test", reason="再次检查")["status"] == "needs_review"
    before = conn.execute(
        "SELECT count(*) FROM source_record WHERE batch_id = %s", (review_id,),
    ).fetchone()[0]
    assert before == 1
    assert batches.abandon(conn, review_id, actor="test", reason="无有效记录")["status"] == "abandoned"
    assert not batches.inspect(conn, review_id)["has_partition"]
    assert conn.execute(
        "SELECT count(*) FROM source_record WHERE batch_id = %s", (review_id,),
    ).fetchone()[0] == before


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
    assert second["reused_snapshot"] and second["run_id"] == first["run_id"]
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


def test_export_selection_keeps_one_per_cluster(conn, tmp_path):
    rng = random.Random(763)
    alphabet = "天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏"
    def text():
        return "".join(rng.choices(alphabet, k=1000))
    base = text()
    path = write(tmp_path / "selection.jsonl", [
        row("short", base, chapter="甲书‧卷一", book="甲书"),
        row("long", base + "！", chapter="乙书‧卷一", book="乙书"),
        row("solo", text(), chapter="丙书‧卷一", book="丙书"),
    ])
    ingest.ingest(conn, ADAPTERS["ccc_corpus"], [path])
    conn.execute("INSERT INTO source (name) VALUES ('second')")
    second = conn.execute("SELECT source_id FROM source WHERE name = 'second'").fetchone()[0]
    docs = dict(conn.execute("SELECT source_key, doc_id FROM document"))
    conn.execute("UPDATE document SET source_id = %s WHERE doc_id = %s", (second, docs["short"]))
    conn.commit()

    def release(name, selection):
        recipe = {"name": name, "seed": "s", "splits": {"train": 1.0}, "selection": selection}
        release_id = export.create_release(conn, export.Recipe.load(recipe))
        members = {doc for doc, in conn.execute(
            "SELECT v.doc_id FROM dataset_member m JOIN document_version v USING (version_id)"
            " WHERE m.release_id = %s", (release_id,))}
        return release_id, members

    _, everything = release("all", {})
    assert everything == set(docs.values())
    longest, members = release("longest", {"dedup": "document"})
    assert members == {docs["long"], docs["solo"]}
    assert export.release_stats(conn, longest)["selection"] == {
        "candidates": 3, "dropped_documents": 1, "dropped_chars": 1000}
    assert export.verify_release(conn, longest)
    _, members = release("preferred", {"dedup": "document", "source_priority": ["second"]})
    assert members == {docs["short"], docs["solo"]}
    with pytest.raises(export.ExportError):
        export.Recipe.load({"name": "bad", "seed": "s", "splits": {"train": 1.0},
                            "selection": {"dedup": "span"}})
    reprocess.reprocess(conn, reprocess.Rule(
        "selection-test-v1", None, lambda doc: reprocess.Change(text=text())))
    with pytest.raises(export.ExportError, match="已过期"):
        release("stale", {"dedup": "document"})


def test_export_selection_splits_with_dropped_bridge(conn, tmp_path):
    """被舍弃的文档仍要连接作品和重复簇，否则两侧会共享文本。"""
    rng = random.Random(764)
    alphabet = "天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏"
    base = "".join(rng.choices(alphabet, k=1000))
    other = "".join(rng.choices(alphabet, k=1000))
    path = write(tmp_path / "bridge.jsonl", [
        row("kept", base + "！", chapter="甲书‧卷一", book="甲书"),
        row("bridge", base, chapter="乙书‧卷一", book="乙书"),
        row("work", other, chapter="乙书‧卷二", book="乙书"),
    ])
    ingest.ingest(conn, ADAPTERS["ccc_corpus"], [path])
    docs = dict(conn.execute("SELECT source_key, doc_id FROM document"))
    groups = export._group_keys(conn, list(docs.values()))
    assert groups[docs["kept"]] == groups[docs["work"]]
    release = export.create_release(conn, export.Recipe.load({
        "name": "bridge", "seed": "s", "splits": {"train": 0.5, "val": 0.5},
        "selection": {"dedup": "document"}}))
    splits = conn.execute(
        "SELECT v.doc_id, m.split FROM dataset_member m JOIN document_version v USING (version_id)"
        " WHERE m.release_id = %s", (release,)).fetchall()
    assert {doc for doc, _ in splits} == {docs["kept"], docs["work"]}
    assert len({split for _, split in splits}) == 1


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
    assert snapshot()[0][0] == second["run_id"] == first_run
    assert second["reused_snapshot"]
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


def test_incremental_dedup_matches_full_and_preserves_unchanged_clusters(conn, tmp_path):
    rng = random.Random(762)
    alphabet = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    a = "".join(rng.choices(alphabet, k=1200))
    b = "".join(rng.choices(alphabet, k=100)) + a[100:]
    c = b[:100] + "".join(rng.choices(alphabet, k=100)) + b[200:]
    unrelated = "".join(rng.choices(alphabet, k=1200))
    texts = [a, b, c, "甲乙", "甲乙", unrelated]

    def ingest_rows(name, rows):
        return ingest.ingest(conn, ADAPTERS["ccc_corpus"], [write(tmp_path / name, rows)])

    def snapshot():
        return conn.execute(
            "SELECT c.cluster_id, c.run_id, m.version_id, m.kind, m.xmin::text"
            " FROM dup_cluster_member m JOIN dup_cluster c USING (cluster_id)"
            " ORDER BY c.cluster_id, m.version_id"
        ).fetchall()

    def membership():
        clusters = {}
        for cid, _, version, kind, _ in snapshot():
            clusters.setdefault(cid, []).append((version, kind))
        return sorted(clusters.values())

    def assert_full_equivalent():
        before = snapshot()
        full = dedup.dedup(conn, full=True)
        assert snapshot() == before
        assert full["cached_pairs"] == 0
        assert full["clusters_changed"] == full["members_written"] == full["members_deleted"] == 0

    first = ingest_rows("initial.jsonl", [row(f"i#{i}", text) for i, text in enumerate(texts)])
    assert sorted(map(len, membership())) == [2, 3]
    docs = dict(conn.execute("SELECT source_key, doc_id FROM document"))
    conn.execute("DELETE FROM document_work")
    conn.commit()
    # 最近一次运行是段落查重，导出仍须采用文档簇。
    assert first.dedup_spans["run_id"] > first.dedup["run_id"]
    groups = export._group_keys(conn, list(docs.values()))
    assert groups[docs["i#0"]] == groups[docs["i#1"]] == groups[docs["i#2"]]
    old_rows = snapshot()
    second = ingest_rows("increment.jsonl", [row("new", unrelated)]).dedup
    assert second["cached_pairs"] > 0 and second["computed_pairs"] == 0
    assert second["clusters_changed"] == 1 and second["members_written"] == 2
    assert set(old_rows).issubset(snapshot())  # 簇 ID、run_id、成员物理版本均不变。
    assert_full_equivalent()

    # 删除连接 A、C 的桥接文本 B，必须拆开旧簇。
    replacement = "".join(rng.choices(alphabet, k=1200))
    reprocess.reprocess(conn, reprocess.Rule(
        "replace-bridge", None,
        lambda doc: reprocess.Change(text=replacement) if doc["doc_id"] == docs["i#1"] else None,
    ))
    report = dedup.dedup(conn)
    assert report["members_deleted"] == 3
    assert sorted(map(len, membership())) == [2, 2]
    assert_full_equivalent()
    assert conn.execute(
        "SELECT count(*) FROM dup_cluster_member m JOIN document_version v USING (version_id)"
        " WHERE NOT v.is_current"
    ).fetchone()[0] == 0

    # 元数据改动会换 version_id，分数仍按比较正文复用。
    reprocess.reprocess(conn, reprocess.Rule(
        "metadata-only", None,
        lambda doc: reprocess.Change(metadata={"title": "修订标题"})
        if doc["doc_id"] == docs["i#0"] else None,
    ))
    report = dedup.dedup(conn)
    assert not report["reused_snapshot"] and report["computed_pairs"] == 0
    assert_full_equivalent()

    # UNLOGGED 缓存可清空；强制重算无需依赖缓存，也不重写未变簇。
    before = snapshot()
    conn.execute("TRUNCATE dedup_pair, text_fingerprint")
    conn.commit()
    rebuilt = dedup.dedup(conn, full=True)
    assert rebuilt["fingerprinted"] > 0 and rebuilt["computed_pairs"] > 0
    assert snapshot() == before
    # 移除全部当前版本，空集合也必须替换旧结果。
    conn.execute("UPDATE document_version SET is_current = false WHERE is_current")
    conn.commit()
    assert dedup.dedup(conn)["clusters"] == 0
    assert snapshot() == []
    assert dedup.dedup_spans(conn)["spans"] == 0


def test_dedup_failure_keeps_previous_snapshot(conn, tmp_path, monkeypatch):
    path = write(tmp_path / "a.jsonl", [row("a", "甲" * 100), row("b", "甲" * 100)])
    ingest.ingest(conn, ADAPTERS["ccc_corpus"], [path])
    before = conn.execute("SELECT * FROM dedup_snapshot ORDER BY level").fetchall()
    members = conn.execute("SELECT * FROM dup_cluster_member ORDER BY cluster_id, version_id").fetchall()
    pairs = conn.execute("SELECT count(*) FROM dedup_pair").fetchone()[0]
    reprocess.reprocess(conn, reprocess.Rule(
        "fail-test", None,
        lambda doc: reprocess.Change(text="天地玄黄宇宙洪荒日月盈昃辰宿列张寒来暑往秋收冬藏" * 4 + str(doc["doc_id"])),
    ))
    original = dedup._write_clusters

    def fail(*args):
        original(*args)
        raise RuntimeError("simulated write failure")

    with monkeypatch.context() as patch:
        patch.setattr(dedup, "_write_clusters", fail)
        with pytest.raises(RuntimeError, match="simulated"):
            dedup.dedup(conn)
    assert conn.execute("SELECT * FROM dedup_snapshot ORDER BY level").fetchall() == before
    assert conn.execute("SELECT * FROM dup_cluster_member ORDER BY cluster_id, version_id").fetchall() == members
    assert conn.execute("SELECT count(*) FROM dedup_pair").fetchone()[0] == pairs
    recovered = dedup.dedup(conn)
    assert not recovered["reused_snapshot"] and recovered["computed_pairs"] == 1


def test_dedup_migration_and_parameter_changes(conn, tmp_path, monkeypatch):
    rng = random.Random(762)
    base = "".join(rng.choices("abcdefghijklmnopqrstuvwxyz", k=1000))
    path = write(tmp_path / "versions.jsonl", [row("a", base), row("b", base[:-1] + "甲")])
    ingest.ingest(conn, ADAPTERS["ccc_corpus"], [path])
    before = conn.execute("SELECT cluster_id, run_id FROM dup_cluster").fetchall()
    conn.commit()
    revision = runpy.run_path(str(SCHEMA.with_name("0006_incremental_dedup.py")))
    engine = create_engine("postgresql+psycopg://", creator=lambda: psycopg.connect(DSN))
    try:
        with engine.begin() as connection:
            with Operations.context(MigrationContext.configure(connection)):
                revision["downgrade"]()
                revision["upgrade"]()
    finally:
        engine.dispose()
    assert conn.execute("SELECT cluster_id, run_id FROM dup_cluster").fetchall() == before
    restored = dedup.dedup(conn)
    assert restored["computed_pairs"] == 1 and restored["clusters_changed"] == 0
    assert restored["members_written"] == 0
    with monkeypatch.context() as patch:
        patch.setattr(dedup, "THRESHOLD", 1.0)
        strict = dedup.dedup(conn)
        assert not strict["reused_snapshot"] and strict["clusters"] == 0
        assert strict["cached_pairs"] == 1 and strict["computed_pairs"] == 0
    assert dedup.dedup(conn)["clusters"] == 1
    with monkeypatch.context() as patch:
        patch.setattr(dedup, "RULE_VERSION", "compare-test-v2")
        changed_rule = dedup.dedup(conn)
        assert changed_rule["fingerprinted"] == 2 and changed_rule["computed_pairs"] == 1
        assert changed_rule["cached_pairs"] == 0


def test_web_api_real_versions(conn, tmp_path):
    from fastapi.testclient import TestClient
    from midstream.api import create_app, connection
    path = write(tmp_path / 'web.jsonl', [row('web#1', '甲篇正文\r\n　第二段，保留标点。', chapter='测试文献·甲篇'), row('web#2', '乙篇正文。', chapter='测试文献·乙篇')])
    ingest.ingest(conn, ADAPTERS['ccc_corpus'], [path])
    app = create_app()
    app.dependency_overrides[connection] = lambda: conn
    with TestClient(app) as client:
        assert client.get('/api/v1/sources').json() == [{'name':'ccc_corpus'}]
        first = client.get('/api/v1/documents?limit=1').json()
        doc = first['items'][0]['doc_id']
        assert first['next_cursor'] == doc
        assert client.get(f'/api/v1/documents?after={doc}&limit=1').json()['next_cursor'] is None
        versions = client.get(f'/api/v1/documents/{doc}/versions').json()
        assert [v['version_no'] for v in versions] == [2,1]
        assert versions[0]['is_current']
        assert '\r\n' in client.get(f'/api/v1/documents/{doc}?version=1').json()['text']
        assert '\r' not in client.get(f'/api/v1/documents/{doc}?version=2').json()['text']


def test_web_metadata_stats_review_and_batches(conn, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from midstream.api import create_app, connection, write_connection
    from midstream import textcodec
    path = write(tmp_path / 'meta-web.jsonl', [row('web-review', '甲篇正文。')])
    ingest.ingest(conn, ADAPTERS['ccc_corpus'], [path])
    conn.execute("INSERT INTO source (name) VALUES ('empty_fixture')")
    conn.commit()
    release = export.create_release(conn, export.Recipe.load({
        'name':'web-stats-fixture','seed':'s','splits':{'train':1.0}}))
    app = create_app()
    app.dependency_overrides[connection] = lambda: conn
    app.dependency_overrides[write_connection] = lambda: conn
    client = TestClient(app)
    headers = {'X-Midstream-Action':'1','Origin':'http://testserver'}
    doc = client.get('/api/v1/documents').json()['items'][0]['doc_id']
    def no_text(*args):
        raise AssertionError('Metadata requests must not decompress text')
    with monkeypatch.context() as patch:
        patch.setattr(textcodec, 'decompress', no_text)
        metadata = client.get(f'/api/v1/documents/{doc}/metadata').json()
    assert 'text' not in metadata and metadata['source'] == 'ccc_corpus'
    assert metadata['document']['source_key'] == 'web-review'
    assert metadata['source_records'][0]['path'] == str(path)
    assert metadata['processing']['batch_id'] is not None
    stats = client.get('/api/v1/stats').json()
    assert stats['current'] == {'documents':1,'characters':len('甲篇正文。'),'quarantined':0}
    assert next(s for s in stats['sources'] if s['source']=='empty_fixture')['versions'] == 0
    assert stats['releases'][0]['release_id'] == release
    payload = {'actor':'fixture reviewer','reason':'correct title','expected_version_id':metadata['version_id'],
               'metadata':{'title':'已核对标题','quality_status':'flagged','quality_flags':['manual_check']}}
    saved = client.post(f'/api/v1/documents/{doc}/review',json=payload,headers=headers)
    assert saved.status_code == 200 and saved.json()['version_no'] == 3
    assert client.post(f'/api/v1/documents/{doc}/review',json=payload,headers=headers).status_code == 409
    old = client.get(f'/api/v1/documents/{doc}/metadata?version=2').json()
    current = client.get(f'/api/v1/documents/{doc}/metadata').json()
    assert old['title'] == metadata['title'] and current['title'] == '已核对标题'
    assert current['field_basis']['title'] == 'human'
    assert export.release_stats(conn,release)['char_count'] == stats['releases'][0]['char_count']
    invalid = {**payload,'expected_version_id':current['version_id'],'metadata':{'composed_years':'bad range'}}
    assert client.post(f'/api/v1/documents/{doc}/review',json=invalid,headers=headers).status_code == 422
    assert client.get(f'/api/v1/documents/{doc}/metadata').json()['version_id'] == current['version_id']
    work = client.get(f'/api/v1/documents/{doc}/works').json()[0]
    decision = {k:work[k] for k in ('work_id','relation')}
    decision.update(decision='rejected',expected_status=work['status'],actor='fixture reviewer',reason='wrong work')
    assert client.post(f'/api/v1/documents/{doc}/works/review',json=decision,headers=headers).status_code == 200
    events = client.get(f'/api/v1/events?doc_id={doc}').json()
    assert len(events) == 2 and events[0]['action']=='work_review'
    new = write(tmp_path / 'resume-web.jsonl',[row('new-web','乙篇正文。')])
    batch = ingest.register_batch(conn,ADAPTERS['ccc_corpus'],[new])
    assert client.get(f'/api/v1/batches/{batch}').json()['status']=='received'
    operation={'actor':'fixture reviewer','reason':'continue checked source'}
    resumed=client.post(f'/api/v1/batches/{batch}/resume',json=operation,headers=headers)
    assert resumed.status_code==200 and resumed.json()['status']=='published'
    assert client.post(f'/api/v1/batches/{batch}/resume',json=operation,headers=headers).json()['status']=='already_published'
    assert client.post(f'/api/v1/batches/{batch}/abandon',json=operation,headers=headers).status_code==409
    unused=write(tmp_path/'abandon-web.jsonl',[row('abandon-web','丙篇正文。')])
    abandoned=ingest.register_batch(conn,ADAPTERS['ccc_corpus'],[unused])
    assert client.post(f'/api/v1/batches/{abandoned}/abandon',json=operation,headers=headers).json()['status']=='abandoned'
    assert unused.exists()
    listing=client.get('/api/v1/batches?include_published=true&limit=1').json()
    assert listing['items'][0]['batch_id']==abandoned and listing['next_cursor']==abandoned
    older=client.get(f'/api/v1/batches?include_published=true&before={abandoned}&limit=1').json()
    assert older['items'][0]['batch_id']==batch


def test_web_year_granularities_and_unknowns(conn, tmp_path):
    from fastapi.testclient import TestClient
    from midstream.api import create_app, connection
    from midstream import management
    path=write(tmp_path/'year-web.jsonl',[row(f'year-{i}', f'合成文献第{i}篇。') for i in range(5)])
    ingest.ingest(conn, ADAPTERS['ccc_corpus'], [path])
    current=conn.execute('SELECT doc_id,version_id FROM document_version WHERE is_current ORDER BY doc_id').fetchall()
    ranges=['[-50,51)','[601,701)',None,'[-100,-99)','[-101,-100)']
    for i,((doc,version),years) in enumerate(zip(current,ranges)):
        management.review_document(conn,doc,version,{'composed_years':years,'composed_label':f'合成时代{i}'},actor='fixture',reason='year fixture')
    app=create_app()
    app.dependency_overrides[connection]=lambda:conn
    c=TestClient(app)
    stats=c.get('/api/v1/stats/years?granularity=century').json()
    assert stats['unknown_years']==1 and stats['known_years']==4
    counts={v['label']:v['documents'] for v in stats['items']}
    assert counts['公元前2世纪']==1 and counts['公元前1世纪']==2
    assert counts['1世纪']==1 and counts['7世纪']==1
    assert sum(counts.values())==5
    selected=c.get('/api/v1/documents?year_from=601&year_to=700').json()['items']
    assert [v['doc_id'] for v in selected]==[current[1][0]]
    assert len(c.get('/api/v1/documents?year_from=601&year_to=700&include_unknown_years=true').json()['items'])==2
    unknown=c.get('/api/v1/documents?only_unknown_years=true').json()['items']
    assert [v['doc_id'] for v in unknown]==[current[2][0]]
    assert len(c.get('/api/v1/documents?era=合成时代0').json()['items'])==1
    labels=c.get('/api/v1/stats/years?granularity=dynasty').json()
    assert sum(v['documents'] for v in labels['items'])==5 and not labels['overlapping']
    assert len(c.get('/api/v1/years/options').json()['labels'])==5
    years=c.get('/api/v1/stats/years?granularity=year').json()
    assert years['windowed'] and years['year_from']==601 and years['year_to']==700
    exact=c.get('/api/v1/stats/years?granularity=year&year_from=-101&year_to=-100').json()
    assert [v['documents'] for v in exact['items']]==[1,1]
    for url in ('documents?year_from=0','documents?year_from=1&only_unknown_years=true',
                'stats/years?granularity=year&year_from=-1000&year_to=1000',
                'stats/years?granularity=dynasty&year_from=1'):
        assert c.get('/api/v1/'+url).status_code==422


def test_mapped_import_plan_resume(conn, tmp_path):
    from midstream import importing
    path = write(tmp_path / 'mapped.jsonl', [json.dumps({'id': '1', 'body': '天地玄黄', 'name': '测试'})])
    plan = {'format': 'jsonl', 'text_field': 'body', 'id_field': 'id', 'fields': {'title': 'name'}}
    preview = importing.preview('mapped_fixture', [str(path)], plan)
    registered = importing.register(conn, 'mapped_fixture', [str(path)], preview['objects'],
        preview['parser_version'], 'tester', 'fixture import', plan)
    batch_id = registered['batch_id']
    assert conn.execute('SELECT import_plan FROM source_batch WHERE batch_id=%s', (batch_id,)).fetchone()[0]['text_field'] == 'body'
    result = batches.resume(conn, batch_id, actor='tester', reason='fixture publish')
    assert result['status'] == 'published'
    hits = query.search(conn, query.Filters(sources=['mapped_fixture']))
    assert hits[0]['title'] == '测试'
    assert query.get_text(conn, hits[0]['doc_id'])['text'] == '天地玄黄'
