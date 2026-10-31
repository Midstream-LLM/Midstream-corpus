"""统一连接、凭据、限速和重试。对语料库与用途服务无依赖。"""

import json
import os
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

import httpx

from .config import Endpoint, LLMConfig
from .errors import LLMError
from .protocols import GenerationResult, parse_response


@dataclass(frozen=True)
class StreamEvent:
    event: str
    data: dict


class LLMClient:
    def __init__(self, config: LLMConfig, *, transport: httpx.BaseTransport | None = None,
                 sleep=time.sleep):
        self.config = config
        self._http = httpx.Client(transport=transport, follow_redirects=False)
        self._sleep = sleep
        self._locks = {name: threading.Lock() for name in config.endpoints}
        self._slots = {name: threading.BoundedSemaphore(ep.concurrency)
                       for name, ep in config.endpoints.items()}
        self._next_request = {name: 0.0 for name in config.endpoints}

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    def close(self):
        self._http.close()

    def _wait(self, name: str, endpoint: Endpoint):
        if endpoint.requests_per_second:
            with self._locks[name]:
                now = time.monotonic()
                start = max(now, self._next_request[name])
                self._next_request[name] = start + 1 / endpoint.requests_per_second
            self._sleep(max(0, start - now))

    def _headers(self, endpoint: Endpoint) -> dict:
        env = self.config.credentials[endpoint.credential]
        key = os.environ.get(env)
        if not key:
            raise LLMError("missing_credential", f"请设置凭据环境变量 {env}")
        if endpoint.protocol == "anthropic_messages":
            return {"x-api-key": key, "anthropic-version": "2023-06-01"}
        return {"Authorization": f"Bearer {key}"}

    @contextmanager
    def _response(self, payload: dict, profile: str, stream: bool):
        _, selected, endpoint = self.config.resolve("generation", profile)
        if not isinstance(payload, dict):
            raise LLMError("invalid_input", "请求必须是 JSON 对象")
        limit = "max_output_tokens" if endpoint.protocol == "responses" else "max_tokens"
        body = {"model": selected.model, limit: selected.max_tokens,
                **selected.parameters, **payload, "stream": stream}
        headers = self._headers(endpoint)
        response = None
        with self._slots[selected.endpoint]:
            for attempt in range(endpoint.max_retries + 1):
                self._wait(selected.endpoint, endpoint)
                try:
                    request = self._http.build_request("POST", endpoint.url, json=body,
                                                       headers=headers, timeout=endpoint.timeout)
                    response = self._http.send(request, stream=True)
                except (httpx.ConnectError, httpx.ConnectTimeout) as exc:
                    if attempt < endpoint.max_retries:
                        self._sleep(min(0.5 * 2 ** attempt, 30))
                        continue
                    raise LLMError("connection_failed", "连接模型接入点失败") from exc
                except (httpx.TransportError, httpx.TimeoutException) as exc:
                    raise LLMError("outcome_unknown", "请求传输中断，服务商可能已执行；未自动重试") from exc
                except (TypeError, ValueError) as exc:
                    raise LLMError("invalid_input", "请求无法编码为 JSON") from exc
                if response.status_code in (429, 502, 503, 504) and attempt < endpoint.max_retries:
                    try:
                        delay = float(response.headers.get("retry-after", 0.5 * 2 ** attempt))
                    except ValueError:
                        delay = 0.5 * 2 ** attempt
                    response.close()
                    self._sleep(max(0, min(delay, 30)))
                    continue
                break
            assert response is not None
            try:
                if response.status_code >= 300:
                    code = {401: "authentication_failed", 403: "permission_denied",
                            429: "rate_limited"}.get(response.status_code, "api_error")
                    # 不回显错误正文、headers 或 request，避免服务商回显密钥。
                    raise LLMError(code, f"模型接口返回 HTTP {response.status_code}",
                                   status_code=response.status_code)
                yield response, endpoint
            except httpx.TransportError as exc:
                raise LLMError("outcome_unknown", "响应读取中断，服务商可能已执行；未自动重试") from exc
            finally:
                response.close()

    def request(self, payload: dict, *, profile: str) -> GenerationResult:
        """发送所选协议原生 JSON；保留 tools 等原生字段和完整原始响应。"""
        if not isinstance(payload, dict):
            raise LLMError("invalid_input", "请求必须是 JSON 对象")
        if payload.get("stream"):
            raise LLMError("invalid_input", "流式请求请调用 stream()")
        with self._response(payload, profile, False) as (response, endpoint):
            try:
                data = json.loads(response.read())
            except (ValueError, UnicodeError) as exc:
                raise LLMError("invalid_response", "模型接口未返回有效 JSON") from exc
            if not isinstance(data, dict):
                raise LLMError("invalid_response", "模型接口响应必须是 JSON 对象")
            return parse_response(endpoint.protocol, profile, data, response.headers.get("x-request-id"))

    def stream(self, payload: dict, *, profile: str) -> Iterator[StreamEvent]:
        """传递协议原生 SSE 事件；中途失败不自动重新发送请求。"""
        with self._response(payload, profile, True) as (response, endpoint):
            event, lines, terminal = "message", [], False
            for line in response.iter_lines():
                if line.startswith("event:"):
                    event = line[6:].strip()
                elif line.startswith("data:"):
                    lines.append(line[5:].lstrip())
                elif line == "" and lines:
                    value = "\n".join(lines)
                    lines = []
                    if value == "[DONE]":
                        if endpoint.protocol not in ("chat_completions", "fim_completions"):
                            raise LLMError("invalid_response", "所选协议不使用 [DONE] 结束事件")
                        terminal = True
                        break
                    try:
                        data = json.loads(value)
                    except ValueError as exc:
                        raise LLMError("invalid_response", "SSE data 不是有效 JSON") from exc
                    if not isinstance(data, dict):
                        raise LLMError("invalid_response", "SSE data 必须是 JSON 对象")
                    kind = data.get("type", event)
                    if kind in ("error", "response.failed") or data.get("error"):
                        raise LLMError("generation_failed", "模型流返回错误")
                    endings = {"anthropic_messages": {"message_stop"},
                               "responses": {"response.completed", "response.incomplete"}}
                    terminal = kind in endings.get(endpoint.protocol, set())
                    yield StreamEvent(kind, data)
                    event = "message"
                    if terminal:
                        break
            if not terminal:
                raise LLMError("incomplete_stream", "模型流在结束事件之前中断")
