"""人工审核：当前版本追加新快照，作品关联决议单独留痕。"""

from collections.abc import Mapping

import psycopg
from psycopg.types.json import Jsonb

from .reprocess import _write_versions


class ManagementError(ValueError):
    """可由 CLI/API 识别的业务错误。"""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_FIELDS = frozenset({
    "quality_status", "quality_flags", "license", "title", "authors",
    "composed_years", "composed_label", "edition_years", "genre", "category",
    "topics", "legacy_category", "extra",
})
_ARRAYS = frozenset({"quality_flags", "authors", "topics"})
_RANGES = frozenset({"composed_years", "edition_years"})


def _identity(actor: str, reason: str) -> None:
    if not isinstance(actor, str) or not actor.strip():
        raise ManagementError("invalid_input", "actor 不能为空")
    if not isinstance(reason, str) or not reason.strip():
        raise ManagementError("invalid_input", "reason 不能为空")


def _patch(patch: Mapping) -> dict:
    if not isinstance(patch, Mapping) or not patch:
        raise ManagementError("invalid_input", "metadata 必须是非空对象")
    unknown = set(patch) - _FIELDS
    if unknown:
        raise ManagementError("invalid_input", f"不支持的字段：{sorted(unknown)}")
    result = dict(patch)
    for key, value in result.items():
        if key in _ARRAYS and value is not None and (
            not isinstance(value, list) or any(not isinstance(item, str) for item in value)
        ):
            raise ManagementError("invalid_input", f"{key} 应为字符串数组或 null")
        if key in _RANGES and value is not None and not isinstance(value, str):
            raise ManagementError("invalid_input", f"{key} 应为 PostgreSQL int4range 字符串或 null")
        if key == "extra" and (not isinstance(value, dict)):
            raise ManagementError("invalid_input", "extra 应为对象")
        if key == "quality_status" and value not in ("ok", "flagged", "quarantined"):
            raise ManagementError("invalid_input", "quality_status 无效")
        if key not in _ARRAYS | _RANGES | {"extra", "quality_status"} and (
            value is not None and not isinstance(value, str)
        ):
            raise ManagementError("invalid_input", f"{key} 应为字符串或 null")
    if result.get("quality_flags") is None and "quality_flags" in result:
        raise ManagementError("invalid_input", "quality_flags 不能为 null")
    return result


def review_document(
    conn: psycopg.Connection, doc_id: int, expected_version_id: int,
    metadata: Mapping, *, actor: str, reason: str,
) -> dict:
    """以乐观版本校验人工修正；旧版本除 is_current 外保持不变。"""
    _identity(actor, reason)
    patch = _patch(metadata)
    with conn.transaction():
        row = conn.execute(
            "SELECT to_jsonb(v) FROM document_version v"
            " WHERE v.doc_id = %s AND v.is_current FOR UPDATE OF v", (doc_id,),
        ).fetchone()
        if row is None:
            raise ManagementError("not_found", f"文档 {doc_id} 没有当前版本")
        current = row[0]
        if current["version_id"] != expected_version_id:
            raise ManagementError("conflict", "当前版本已变化，请重新查看后再审核")
        changed = {key: value for key, value in patch.items() if current[key] != value}
        if not changed:
            raise ManagementError("no_change", "提交内容与当前版本相同")
        old = {key: current[key] for key in changed}
        basis = dict(current["field_basis"])
        basis.update({key: "human" for key in changed})
        changed["field_basis"] = basis
        run_id = conn.execute(
            "INSERT INTO processing_run (kind, rule_version, config, finished_at)"
            " VALUES ('manual', 'manual-v1', %s, now()) RETURNING run_id",
            (Jsonb({"actor": actor, "reason": reason, "fields": sorted(old)}),),
        ).fetchone()[0]
        _write_versions(conn, run_id, [(doc_id, expected_version_id, None, changed)])
        version_id, version_no = conn.execute(
            "SELECT version_id, version_no FROM document_version"
            " WHERE doc_id = %s AND is_current", (doc_id,),
        ).fetchone()
        event_id = conn.execute(
            "INSERT INTO management_event"
            " (action, actor, reason, doc_id, from_version_id, to_version_id,"
            " before_state, after_state)"
            " VALUES ('document_review', %s, %s, %s, %s, %s, %s, %s) RETURNING event_id",
            (actor, reason, doc_id, expected_version_id, version_id,
             Jsonb(old), Jsonb({key: patch[key] for key in old})),
        ).fetchone()[0]
    conn.commit()
    return {"status": "applied", "event_id": event_id, "doc_id": doc_id,
            "version_id": version_id, "version_no": version_no,
            "changed_fields": sorted(old)}


