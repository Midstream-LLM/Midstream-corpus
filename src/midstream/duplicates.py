"""重复浏览、原文坐标与持久否决。"""
import difflib
from bisect import bisect_left
from collections import defaultdict
from functools import lru_cache

import midstream_dedup
from psycopg.types.json import Jsonb

from . import query,textcodec
from .management import ManagementError,_identity


def clusters(conn, *, before=None, kind=None, source=None, min_size=2, max_size=None, limit=30):
    rows=conn.execute(
        "WITH groups AS (SELECT c.cluster_id,c.run_id,count(*) AS size,sum(v.char_count) AS chars,"
        " CASE WHEN bool_or(m.kind='near_candidate') THEN 'near' ELSE 'exact' END AS kind,"
        ' array_agg(DISTINCT s.name ORDER BY s.name) AS sources FROM dup_cluster c'
        ' JOIN dup_cluster_member m USING(cluster_id) JOIN document_version v USING(version_id)'
        " JOIN document d USING(doc_id) JOIN source s USING(source_id) WHERE c.level='document'"
        ' GROUP BY c.cluster_id) SELECT * FROM groups WHERE (%s::bigint IS NULL OR cluster_id<%s)'
        ' AND (%s::text IS NULL OR kind=%s) AND (%s::text IS NULL OR %s=ANY(sources))'
        ' AND size>=%s AND (%s::bigint IS NULL OR size<=%s) ORDER BY cluster_id DESC LIMIT %s',
        (before,before,kind,kind,source,source,min_size,max_size,max_size,limit+1)).fetchall()
    keys=('cluster_id','run_id','size','chars','kind','sources')
    return {'items':[dict(zip(keys,row)) for row in rows[:limit]],
            'next_cursor':rows[limit-1][0] if len(rows)>limit else None}


def cluster(conn, cluster_id, *, after=0, limit=30):
    if not conn.execute('SELECT 1 FROM dup_cluster WHERE cluster_id=%s',(cluster_id,)).fetchone():
        raise ManagementError('not_found','重复簇不存在或重算后已变化')
    rows=conn.execute('SELECT v.doc_id,v.version_id,v.title,v.authors,v.category,v.genre,v.char_count,s.name,m.kind'
        ' FROM dup_cluster_member m JOIN document_version v USING(version_id) JOIN document d USING(doc_id)'
        ' JOIN source s USING(source_id) WHERE cluster_id=%s AND v.doc_id>%s ORDER BY v.doc_id LIMIT %s',
        (cluster_id,after,limit+1)).fetchall()
    keys=('doc_id','version_id','title','authors','category','genre','char_count','source','kind')
    return {'cluster_id':cluster_id,'members':[dict(zip(keys,row)) for row in rows[:limit]],
            'next_cursor':rows[limit-1][0] if len(rows)>limit else None}


def compare(conn,left,right,length=10000):
    a,b=query.get_text(conn,left),query.get_text(conn,right)
    if a is None or b is None:
        raise ManagementError('not_found','比较文档不存在')
    first,second=a['text'][:length],b['text'][:length]
    return {'left':first,'right':second,'truncated':len(a['text'])>length or len(b['text'])>length,
            'diff':''.join(difflib.unified_diff(first.splitlines(keepends=True),second.splitlines(keepends=True),
                                              fromfile=str(left),tofile=str(right)))}


def overrides(conn,doc_id=None):
    return [row[0] for row in conn.execute('SELECT to_jsonb(o) FROM dedup_override o'
        ' WHERE %s::bigint IS NULL OR left_doc_id=%s OR right_doc_id=%s ORDER BY override_id DESC LIMIT 100',
        (doc_id,doc_id,doc_id))]


def override(conn,left,right,cluster_id,actor,reason):
    _identity(actor,reason)
    if right is None:
        if cluster_id is None:
            raise ManagementError('invalid_input','请指定另一文档或所在簇')
        members=[row[0] for row in conn.execute('SELECT v.doc_id FROM dup_cluster_member m'
            ' JOIN document_version v USING(version_id) WHERE cluster_id=%s',(cluster_id,))]
        if left not in members:
            raise ManagementError('conflict','文档不在指定簇中')
        others=[doc for doc in members if doc!=left]
    else:
        if right==left:
            raise ManagementError('invalid_input','不能否决文档自身')
        others=[right]
    pairs=[tuple(sorted((left,other))) for other in others]
    for a,b in pairs:
        conn.execute('INSERT INTO dedup_override(left_doc_id,right_doc_id,actor,reason) VALUES(%s,%s,%s,%s)'
                     ' ON CONFLICT DO NOTHING',(a,b,actor,reason))
    conn.execute("INSERT INTO management_event(action,actor,reason,doc_id,after_state)"
                 " VALUES('dedup_override',%s,%s,%s,%s)",(actor,reason,left,Jsonb({'pairs':pairs})))
    conn.commit()
    return {'pairs':len(pairs),'next_action':'dedup'}


