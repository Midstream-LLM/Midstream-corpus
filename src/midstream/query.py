"""按当前版本查询。"""

from dataclasses import dataclass, field

import psycopg
from pydantic import TypeAdapter

from . import textcodec


@dataclass(frozen=True)
class Filters:
    sources: list[str] = field(default_factory=list)
    authors: list[str] = field(default_factory=list)
    genres: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    title_prefix: str | None = None
    author: str | None = None
    genre: str | None = None
    category: str | None = None
    year_from: int | None = None
    year_to: int | None = None
    include_unknown_years: bool = False
    quality: list[str] = field(default_factory=lambda: ["ok", "flagged"])
    min_chars: int | None = None
    composed_label: str | None = None
    only_unknown_years: bool = False

    @classmethod
    def from_dict(cls, data: dict) -> "Filters":
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ValueError(f"未知的筛选条件：{sorted(unknown)}")
        return TypeAdapter(cls).validate_python(data)


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
    authors = filters.authors + ([filters.author] if filters.author else [])
    if authors:
        clauses.append("v.authors && %(authors)s::text[]")
        params["authors"] = authors
    for field, values, legacy in (("genre", filters.genres, filters.genre),
                                  ("category", filters.categories, filters.category)):
        values = values + ([legacy] if legacy else [])
        if values:
            clauses.append(f"v.{field} = ANY(%({field})s)")
            params[field] = values
    if filters.composed_label:
        clauses.append("v.composed_label = %(composed_label)s")
        params["composed_label"] = filters.composed_label
    if filters.only_unknown_years:
        clauses.append("v.composed_years IS NULL")
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
                  count(v.version_id)
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


def metadata(conn: psycopg.Connection, doc_id: int, version_no: int | None = None) -> dict | None:
    """完整元数据与出处，不读取或解压正文。来源记录标明各自批次。"""
    condition = "v.version_no = %(version)s" if version_no is not None else "v.is_current"
    row = conn.execute(
        "SELECT to_jsonb(v), to_jsonb(d), s.name, r.kind, r.rule_version, r.batch_id"
        " FROM document_version v JOIN document d USING (doc_id)"
        " JOIN source s USING (source_id) JOIN processing_run r USING (run_id)"
        f" WHERE v.doc_id = %(doc)s AND {condition}",
        {"doc": doc_id, "version": version_no},
    ).fetchone()
    if row is None:
        return None
    records = conn.execute(
        "SELECT sr.record_id, sr.batch_id, sr.locator, sr.raw_fields, ro.path,"
        " encode(ro.sha256, 'hex'), ro.size_bytes FROM source_record sr"
        " LEFT JOIN raw_object ro USING (raw_id) WHERE sr.doc_id = %s"
        " ORDER BY sr.record_id DESC LIMIT 20", (doc_id,),
    ).fetchall()
    keys = ('record_id', 'batch_id', 'locator', 'raw_fields', 'path', 'sha256', 'size_bytes')
    return {**row[0], "document": row[1], "source": row[2],
            "processing": {"kind": row[3], "rule_version": row[4], "batch_id": row[5]},
            "source_records": [dict(zip(keys, record)) for record in records]}


def overview(conn: psycopg.Connection) -> dict:
    result = stats(conn)
    row = conn.execute("SELECT count(*), coalesce(sum(char_count),0),"
                       " count(*) FILTER (WHERE quality_status='quarantined')"
                       " FROM document_version WHERE is_current").fetchone()
    result['current'] = {"documents": row[0], "characters": int(row[1]), "quarantined": row[2]}
    result['categories'] = [{"name": name, "documents": n, "characters": int(chars)}
        for name, n, chars in conn.execute(
            "SELECT category, count(*), sum(char_count) FROM document_version WHERE is_current"
            " GROUP BY category ORDER BY count(*) DESC, category NULLS LAST")]
    result['batch_statuses'] = dict(conn.execute("SELECT status,count(*) FROM source_batch GROUP BY status"))
    result['releases'] = [{"release_id": rid, "name": name, "char_count": total,
                           "train": train, "val": val, "test": test, "created_at": created}
        for rid, name, total, train, val, test, created in conn.execute(
            "SELECT release_id,name,char_count,train_char_count,val_char_count,test_char_count,created_at"
            " FROM dataset_release ORDER BY release_id DESC LIMIT 50")]
    return result