def review_work(
    conn: psycopg.Connection, doc_id: int, work_id: int, relation: str,
    decision: str, *, actor: str, reason: str, expected_status: str = "candidate",
) -> dict:
    """审核已存在的关联；原始来源 basis 保留，人工决议写审计事件。"""
    _identity(actor, reason)
    if decision not in ("confirmed", "rejected"):
        raise ManagementError("invalid_input", "decision 必须是 confirmed 或 rejected")
    if expected_status not in ("candidate", "confirmed", "rejected"):
        raise ManagementError("invalid_input", "expected_status 无效")
    with conn.transaction():
        row = conn.execute(
            "SELECT basis, status FROM document_work"
            " WHERE doc_id = %s AND work_id = %s AND relation = %s FOR UPDATE",
            (doc_id, work_id, relation),
        ).fetchone()
        if row is None:
            raise ManagementError("not_found", "作品关联不存在")
        basis, status = row
        if status != expected_status:
            raise ManagementError("conflict", "作品关联状态已变化，请重新查看")
        if status == decision:
            raise ManagementError("no_change", "作品关联状态未变化")
        conn.execute(
            "UPDATE document_work SET status = %s"
            " WHERE doc_id = %s AND work_id = %s AND relation = %s",
            (decision, doc_id, work_id, relation),
        )
        event_id = conn.execute(
            "INSERT INTO management_event"
            " (action, actor, reason, doc_id, work_id, before_state, after_state)"
            " VALUES ('work_review', %s, %s, %s, %s, %s, %s) RETURNING event_id",
            (actor, reason, doc_id, work_id,
             Jsonb({"relation": relation, "basis": basis, "status": status}),
             Jsonb({"relation": relation, "basis": basis, "status": decision})),
        ).fetchone()[0]
    conn.commit()
    return {"status": "applied", "event_id": event_id, "doc_id": doc_id,
            "work_id": work_id, "relation": relation, "decision": decision}


def work_links(conn: psycopg.Connection, doc_id: int, *,
               status: str | None = None) -> list[dict]:
    """列出一篇文档已有的作品关联，供人工确认或拒绝。"""
    if status is not None and status not in ("candidate", "confirmed", "rejected"):
        raise ManagementError("invalid_input", "status 无效")
    condition = " AND dw.status = %s" if status is not None else ""
    rows = conn.execute(
        "SELECT dw.work_id, w.canonical_title, dw.relation, dw.basis, dw.status"
        " FROM document_work dw JOIN work w USING (work_id)"
        " WHERE dw.doc_id = %s" + condition + " ORDER BY dw.work_id, dw.relation",
        (doc_id, status) if status is not None else (doc_id,),
    ).fetchall()
    keys = ("work_id", "canonical_title", "relation", "basis", "status")
    return [dict(zip(keys, row)) for row in rows]


def events(conn: psycopg.Connection, *, doc_id: int | None = None,
           batch_id: int | None = None, limit: int = 50) -> list[dict]:
    if (doc_id is None) == (batch_id is None):
        raise ManagementError("invalid_input", "必须且只能指定 doc_id 或 batch_id")
    if not 1 <= limit <= 1000:
        raise ManagementError("invalid_input", "limit 必须在 1 到 1000 之间")
    column, value = ("doc_id", doc_id) if doc_id is not None else ("batch_id", batch_id)
    rows = conn.execute(
        f"SELECT event_id, action, actor, reason, doc_id, work_id, batch_id,"
        f" from_version_id, to_version_id, before_state, after_state, created_at"
        f" FROM management_event WHERE {column} = %s ORDER BY event_id DESC LIMIT %s",
        (value, limit),
    ).fetchall()
    keys = ("event_id", "action", "actor", "reason", "doc_id", "work_id",
            "batch_id", "from_version_id", "to_version_id", "before_state",
            "after_state", "created_at")
    return [dict(zip(keys, row)) for row in rows]
