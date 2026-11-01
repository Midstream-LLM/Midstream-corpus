"""本地配置管理；原子写入，乐观冲突检测，API 不返回密钥。"""
import fcntl
import hashlib
import json
import math
import os
import tempfile
import tomllib
from dataclasses import asdict

from .config import LLMConfig, Endpoint, Profile, PROTOCOLS, config_path
from .errors import LLMError


class SettingsError(Exception):
    def __init__(self, message, status=422):
        super().__init__(message)
        self.status = status


def _read():
    path = config_path()
    try:
        raw = path.read_bytes() if path.exists() else b''
        return raw, tomllib.loads(raw.decode()) if raw else {}
    except (OSError, ValueError):
        raise SettingsError('无法读取 LLM 配置，请检查文件格式和访问权限') from None


def _revision(raw):
    return hashlib.sha256(raw).hexdigest()


def _config(data):
    try:
        if set(data) - {'credentials', 'endpoints', 'profiles', 'services', 'local_keys'}:
            raise ValueError()
        return LLMConfig(data.get('credentials', {}),
                         {k: Endpoint(**v) for k,v in data.get('endpoints', {}).items()},
                         {k: Profile(**v) for k,v in data.get('profiles', {}).items()},
                         data.get('services', {}), data.get('local_keys', {}))
    except (LLMError, ValueError, TypeError, AttributeError, KeyError):
        raise SettingsError('配置无效，请检查接入点、凭据引用、模型和用途默认值') from None


def get():
    raw, data = _read()
    if raw:
        config = _config(data)
        editable = {'credentials': config.credentials,
                    'endpoints': {k: asdict(v) for k,v in config.endpoints.items()},
                    'profiles': {k: asdict(v) for k,v in config.profiles.items()},
                    'services': config.services}
        status = {k: {'configured': bool(config.key(k)),
                      'source': 'environment' if os.environ.get(env) else
                                'local' if config.local_keys.get(k) else 'missing',
                      'local_saved': bool(config.local_keys.get(k))}
                  for k,env in config.credentials.items()}
    else:
        editable = {k:{} for k in ('credentials', 'endpoints', 'profiles', 'services')}
        status = {}
    return {'config': editable, 'credentials_status': status, 'revision': _revision(raw),
            'configured': bool(raw), 'protocols': list(PROTOCOLS)}


def _toml(value):
    if isinstance(value, str): return json.dumps(value, ensure_ascii=False)
    if isinstance(value, bool): return 'true' if value else 'false'
    if isinstance(value, (int, float)) and math.isfinite(value): return str(value)
    if isinstance(value, list): return '[' + ', '.join(_toml(v) for v in value) + ']'
    if isinstance(value, dict):
        return '{' + ', '.join(json.dumps(k) + ' = ' + _toml(v) for k,v in value.items()) + '}'
    raise SettingsError('参数仅支持字符串、有限数字、布尔值、数组和对象')


def save(data, keys, clear_keys, revision):
    path = config_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(str(path) + '.lock', os.O_CREAT | os.O_RDWR, 0o600)
        with os.fdopen(fd, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            raw, previous = _read()
            if _revision(raw) != revision:
                raise SettingsError('配置已被修改，请重新加载后编辑', 409)
            if not isinstance(data, dict) or set(data) != {'credentials', 'endpoints', 'profiles', 'services'}:
                raise SettingsError('配置字段不完整')
            _config(data)
            local = {k:v for k,v in previous.get('local_keys', {}).items() if k in data['credentials']}
            if set(keys) - set(data['credentials']) or set(clear_keys) - set(data['credentials']):
                raise SettingsError('密钥引用的凭据不存在')
            if set(keys) & set(clear_keys):
                raise SettingsError('不能同时更新和清除同一凭据的密钥')
            for k in clear_keys: local.pop(k, None)
            for k,v in keys.items():
                if not v.strip(): raise SettingsError('新密钥不能为空；留空表示保留')
                local[k] = v
            full = {**data, 'local_keys': local}
            _config(full)
            content = '# Managed by Midstream; contains local credentials. Do not commit.\n' + '\n'.join(
                '[' + section + ']\n' + '\n'.join(json.dumps(k) + ' = ' + _toml(v) for k,v in values.items())
                for section,values in full.items()) + '\n'
            # 写前再用实际 TOML 读取器验证，替换失败时保留原文件。
            _config(tomllib.loads(content))
            temp_fd, temp = tempfile.mkstemp(prefix='.llm-', dir=path.parent)
            try:
                with os.fdopen(temp_fd, 'w') as handle:
                    handle.write(content)
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(temp, path)
            finally:
                if os.path.exists(temp): os.unlink(temp)
    except OSError:
        raise SettingsError('保存失败，请检查后端配置目录权限', 500) from None
    return get()