def year_options(conn: psycopg.Connection) -> dict:
    labels = [row[0] for row in conn.execute(
        "SELECT DISTINCT composed_label FROM document_version WHERE is_current"
        " AND composed_label IS NOT NULL ORDER BY composed_label")]
    return {'labels': labels}


def year_distribution(conn: psycopg.Connection, granularity: str = 'century',
                      year_from: int | None = None, year_to: int | None = None) -> dict:
    """时代标签按原记录分组；年份分桶按区间相交计数，跨桶文档可能多次出现。"""
    total, unknown, open_ranges, low, high = conn.execute(
        "SELECT count(*), count(*) FILTER (WHERE composed_years IS NULL),"
        " count(*) FILTER (WHERE composed_years IS NOT NULL AND"
        " (lower_inf(composed_years) OR upper_inf(composed_years))),"
        " min(coalesce(lower(composed_years),upper(composed_years)-1)),"
        " max(coalesce(upper(composed_years)-1,lower(composed_years)))"
        " FROM document_version WHERE is_current").fetchone()
    result = {'granularity': granularity, 'total': total, 'unknown_years': unknown,
              'open_ranges': open_ranges, 'known_years': total-unknown}
    if granularity == 'dynasty':
        result['items'] = [{'label': label or '时代标签未记录', 'documents': n, 'characters': int(chars)}
            for label, n, chars in conn.execute(
                "SELECT composed_label,count(*),coalesce(sum(char_count),0) FROM document_version"
                " WHERE is_current GROUP BY composed_label"
                " ORDER BY min(lower(composed_years)) NULLS LAST, composed_label NULLS LAST")]
        result['overlapping'] = False
        return result
    width = {'century': 100, 'year': 1}[granularity]
    if low is None or high is None:
        result.update(items=[], overlapping=True, year_from=None, year_to=None, windowed=False)
        return result
    end = high if year_to is None else year_to
    start = low if year_from is None else year_from
    index = lambda y: (y-1)//width if y > 0 else y//width
    # 默认最多展示最近 100 桶；手动指定宽区间则要求用户调整粒度。
    windowed = False
    if year_from is None and year_to is None and index(end)-index(start) >= 100:
        first = index(end)-99
        start = first*width+(1 if first >= 0 else 0)
        windowed = True
    if start > end or index(end)-index(start) >= 500:
        raise ValueError('年份区间无效或超过500栏，请缩小区间或选择世纪粒度')
    starts, ends, labels = [], [], []
    for bucket in range(index(start), index(end)+1):
        left = bucket*width+(1 if bucket >= 0 else 0)
        right = (bucket+1)*width-(1 if bucket < 0 else 0)
        left, right = max(start,left), min(end,right)
        starts.append(left)
        ends.append(right)
        labels.append((f'公元前{-bucket}世纪' if bucket < 0 else f'{bucket+1}世纪') if width==100
                      else (f'公元前{-left}年' if left<0 else f'{left}年'))
    rows = conn.execute(
        "SELECT b.lo,b.hi,count(v.version_id),coalesce(sum(v.char_count),0)"
        " FROM unnest(%s::int[],%s::int[]) AS b(lo,hi) LEFT JOIN document_version v"
        " ON v.is_current AND v.composed_years && int4range(b.lo,b.hi,'[]')"
        " GROUP BY b.lo,b.hi ORDER BY b.lo", (starts, ends)).fetchall()
    result.update(items=[{'label': label, 'year_from': lo, 'year_to': hi,
                          'documents': n, 'characters': int(chars)}
                         for label,(lo,hi,n,chars) in zip(labels,rows)],
                  overlapping=True, year_from=start, year_to=end, windowed=windowed)
    return result
