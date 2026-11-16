"""Structured authorities and broad web search share an evidence/cache interface."""
import hashlib
import os
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit
from xml.etree import ElementTree

import httpx
from psycopg.types.json import Jsonb
from ..llm.errors import LLMError
from .models import Evidence


USER_AGENT = 'MidstreamCorpus/0.1 (https://github.com/Midstream-LLM/Midstream-corpus)'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                     separators=(',', ':')).encode()).hexdigest()


class Retrieval:
    def __init__(self, conn, llm_config, search_profile=None, client=None):
        self.conn = conn
        self.config = llm_config
        self.search_profile = search_profile
        # Wikimedia 拒绝不带联系方式的 User-Agent（403）。只写公开仓库地址，不含个人信息。
        # 外部检索可单独走代理（MIDSTREAM_RETRIEVAL_PROXY），不影响模型调用。
        self.client = client or httpx.Client(timeout=40, follow_redirects=False,
            proxy=os.environ.get('MIDSTREAM_RETRIEVAL_PROXY') or None,
            headers={'User-Agent': USER_AGENT})

    def close(self):
        self.client.close()

    def _json(self, method, url, **kwargs):
        try:
            response = self.client.request(method, url, **kwargs)
            response.raise_for_status()
            if len(response.content) > 4_000_000:
                raise ValueError('检索响应过大')
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            # Provider errors may include credentials/request bodies; keep logs safe.
            raise LLMError('retrieval_failed', '外部检索失败，请检查服务配置或稍后重试') from exc

    def _deepseek(self, action, query):
        _, _, endpoint = self.config.resolve('search', self.search_profile)
        key = self.config.key(endpoint.credential)
        if not key:
            raise LLMError('missing_credential', '搜索服务尚未配置凭据')
        base = endpoint.base_url.rstrip('/')
        if urlsplit(base).hostname == 'api.deepseek.com':
            base = 'https://api.deepseek.com/v1'
        elif not base.endswith('/v1'):
            base += '/v1'
        if action == 'search':
            if not 1 <= len(query.strip()) <= 128:
                raise ValueError('搜索词长度须为 1–128 字符')
            payload = {'query': query, 'count': 5, 'need_content': True}
        else:
            parts = urlsplit(query)
            if parts.scheme not in ('http', 'https') or not parts.hostname or parts.username or parts.password:
                raise ValueError('无效网页 URL')
            payload = {'url': query}
        return self._json('POST', f'{base}/tools/{action}', json=payload,
                          headers={'Authorization': f'Bearer {key}'}, timeout=endpoint.timeout)

    def lookup(self, action, query):
        scope = ''
        if action in ('search', 'fetch'):
            _, _, endpoint = self.config.resolve('search', self.search_profile)
            scope = endpoint.base_url
        cache_key = digest(['retrieval-v1', scope, action, query])
        row = self.conn.execute('SELECT payload FROM retrieval_cache WHERE cache_key=%s AND expires_at>now()',
                                (cache_key,)).fetchone()
        if row:
            return [Evidence.model_validate(item) for item in row[0]]
        result = self._lookup(action, query)
        now = datetime.now(timezone.utc).isoformat()
        for item in result:
            item.retrieved_at = now
        self.conn.execute("INSERT INTO retrieval_cache(cache_key,payload,expires_at) VALUES(%s,%s,now()+interval '7 days')"
                          ' ON CONFLICT(cache_key) DO UPDATE SET payload=excluded.payload,expires_at=excluded.expires_at',
                          (cache_key, Jsonb([item.model_dump() for item in result])))
        self.conn.commit()
        return result

    def _lookup(self, action, query):
        if action == 'wikidata':
            is_id = bool(re.fullmatch(r'Q[1-9]\d*', query))
            params = {'format': 'json', 'action': 'wbgetentities' if is_id else 'wbsearchentities'}
            params.update({'ids': query, 'languages': 'zh|en', 'props': 'labels|aliases|claims|descriptions'}
                          if is_id else {'search': query, 'language': 'zh', 'limit': 5})
            raw = self._json('GET', 'https://www.wikidata.org/w/api.php', params=params)
            if 'error' in raw:
                raise LLMError('retrieval_failed', 'Wikidata 返回查询错误')
            entities = list(raw.get('entities', {}).values()) if is_id else raw.get('search', [])
            properties = {'P31','P50','P170','P123','P577','P571','P136','P921','P1476','P407',
                          'P144','P361','P1433','P569','P570','P106','P1559'}
            if is_id:
                entities = [{**item, 'claims':{key:statements[:10] for key,statements in item.get('claims',{}).items()
                                              if key in properties}} for item in entities]
            return [Evidence(key='', provider='wikidata', reference='https://www.wikidata.org/wiki/'+item['id'],
                             title=item.get('label') or item.get('labels', {}).get('zh', {}).get('value', ''),
                             data=item, query=query) for item in entities if 'missing' not in item]
        if action == 'gutenberg':
            if not re.fullmatch(r'[1-9]\d{0,7}', query):
                raise ValueError('Gutenberg 查询需要 ebook ID')
            # /ebooks/{id}.rdf 会重定向到这里，而检索客户端不跟随重定向。
            url = f'https://www.gutenberg.org/cache/epub/{query}/pg{query}.rdf'
            try:
                response = self.client.get(url)
                response.raise_for_status()
                if len(response.content) > 2_000_000 or b'<!DOCTYPE' in response.content:
                    raise ValueError('无效 RDF')
                root = ElementTree.fromstring(response.content)
            except (httpx.HTTPError, ElementTree.ParseError, ValueError) as exc:
                raise LLMError('retrieval_failed', 'Gutenberg 书目读取失败') from exc
            ns = {'d': 'http://purl.org/dc/terms/', 'p': 'http://www.gutenberg.org/2009/pgterms/'}
            title = root.findtext('.//d:title', default='', namespaces=ns)
            authors = [node.text for node in root.findall('.//d:creator/p:agent/p:name', ns) if node.text]
            return [Evidence(key='', provider='gutenberg', reference=url, title=title,
                             data={'ebook_id': query, 'title': title, 'authors': authors}, query=query)]
        if action in ('search', 'fetch'):
            raw = self._deepseek(action, query)
            pages = raw.get('results', []) if action == 'search' else [{**raw, 'url': query}]
            return [Evidence(key='', provider='deepseek_'+action, reference=p['url'],
                             title=p.get('title') or '', excerpt=(p.get('content') or p.get('summary') or p.get('snippet') or '')[:20000],
                             data={'snippet': (p.get('snippet') or '')[:2000], 'authority': p.get('authority')},
                             query=query) for p in pages[:5] if p.get('url')]
        raise ValueError('未知检索工具')
