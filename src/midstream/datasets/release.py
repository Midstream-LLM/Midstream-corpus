"""固定发布：写入成员、查询、统计、校验和训练历史。

成员、顺序和划分写入数据库后不再变化，文件可以删除后重建。
"""

import json

import psycopg

from .plan import ReleasePlan, _digest, plan_release
from .recipe import SPLIT_NAMES, ExportError, Recipe


def write_release(conn: psycopg.Connection, plan: ReleasePlan) -> int:
    recipe, report = plan.recipe, plan.report
    char_counts = {name: value['chars'] for name, value in report['splits'].items()}
    selection_report = {key: report[key] for key in ('candidates', 'dropped_documents', 'dropped_chars')}
    release_id = conn.execute(
        "INSERT INTO dataset_release (name, recipe, content_digest, char_count,"
        " train_char_count, val_char_count, test_char_count, selection_report)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
        " RETURNING release_id",
        (recipe.name, json.dumps(recipe.raw, ensure_ascii=False), _digest(plan.members,plan.cuts),
         sum(char_counts.values()), char_counts["train"], char_counts["val"], char_counts["test"],
         json.dumps(selection_report)),
    ).fetchone()[0]
    with conn.cursor() as cursor, cursor.copy(
        "COPY dataset_member (release_id, version_id, ordinal, split) FROM STDIN"
    ) as copy:
        for ordinal, version_id, split, _ in plan.members:
            copy.write_row((release_id, version_id, ordinal, split))
    with conn.cursor() as cursor, cursor.copy(
        'COPY dataset_member_cut (release_id,version_id,start_char,end_char) FROM STDIN') as copy:
        for version,intervals in sorted(plan.cuts.items()):
            for start,end in intervals:
                copy.write_row((release_id,version,start,end))
    conn.commit()
    return release_id


def create_release(conn: psycopg.Connection, recipe: Recipe) -> int:
    return write_release(conn, plan_release(conn, recipe))


def list_releases(conn, *, limit=30, before=None):
    return [row[0] for row in conn.execute(
        'SELECT to_jsonb(r) - \'content_digest\' || jsonb_build_object(\'content_digest\','
        " encode(content_digest, 'hex'), 'token_stats', coalesce((SELECT jsonb_agg("
        " jsonb_build_object('tokenizer_name',t.tokenizer_name,'tokenizer_fingerprint',"
        " encode(t.tokenizer_fingerprint,'hex'),'total',t.total) ORDER BY t.created_at)"
        " FROM release_token_stats t WHERE t.release_id=r.release_id AND t.unit='document'), '[]'::jsonb))"
        " FROM dataset_release r"
        ' WHERE (%s::bigint IS NULL OR release_id < %s) ORDER BY release_id DESC LIMIT %s',
        (before, before, limit))]


def get_release(conn, release_id):
    rows = list_releases(conn, limit=1, before=release_id + 1)
    if not rows or rows[0]['release_id'] != release_id:
        from ..corpus.management import ManagementError
        raise ManagementError('not_found', f'数据集 {release_id} 不存在')
    result = rows[0]
    result.pop('token_stats')
    result['splits'] = {split: {'documents': count, 'chars': int(chars)}
                        for split, count, chars in conn.execute(
                            'SELECT split, count(*), sum(v.char_count-coalesce(c.chars,0)) FROM dataset_member m'
                            ' JOIN document_version v USING (version_id)'
                            ' LEFT JOIN (SELECT version_id,sum(end_char-start_char) chars FROM dataset_member_cut'
                            ' WHERE release_id=%s GROUP BY version_id) c USING(version_id)'
                            ' WHERE m.release_id = %s GROUP BY split', (release_id,release_id))}
    return result


def release_stats(conn: psycopg.Connection, release_id: int) -> dict:
    """读取发布时固定的字符数和选用结果；空划分为 0，旧发布没有选用结果。"""
    row = conn.execute(
        "SELECT name, char_count, train_char_count, val_char_count, test_char_count,"
        " selection_report FROM dataset_release WHERE release_id = %s", (release_id,),
    ).fetchone()
    if row is None:
        raise ExportError(f"数据集 {release_id} 不存在")
    return {
        "release_id": release_id,
        "name": row[0],
        "char_count": row[1],
        "split_char_counts": {"train": row[2], "val": row[3], "test": row[4]},
        "selection": row[5],
    }


def verify_members(conn: psycopg.Connection, release_id: int) -> bool:
    """核对固定成员的内容摘要和字符数；产物文件另由交付层校验。"""
    expected = conn.execute(
        "SELECT content_digest, char_count, train_char_count, val_char_count, test_char_count"
        " FROM dataset_release WHERE release_id = %s", (release_id,)
    ).fetchone()
    if expected is None:
        raise ExportError(f"数据集 {release_id} 不存在")
    from . import cuts as span_cuts
    cuts = span_cuts.load(conn,release_id)
    members = []
    char_counts = {split: 0 for split in SPLIT_NAMES}
    for ordinal, version_id, split, text_hash, char_count in conn.execute(
        """SELECT m.ordinal, m.version_id, m.split, v.text_hash, v.char_count
           FROM dataset_member m JOIN document_version v USING (version_id)
           WHERE m.release_id = %s ORDER BY m.ordinal""",
        (release_id,),
    ):
        members.append((ordinal, version_id, split, bytes(text_hash)))
        char_counts[split] += char_count-sum(end-start for start,end in cuts.get(version_id,()))
    if _digest(members,cuts) != bytes(expected[0]):
        return False
    if (sum(char_counts.values()), char_counts["train"], char_counts["val"],
            char_counts["test"]) != expected[1:]:
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