def apply_overrides(conn):
    """只重分含负边的原始组件；合并始终要有已验证正边，且组件间无否决。"""
    from .dedup import RULE_VERSION,THRESHOLD
    pairs=conn.execute('SELECT left_doc_id,right_doc_id FROM dedup_override ORDER BY override_id').fetchall()
    if not pairs:
        return {}
    denied=defaultdict(set)
    for a,b in pairs:
        denied[a].add(b);denied[b].add(a)
    locations=dict(conn.execute('SELECT v.doc_id,r.cluster_key FROM dedup_result r'
        ' JOIN document_version v USING(version_id) WHERE v.doc_id=ANY(%s)',(list(denied),)))
    affected=sorted({locations[a] for a,b in pairs if a in locations and b in locations and locations[a]==locations[b]})
    for key in affected:
        rows=conn.execute('SELECT v.doc_id,r.version_id,f.compare_hash FROM dedup_result r'
            ' JOIN document_version v USING(version_id) JOIN text_fingerprint f USING(text_hash)'
            ' WHERE r.cluster_key=%s AND f.rule_version=%s ORDER BY v.doc_id',(key,RULE_VERSION)).fetchall()
        parent={doc:doc for doc,_,_ in rows}
        members={doc:{doc} for doc,_,_ in rows}
        blocked={doc:set(denied[doc]) for doc,_,_ in rows}
        def find(doc):
            while parent[doc]!=doc:
                parent[doc]=parent[parent[doc]];doc=parent[doc]
            return doc
        def union(a,b):
            a,b=find(a),find(b)
            if a==b:
                return True
            if not blocked[a].isdisjoint(members[b]):
                return False
            if len(members[a])<len(members[b]):
                a,b=b,a
            parent[b]=a;members[a].update(members.pop(b));blocked[a].update(blocked.pop(b))
            return True
        hashes=defaultdict(list)
        for doc,_,digest in rows:
            hashes[digest].append(doc)
        for docs in hashes.values():
            leaders=[]
            for doc in docs:
                if not any(union(leader,doc) for leader in leaders):
                    leaders.append(doc)
        for left_hash,right_hash in conn.execute('SELECT left_hash,right_hash FROM dedup_pair'
            ' WHERE rule_version=%s AND similarity>=%s AND left_hash=ANY(%s) AND right_hash=ANY(%s)'
            ' ORDER BY left_hash,right_hash',(RULE_VERSION,THRESHOLD,list(hashes),list(hashes))):
            for a in sorted({find(doc) for doc in hashes[left_hash]}):
                for b in sorted({find(doc) for doc in hashes[right_hash]}):
                    union(a,b)
        groups=defaultdict(list)
        for doc,vid,digest in rows:
            groups[find(doc)].append((vid,digest))
        conn.execute('DELETE FROM dedup_result WHERE cluster_key=%s',(key,))
        with conn.cursor() as cursor,cursor.copy('COPY dedup_result(cluster_key,version_id,kind) FROM STDIN') as copy:
            for items in groups.values():
                if len(items)<2:
                    continue
                anchor=min(vid for vid,_ in items)
                kind='exact' if len({digest for _,digest in items})==1 else 'near_candidate'
                for vid,_ in items:
                    copy.write_row((-anchor,vid,kind))
    count,members_count=conn.execute('SELECT count(DISTINCT cluster_key),count(*) FROM dedup_result').fetchone()
    return {'clusters':count,'members':members_count}


def document_spans(conn,doc_id,version=None):
    selected=query.get_text(conn,doc_id,version)
    if selected is None:
        raise ManagementError('not_found','文档不存在')
    version_id=selected['version_id']
    if selected['variant'] is not None:
        if not conn.execute('SELECT 1 FROM document_version WHERE version_id=%s AND is_current',
                            (selected['parent_version_id'],)).fetchone():
            return {'items':[],'message':'派生版已过期，不能叠加当前查重坐标'}
        version_id=selected['parent_version_id']
    @lru_cache(maxsize=4)
    def positions(vid):
        content=conn.execute('SELECT b.content FROM document_version v JOIN text_blob b USING(text_hash)'
                             ' WHERE v.version_id=%s',(vid,)).fetchone()[0]
        return midstream_dedup.span_normalize(textcodec.decompress(bytes(content)))[1]
    items=[]
    for target,start,end,source,source_start in conn.execute(
        'SELECT version_id,start_char,end_char,source_version_id,source_start_char FROM dup_span'
        ' WHERE version_id=%s OR source_version_id=%s ORDER BY version_id,start_char LIMIT 200',(version_id,version_id)):
        if target==version_id:
            items.append({'start':start,'end':end,'other_version_id':source})
        else:
            p=positions(target)
            count=bisect_left(p,end)-bisect_left(p,start)
            p=positions(source);offset=bisect_left(p,source_start)
            items.append({'start':source_start,'end':p[offset+count-1]+1,'other_version_id':target})
    return {'items':sorted(items,key=lambda x:(x['start'],x['end'])),'message':None}
