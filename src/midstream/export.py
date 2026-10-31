"""数据集导出：先固定成员，再生成文件。

成员、顺序和划分写入数据库后不再变化，文件可以删除后重建。
验证集按组划分：同一作品或同一重复簇的文档落在同一侧。
"""

import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

import psycopg
import pyarrow as pa
import pyarrow.parquet as pq

from . import textcodec
from .query import Filters, where_clause

SPLIT_NAMES = ("test", "train", "val")
PARQUET_BATCH = 500


class ExportError(Exception):
    pass


class TrainingLeak(ExportError):
    pass


@dataclass(frozen=True)
class Recipe:
    name: str
    filters: Filters
    splits: dict[str, float]
    seed: str
    raw: dict

    @classmethod
    def load(cls, data: dict) -> "Recipe":
        splits = data["splits"]
        if set(splits) - set(SPLIT_NAMES):
            raise ExportError(f"未知的划分：{sorted(set(splits) - set(SPLIT_NAMES))}")
        if abs(sum(splits.values()) - 1) > 1e-9:
            raise ExportError("划分比例之和必须为 1")
        filters = {"quality": ["ok"], **data.get("filters", {})}
        return cls(data["name"], Filters.from_dict(filters), splits, data["seed"], data)


class _Groups:
    """并查集，节点是字符串，如 d:1、w:2、c:3。"""

    def __init__(self) -> None:
        self.parent: dict[str, str] = {}

    def find(self, node: str) -> str:
        parent = self.parent.setdefault(node, node)
        while parent != self.parent[parent]:
            self.parent[parent] = self.parent[self.parent[parent]]
            parent = self.parent[parent]
        self.parent[node] = parent
        return parent

    def union(self, a: str, b: str) -> None:
        self.parent[self.find(a)] = self.find(b)


def _select(conn: psycopg.Connection, filters: Filters) -> list[tuple[int, int, bytes, int]]:
    where, params = where_clause(filters)
    rows = conn.execute(
        f"""SELECT v.version_id, v.doc_id, v.text_hash, v.char_count
            FROM document_version v
            JOIN document d USING (doc_id)
            JOIN source s USING (source_id)
            WHERE {where} ORDER BY v.version_id""",
        params,
    ).fetchall()
    return [(row[0], row[1], bytes(row[2]), row[3]) for row in rows]


def _group_keys(conn: psycopg.Connection, doc_ids: list[int]) -> dict[int, int]:
    """返回 doc_id 到组标识（组内最小 doc_id）的映射。"""
    groups = _Groups()
    for doc_id in doc_ids:
        groups.find(f"d:{doc_id}")
    for doc_id, work_id in conn.execute(
        "SELECT doc_id, work_id FROM document_work"
        " WHERE status <> 'rejected' AND doc_id = ANY(%s)",
        (doc_ids,),
    ):
        groups.union(f"d:{doc_id}", f"w:{work_id}")
    for doc_id, cluster_id in conn.execute(
        """SELECT v.doc_id, m.cluster_id
           FROM dup_cluster_member m
           JOIN dup_cluster c USING (cluster_id)
           JOIN document_version v USING (version_id)
           WHERE c.level = 'document' AND v.doc_id = ANY(%s)""",
        (doc_ids,),
    ):
        groups.union(f"d:{doc_id}", f"c:{cluster_id}")
    smallest: dict[str, int] = {}
    for doc_id in sorted(doc_ids):
        smallest.setdefault(groups.find(f"d:{doc_id}"), doc_id)
    return {doc_id: smallest[groups.find(f"d:{doc_id}")] for doc_id in doc_ids}


