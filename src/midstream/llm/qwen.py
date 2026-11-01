"""阿里云百炼 Qwen：区域预设和官方模型发现。"""
import re

import httpx

from .config import LLMConfig
from .settings import SettingsError, config_path

REGIONS = {
    'beijing': {'name': '中国 · 北京', 'code': 'cn-beijing'},
    'singapore': {'name': '新加坡', 'code': 'ap-southeast-1'},
    'virginia': {'name': '美国 · 弗吉尼亚', 'code': 'us-east-1'},
}


def urls(region, workspace):
    if region not in REGIONS:
        raise SettingsError('请选择支持的 Qwen 服务区域')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,62}', workspace):
        raise SettingsError('请填写有效的百炼业务空间 ID（字母、数字或连字符）')
    origin = f"https://{workspace}.{REGIONS[region]['code']}.maas.aliyuncs.com"
    # 官方模型发现文档当前对新加坡给出此区域入口。
    discovery = 'https://dashscope-intl.aliyuncs.com' if region == 'singapore' else origin
    return {'base_url': origin + '/compatible-mode/v1', 'models_url': discovery + '/api/v1/models'}


def preset():
    return {'regions': REGIONS, 'checked_on': '2026-11-01',
            'models': [{'id': 'qwen3.8-max', 'name': 'Qwen3.8-Max'}],
            'source': 'https://www.alibabacloud.com/help/en/model-studio/compatibility-of-openai-with-dashscope'}


def models(region, workspace, credential, api_key=None, *, transport=None):
    target = urls(region, workspace)
    key = api_key
    if api_key is not None and not api_key.strip():
        raise SettingsError('API key 不能为空白')
    if not key:
        if not config_path().exists():
            raise SettingsError('请先输入 Qwen API key，或保存凭据配置')
        config = LLMConfig.load()
        if credential not in config.credentials:
            raise SettingsError('凭据不存在，请先输入 API key 或保存凭据配置')
        key = config.key(credential)
    if not key:
        raise SettingsError('Qwen 凭据尚未配置')
    try:
        items, seen, total = [], set(), 0
        with httpx.Client(timeout=20, follow_redirects=False, transport=transport) as client:
            for page in range(1, 6):
                response = client.get(target['models_url'],
                    headers={'Authorization': f'Bearer {key}'},
                    params={'providers': 'qwen', 'capabilities': 'TG', 'page_no': page, 'page_size': 100})
                if response.status_code != 200:
                    raise SettingsError(f'Qwen 模型列表请求失败（HTTP {response.status_code}），请核对区域与 key 是否一致', 502)
                data = response.json()
                if data.get('success') is False: raise ValueError()
                output = data['output']
                rows, total = output['models'], output['total']
                if not isinstance(rows, list) or type(total) is not int or total < 0: raise ValueError()
                for row in rows:
                    if not isinstance(row, dict) or not isinstance(row.get('model'), str): raise ValueError()
                    if row['model'] in seen: continue
                    seen.add(row['model'])
                    item = {'id': row['model'], 'name': row.get('name') if isinstance(row.get('name'),str) else row['model']}
                    info = row.get('model_info') or {}
                    for field in ('context_window', 'max_output_tokens'):
                        if type(info.get(field)) is int and info[field] > 0: item[field] = info[field]
                    items.append(item)
                if not rows or len(items) >= total: break
        return {'models': items, 'source': 'api', 'total': total,
                'truncated': len(items) < total, **target}
    except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError):
        raise SettingsError('无法获取 Qwen 模型列表，请检查网络、区域与业务空间；仍可手工填写模型 ID', 502) from None
