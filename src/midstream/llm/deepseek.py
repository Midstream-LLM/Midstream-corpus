"""DeepSeek 配置预设与官方模型发现；不发送生成请求。"""
import httpx

from .config import LLMConfig
from .settings import SettingsError, config_path

BASE_URLS = {'chat_completions': 'https://api.deepseek.com',
             'responses': 'https://api.deepseek.com',
             'anthropic_messages': 'https://api.deepseek.com/anthropic',
             'fim_completions': 'https://api.deepseek.com/beta'}


def preset():
    # 离线候选来自官方文档；实际账户可用模型以 GET /models 为准。
    return {'checked_on': '2026-11-01', 'source': 'https://api-docs.deepseek.com/api/list-models/',
            'base_urls': BASE_URLS, 'models': [
                {'id': 'deepseek-flash', 'name': 'DeepSeek Flash'},
                {'id': 'deepseek-v4-pro', 'name': 'DeepSeek V4 Pro'}]}


def models(credential: str, api_key: str | None = None, *, transport=None):
    key = api_key
    if api_key is not None and not api_key.strip():
        raise SettingsError('API key 不能为空白')
    if not key:
        if not config_path().exists():
            raise SettingsError('请先输入 DeepSeek API key，或保存凭据配置')
        config = LLMConfig.load()
        if credential not in config.credentials:
            raise SettingsError('凭据不存在，请先输入 API key 或保存凭据配置')
        key = config.key(credential)
    if not key:
        raise SettingsError('DeepSeek 凭据尚未配置')
    try:
        with httpx.Client(timeout=20, follow_redirects=False, transport=transport) as client:
            response = client.get('https://api.deepseek.com/models', headers={'Authorization': f'Bearer {key}'})
        if response.status_code != 200:
            raise SettingsError(f'DeepSeek 模型列表请求失败（HTTP {response.status_code}）', 502)
        data = response.json()['data']
        if not isinstance(data, list): raise ValueError()
        result = []
        for row in data:
            if not isinstance(row, dict) or not isinstance(row.get('id'), str): raise ValueError()
            item = {'id': row['id'], 'name': row.get('name') if isinstance(row.get('name'), str) else row['id']}
            for field in ('context_window', 'max_output_tokens'):
                if type(row.get(field)) is int and row[field] > 0: item[field] = row[field]
            effort = row.get('effort', {})
            if isinstance(effort, dict) and isinstance(effort.get('supported_levels'), list):
                item['effort_levels'] = [v for v in effort['supported_levels'] if v in ('low','high','max')]
            result.append(item)
        return {'models': result, 'source': 'api', 'url': 'https://api.deepseek.com/models'}
    except (httpx.HTTPError, ValueError, KeyError, TypeError):
        raise SettingsError('无法获取 DeepSeek 模型列表，请检查网络和凭据；可继续使用离线候选或手工填写', 502) from None