def _fraction(seed: str, value: int) -> float:
    digest = hashlib.sha256(f"{seed}:{value}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def _assign_split(recipe: Recipe, group: int) -> str:
    point = _fraction(recipe.seed, group)
    upper = 0.0
    name = "train"
    for name in SPLIT_NAMES:
        upper += recipe.splits.get(name, 0.0)
        if point < upper:
            return name
    return name


def _check_training_history(conn: psycopg.Connection, held_out: list[int]) -> None:
    """进过训练的文档不能进验证或测试，经文档和作品两条路径反查。"""
    if not held_out:
        return
    leaked = conn.execute(
        """WITH trained AS (
               SELECT DISTINCT v.doc_id
               FROM training_use u
               JOIN dataset_member m ON m.release_id = u.release_id AND m.split = 'train'
               JOIN document_version v USING (version_id)
           )
           SELECT h.doc_id FROM unnest(%s::bigint[]) AS h(doc_id)
           WHERE h.doc_id IN (SELECT doc_id FROM trained)
              OR EXISTS (
                   SELECT 1 FROM document_work a
                   JOIN document_work b USING (work_id)
                   JOIN trained t ON t.doc_id = b.doc_id
                   WHERE a.doc_id = h.doc_id
                     AND a.status <> 'rejected' AND b.status <> 'rejected')
           LIMIT 5""",
        (held_out,),
    ).fetchall()
    if leaked:
        raise TrainingLeak(f"验证或测试集包含已训练过的文档，例如 {[row[0] for row in leaked]}")


def _digest(members: list[tuple[int, int, str, bytes]]) -> bytes:
    digest = hashlib.sha256()
    for ordinal, version_id, split, text_hash in members:
        digest.update(f"{ordinal}:{version_id}:{split}:{text_hash.hex()}\n".encode())
    return digest.digest()


def create_release(conn: psycopg.Connection, recipe: Recipe) -> int:
    selected = _select(conn, recipe.filters)
    if not selected:
        raise ExportError("配方没有选中任何文档")
    groups = _group_keys(conn, [doc_id for _, doc_id, _, _ in selected])
    assigned = [
        (version_id, _assign_split(recipe, groups[doc_id]), doc_id, text_hash, char_count)
        for version_id, doc_id, text_hash, char_count in selected
    ]
    _check_training_history(
        conn, [doc_id for _, split, doc_id, _, _ in assigned if split != "train"]
    )
    ordered = sorted(assigned, key=lambda item: _fraction(recipe.seed + ":order", item[0]))
    members = [
        (ordinal, version_id, split, text_hash)
        for ordinal, (version_id, split, _, text_hash, _) in enumerate(ordered)
    ]
    char_counts = {split: 0 for split in SPLIT_NAMES}
    for _, split, _, _, char_count in assigned:
        char_counts[split] += char_count
    release_id = conn.execute(
        "INSERT INTO dataset_release (name, recipe, content_digest, char_count,"
        " train_char_count, val_char_count, test_char_count)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s)"
        " RETURNING release_id",
        (recipe.name, json.dumps(recipe.raw, ensure_ascii=False), _digest(members),
         sum(char_counts.values()), char_counts["train"], char_counts["val"], char_counts["test"]),
    ).fetchone()[0]
    with conn.cursor() as cursor, cursor.copy(
        "COPY dataset_member (release_id, version_id, ordinal, split) FROM STDIN"
    ) as copy:
        for ordinal, version_id, split, _ in members:
            copy.write_row((release_id, version_id, ordinal, split))
    conn.commit()
    return release_id


def release_stats(conn: psycopg.Connection, release_id: int) -> dict:
    """读取发布时固定的字符数；空划分为 0。"""
    row = conn.execute(
        "SELECT name, char_count, train_char_count, val_char_count, test_char_count"
        " FROM dataset_release WHERE release_id = %s", (release_id,),
    ).fetchone()
    if row is None:
        raise ExportError(f"数据集 {release_id} 不存在")
    return {
        "release_id": release_id,
        "name": row[0],
        "char_count": row[1],
        "split_char_counts": {"train": row[2], "val": row[3], "test": row[4]},
    }


def _member_rows(conn: psycopg.Connection, release_id: int, split: str):
    with conn.cursor(name=f"export_{split}") as cursor:
        cursor.execute(
            """SELECT m.ordinal, m.version_id, v.doc_id, v.title, b.content
               FROM dataset_member m
               JOIN document_version v USING (version_id)
               JOIN text_blob b USING (text_hash)
               WHERE m.release_id = %s AND m.split = %s
               ORDER BY m.ordinal""",
            (release_id, split),
        )
        while rows := cursor.fetchmany(PARQUET_BATCH):
            yield rows


SCHEMA = pa.schema([
    ("ordinal", pa.int64()),
    ("version_id", pa.int64()),
    ("doc_id", pa.int64()),
    ("title", pa.string()),
    ("text", pa.string()),
])


def write_parquet(conn: psycopg.Connection, release_id: int, out_dir: Path) -> list[Path]:
    """生成各划分的 Parquet 文件，写完再改名。"""
    out_dir.mkdir(parents=True, exist_ok=True)
    splits = [row[0] for row in conn.execute(
        "SELECT DISTINCT split FROM dataset_member WHERE release_id = %s ORDER BY split",
        (release_id,),
    )]
    written = []
    for split in splits:
        target = out_dir / f"{split}.parquet"
        temporary = target.with_suffix(".parquet.tmp")
        with pq.ParquetWriter(temporary, SCHEMA, compression="zstd") as writer:
            for rows in _member_rows(conn, release_id, split):
                writer.write_table(pa.table(
                    {
                        "ordinal": [row[0] for row in rows],
                        "version_id": [row[1] for row in rows],
                        "doc_id": [row[2] for row in rows],
                        "title": [row[3] for row in rows],
                        "text": [textcodec.decompress(bytes(row[4])) for row in rows],
                    },
                    schema=SCHEMA,
                ))
        os.replace(temporary, target)
        digest = hashlib.sha256()
        with open(target, "rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
        conn.execute(
            "INSERT INTO export_artifact (release_id, path, format, sha256, tool_info)"
            " VALUES (%s, %s, 'parquet', %s, %s)",
            (release_id, str(target), digest.digest(),
             json.dumps({"pyarrow": pa.__version__, "compression": "zstd"})),
        )
        written.append(target)
    conn.commit()
    return written


def verify_release(conn: psycopg.Connection, release_id: int) -> bool:
    """重算成员摘要和字符数，并逐篇核对解压后的正文。"""
    expected = conn.execute(
        "SELECT content_digest, char_count, train_char_count, val_char_count, test_char_count"
        " FROM dataset_release WHERE release_id = %s", (release_id,)
    ).fetchone()
    if expected is None:
        raise ExportError(f"数据集 {release_id} 不存在")
    members = []
    char_counts = {split: 0 for split in SPLIT_NAMES}
    for ordinal, version_id, split, text_hash, char_count in conn.execute(
        """SELECT m.ordinal, m.version_id, m.split, v.text_hash, v.char_count
           FROM dataset_member m JOIN document_version v USING (version_id)
           WHERE m.release_id = %s ORDER BY m.ordinal""",
        (release_id,),
    ):
        members.append((ordinal, version_id, split, bytes(text_hash)))
        char_counts[split] += char_count
    if _digest(members) != bytes(expected[0]):
        return False
    if (sum(char_counts.values()), char_counts["train"], char_counts["val"],
            char_counts["test"]) != expected[1:]:
        return False
    with conn.cursor(name="verify_blobs") as cursor:
        cursor.execute(
            """SELECT DISTINCT b.text_hash, b.content, v.char_count
               FROM dataset_member m
               JOIN document_version v USING (version_id)
               JOIN text_blob b USING (text_hash)
               WHERE m.release_id = %s""",
            (release_id,),
        )
        while rows := cursor.fetchmany(PARQUET_BATCH):
            for text_hash, content, char_count in rows:
                text = textcodec.decompress(bytes(content))
                if len(text) != char_count or textcodec.text_hash(text) != bytes(text_hash):
                    return False
    return True


def record_training(conn: psycopg.Connection, release_name: str, run_name: str) -> None:
    release = conn.execute(
        "SELECT release_id FROM dataset_release WHERE name = %s", (release_name,)
    ).fetchone()
    if release is None:
        raise ExportError(f"数据集不存在：{release_name}")
    conn.execute(
        "INSERT INTO training_use (release_id, run_name, used_at) VALUES (%s, %s, now())",
        (release[0], run_name),
    )
    conn.commit()
