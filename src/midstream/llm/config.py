"""接入点、环境变量凭据引用、模型配置和服务默认值。无需数据库。"""

import math
import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from .errors import LLMError

def config_path() -> Path:
    return Path(os.environ.get("MIDSTREAM_LLM_CONFIG", str(Path(os.environ.get(
        "MIDSTREAM_HOME", "~/.local/share/midstream")).expanduser() / "llm.toml"))).expanduser()


PROTOCOLS = ("chat_completions", "responses", "anthropic_messages", "fim_completions")


def _keys(data, allowed, label):
    if not isinstance(data, dict) or set(data) - set(allowed):
        raise LLMError("invalid_config", f"{label} 包含未知字段或不是对象")


def _number(value, label, minimum, *, integer=False):
    if (isinstance(value, bool) or not isinstance(value, int if integer else (int, float))
            or not math.isfinite(value) or value < minimum):
        raise LLMError("invalid_config", f"{label} 必须是 >= {minimum} 的有限数值")


@dataclass(frozen=True)
class Endpoint:
    protocol: str
    base_url: str
    credential: str
    timeout: float = 120
    concurrency: int = 4
    max_retries: int = 2
    requests_per_second: float = 0

    def __post_init__(self):
        parts = urlsplit(self.base_url)
        if (self.protocol not in PROTOCOLS or parts.scheme not in ("https", "http")
                or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment):
            raise LLMError("invalid_config", "接入点协议或 base_url 无效")
        _number(self.timeout, "timeout", 0.001)
        _number(self.concurrency, "concurrency", 1, integer=True)
        _number(self.max_retries, "max_retries", 0, integer=True)
        _number(self.requests_per_second, "requests_per_second", 0)

    @property
    def url(self) -> str:
        suffix = {"chat_completions": "chat/completions", "responses": "responses",
                  "fim_completions": "completions"}.get(self.protocol)
        if self.protocol == "anthropic_messages":
            suffix = "messages" if self.base_url.rstrip("/").endswith("/v1") else "v1/messages"
        return f"{self.base_url.rstrip('/')}/{suffix}"


@dataclass(frozen=True)
class Profile:
    endpoint: str
    model: str
    max_tokens: int = 4096
    parameters: dict = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.model, str) or not self.model.strip():
            raise LLMError("invalid_config", "model 不能为空")
        _number(self.max_tokens, "max_tokens", 1, integer=True)
        if not isinstance(self.parameters, dict):
            raise LLMError("invalid_config", "parameters 必须是对象")
        if {"model", "stream", "messages", "input", "instructions", "prompt", "system"} & self.parameters.keys():
            raise LLMError("invalid_config", "parameters 不能覆盖模型、输入或 stream")


@dataclass(frozen=True)
class LLMConfig:
    credentials: dict[str, str]
    endpoints: dict[str, Endpoint]
    profiles: dict[str, Profile]
    services: dict[str, str] = field(default_factory=dict)

    local_keys: dict[str, str] = field(default_factory=dict, repr=False)

    def key(self, credential: str) -> str | None:
        return os.environ.get(self.credentials[credential]) or self.local_keys.get(credential)

    def __post_init__(self):
        if set(self.local_keys) - set(self.credentials) or any(not isinstance(k, str) or not k.strip() for k in self.local_keys.values()):
            raise LLMError("invalid_config", "本地凭据必须引用已登记的凭据名称且非空")
        if not self.endpoints or not self.profiles:
            raise LLMError("invalid_config", "至少配置一个接入点和模型 profile")
        for name, env in self.credentials.items():
            if not isinstance(env, str) or not env.isidentifier():
                raise LLMError("invalid_config", f"凭据 {name} 应引用环境变量名")
        for name, endpoint in self.endpoints.items():
            if endpoint.credential not in self.credentials:
                raise LLMError("invalid_config", f"接入点 {name} 引用了不存在的凭据")
        for name, profile in self.profiles.items():
            if profile.endpoint not in self.endpoints:
                raise LLMError("invalid_config", f"profile {name} 引用了不存在的接入点")
        if set(self.services) - {"generation", "quality_check", "import_detection"}:
            raise LLMError("invalid_config", "未知服务名称")
        if any(profile not in self.profiles for profile in self.services.values()):
            raise LLMError("invalid_config", "服务引用了不存在的 profile")

    @classmethod
    def load(cls, path: Path | None = None) -> "LLMConfig":
        path = path or config_path()
        try:
            with Path(path).expanduser().open("rb") as handle:
                data = tomllib.load(handle)
            _keys(data, ("credentials", "endpoints", "profiles", "services", "local_keys"), "配置")
            endpoints, profiles = {}, {}
            for name, values in data.get("endpoints", {}).items():
                _keys(values, Endpoint.__dataclass_fields__, f"接入点 {name}")
                endpoints[name] = Endpoint(**values)
            for name, values in data.get("profiles", {}).items():
                _keys(values, Profile.__dataclass_fields__, f"profile {name}")
                profiles[name] = Profile(**values)
            return cls(data.get("credentials", {}), endpoints, profiles, data.get("services", {}), data.get("local_keys", {}))
        except (OSError, ValueError, TypeError, AttributeError) as exc:
            # TOML 原文可能包含用户误填的密钥，不回显解析器上下文。
            raise LLMError("invalid_config", "无法读取 LLM 配置或配置格式无效") from exc

    def resolve(self, service: str, profile: str | None = None) -> tuple[str, Profile, Endpoint]:
        name = profile or self.services.get(service, "default")
        if name not in self.profiles:
            raise LLMError("invalid_config", f"未配置 profile：{name}")
        selected = self.profiles[name]
        return name, selected, self.endpoints[selected.endpoint]

    def describe(self) -> dict:
        return {
            "endpoints": {name: {"protocol": value.protocol, "base_url": value.base_url,
                                 "credential": value.credential,
                                 "key_configured": bool(self.key(value.credential))}
                          for name, value in self.endpoints.items()},
            "profiles": {name: {"endpoint": value.endpoint, "model": value.model}
                         for name, value in self.profiles.items()},
            "services": self.services,
        }
