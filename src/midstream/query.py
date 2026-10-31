"""按当前版本查询。"""

from dataclasses import dataclass, field

import psycopg

from . import textcodec


@dataclass(frozen=True)
class Filters:
    sources: list[str] = field(default_factory=list)
    title_prefix: str | None = None
    author: str | None = None
    genre: str | None = None
    category: str | None = None
    year_from: int | None = None
    year_to: int | None = None
    include_unknown_years: bool = False
    quality: list[str] = field(default_factory=lambda: ["ok", "flagged"])
    min_chars: int | None = None

    @classmethod
    def from_dict(cls, data: dict) -> "Filters":
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"未知的筛选条件：{sorted(unknown)}")
        return cls(**data)


def where_clause(filters: Filters) -> tuple[str, dict]:
    """生成筛选条件。表别名固定为 v（版本）、d（文档）、s（来源）。"""
    clauses = ["v.is_current", "v.quality_status = ANY(%(quality)s)"]
    params: dict = {"quality": filters.quality}
    if filters.sources:
        clauses.append("s.name = ANY(%(sources)s)")
        params["sources"] = filters.sources
    if filters.title_prefix:
        clauses.append("v.title LIKE %(title)s")
        escaped = filters.title_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        params["title"] = escaped + "%"
    if filters.author:
        clauses.append("v.authors @> ARRAY[%(author)s]::text[]")
        params["author"] = filters.author
    if filters.genre:
        clauses.append("v.genre = %(genre)s")
        params["genre"] = filters.genre
    if filters.category:
        clauses.append("v.category = %(category)s")
        params["category"] = filters.category
    if filters.min_chars is not None:
        clauses.append("v.char_count >= %(min_chars)s")
        params["min_chars"] = filters.min_chars
    if filters.year_from is not None or filters.year_to is not None:
        overlap = "v.composed_years && int4range(%(year_from)s, %(year_to)s, '[]')"
        if filters.include_unknown_years:
            overlap = f"({overlap} OR v.composed_years IS NULL)"
        clauses.append(overlap)
        params["year_from"] = filters.year_from
        params["year_to"] = filters.year_to
    return " AND ".join(clauses), params


def search(
    conn: psycopg.Connection, filters: Filters, limit: int = 50, after_doc: int = 0
) -> list[dict]:
    """按 doc_id 稳定分页，只读列表需要的字段。"""
    where, params = where_clause(filters)
    params.update(limit=limit, after=after_doc)
    rows = conn.execute(
        f"""SELECT d.doc_id, v.version_id, s.name, v.version_no, v.title, v.authors, v.composed_label,
                   lower(v.composed_years), upper(v.composed_years) - 1,
                   v.category, v.genre, v.quality_status, v.quality_flags, v.char_count
            FROM document_version v
            JOIN document d USING (doc_id)
            JOIN source s USING (source_id)
            WHERE {where} AND d.doc_id > %(after)s
            ORDER BY d.doc_id LIMIT %(limit)s""",
        params,
    ).fetchall()
    keys = ("doc_id", "version_id", "source", "version_no", "title", "authors",
            "composed_label", "year_from", "year_to", "category", "genre",
            "quality_status", "quality_flags", "char_count")
    return [dict(zip(keys, row)) for row in rows]


def get_text(conn: psycopg.Connection, doc_id: int, version_no: int | None = None) -> dict | None:
    """读取某篇文档的一个版本，默认当前版本。"""
    condition = "v.version_no = %(no)s" if version_no else "v.is_current"
    row = conn.execute(
        f"""SELECT to_jsonb(v), b.content
            FROM document_version v JOIN text_blob b USING (text_hash)
            WHERE v.doc_id = %(doc)s AND {condition}""",
        {"doc": doc_id, "no": version_no},
    ).fetchone()
    if row is None:
        return None
    return {**row[0], "text": textcodec.decompress(bytes(row[1]))}


def stats(conn: psycopg.Connection) -> dict:
    per_source = conn.execute(
        """SELECT s.name, count(*) FILTER (WHERE v.is_current),
                  count(*) FILTER (WHERE v.is_current AND v.quality_status = 'ok'),
                  count(*) FILTER (WHERE v.is_current AND v.quality_status = 'flagged'),
                  count(*) FILTER (WHERE v.is_current AND v.composed_years IS NOT NULL),
                  count(*)
           FROM source s
           LEFT JOIN document d USING (source_id)
           LEFT JOIN document_version v USING (doc_id)
           GROUP BY s.name ORDER BY s.name"""
    ).fetchall()
    blob = conn.execute(
        """SELECT count(*), coalesce(sum(byte_count), 0), coalesce(sum(octet_length(content)), 0),
                  pg_total_relation_size('text_blob'), pg_database_size(current_database())
           FROM text_blob"""
    ).fetchone()
    return {
        "sources": [
            dict(zip(("source", "documents", "ok", "flagged", "with_years", "versions"), row))
            for row in per_source
        ],
        "blobs": blob[0],
        "plain_bytes": int(blob[1]),
        "compressed_bytes": int(blob[2]),
        "blob_table_bytes": blob[3],
        "database_bytes": blob[4],
    }
