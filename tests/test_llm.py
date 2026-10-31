"""使用真实 HTTPX 序列化和模拟服务端响应，不调用付费 API。"""

import io
import json
import sys
import threading
import time
from dataclasses import asdict, replace
from pathlib import Path

import httpx
import pytest

from midstream.llm import Endpoint, LLMClient, LLMConfig, LLMError, LLMServices, Profile
from midstream.llm.config import PROTOCOLS


def configuration(protocol="chat_completions", concurrency=2):
    base = {"anthropic_messages": "https://example.test/anthropic",
            "fim_completions": "https://example.test/beta"}.get(protocol, "https://example.test/v1")
    return LLMConfig({"shared": "MIDSTREAM_TEST_LLM_KEY"},
                     {"test": Endpoint(protocol, base, "shared", concurrency=concurrency)},
                     {"default": Profile("test", "configured-model", 200)},
                     {"generation": "default", "quality_check": "default"})


def response_data(protocol, text="答复", finish=None):
    common = {"id": "reply-1", "model": "actual-model"}
    usage = {"input_tokens": 11, "output_tokens": 7}
    if protocol == "anthropic_messages":
        return {**common, "stop_reason": finish or "end_turn", "usage": usage,
                "content": [{"type": "thinking", "thinking": "推理"}, {"type": "text", "text": text}]}
    if protocol == "responses":
        return {**common, "status": finish or "completed", "usage": usage,
                "output": [{"type": "reasoning", "content": [{"type": "reasoning_text", "text": "推理"}]},
                           {"type": "message", "content": [{"type": "output_text", "text": text}]}]}
    choice = {"index": 0, "finish_reason": finish or "stop"}
    if protocol == "fim_completions":
        choice["text"] = text
    else:
        choice["message"] = {"role": "assistant", "content": text, "reasoning_content": "推理"}
    return {**common, "choices": [choice], "usage": {"prompt_tokens": 11, "completion_tokens": 7}}


@pytest.fixture(autouse=True)
def fake_key(monkeypatch):
    monkeypatch.setenv("MIDSTREAM_TEST_LLM_KEY", "synthetic-test-credential")


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_protocol_serialization_auth_and_response(protocol):
    paths = {"chat_completions": "/v1/chat/completions", "responses": "/v1/responses",
             "anthropic_messages": "/anthropic/v1/messages", "fim_completions": "/beta/completions"}

    def handler(request):
        assert request.url.path == paths[protocol]
        body = json.loads(request.content)
        assert body["model"] == "configured-model" and body["stream"] is False
        assert body["temperature"] == 0.2
        if protocol == "anthropic_messages":
            assert request.headers["x-api-key"] == "synthetic-test-credential"
            assert body["system"] == "规则"
            assert body["messages"] == [{"role": "user", "content": "正文"}]
        else:
            assert request.headers["authorization"] == "Bearer synthetic-test-credential"
        if protocol == "responses":
            assert body["input"] == "正文" and body["instructions"] == "规则"
            assert body["max_output_tokens"] == 200 and "max_tokens" not in body
        elif protocol == "fim_completions":
            assert body["prompt"] == "规则\n\n正文" and body["suffix"] == "后文"
        elif protocol == "chat_completions":
            assert body["messages"][0] == {"role": "system", "content": "规则"}
        return httpx.Response(200, json=response_data(protocol), headers={"x-request-id": "http-request-1"})

    with LLMClient(configuration(protocol), transport=httpx.MockTransport(handler)) as client:
        parameters = {"temperature": 0.2, **({"suffix": "后文"} if protocol == "fim_completions" else {})}
        result = LLMServices(client).generate("正文", system="规则", parameters=parameters)
        assert (result.text, result.model, result.input_tokens, result.output_tokens) == ("答复", "actual-model", 11, 7)
        assert result.provider_request_id == "http-request-1"
        assert result.raw == response_data(protocol)


def test_native_request_preserves_tool_and_multiturn_fields():
    payload = {"messages": [{"role": "assistant", "content": None, "reasoning_content": "此前推理",
                            "tool_calls": [{"id": "call-1", "type": "function",
                                            "function": {"name": "lookup", "arguments": "{}"}}]},
                           {"role": "tool", "tool_call_id": "call-1", "content": "结果"}],
               "tools": [{"type": "function", "function": {"name": "lookup", "parameters": {}}}],
               "thinking": {"type": "enabled"}, "reasoning_effort": "high"}

    def handler(request):
        body = json.loads(request.content)
        assert all(body[key] == value for key, value in payload.items())
        assert body["max_tokens"] == 200 and body["temperature"] == 0.1
        raw = response_data("chat_completions")
        raw["choices"][0]["message"]["tool_calls"] = [{"id": "call-2"}]
        return httpx.Response(200, json=raw)

    config = replace(configuration(), profiles={"default": Profile(
        "test", "configured-model", 200, {"temperature": 0.1, "thinking": {"type": "disabled"}})})
    with LLMClient(config, transport=httpx.MockTransport(handler)) as client:
        result = client.request(payload, profile="default")
        assert result.raw["choices"][0]["message"]["tool_calls"] == [{"id": "call-2"}]


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_stream_protocols(protocol):
    if protocol == "anthropic_messages":
        events = [{"type": "content_block_delta", "delta": {"type": "text_delta", "text": "𠀋答"}},
                  {"type": "message_stop"}]
    elif protocol == "responses":
        events = [{"type": "response.output_text.delta", "delta": "𠀋答"},
                  {"type": "response.completed", "response": response_data(protocol)}]
    else:
        events = [{"choices": [{"delta": {"content": "𠀋答"}}]}, "[DONE]"]
    content = ": keepalive\n\n" + "".join(
        f"data: {item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)}\n\n" for item in events)

    def handler(request):
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, content=content.encode(), headers={"content-type": "text/event-stream"})

    with LLMClient(configuration(protocol), transport=httpx.MockTransport(handler)) as client:
        result = list(client.stream({"prompt": "问题"}, profile="default"))
        assert "𠀋答" in json.dumps([asdict(e) for e in result], ensure_ascii=False)


@pytest.mark.parametrize("content,code", [
    ('data: {"choices":[]}\n\n', "incomplete_stream"),
    ('data: {"type":"response.failed","error":{"message":"failure"}}\n\n', "generation_failed"),
    ('data: not-json\n\n', "invalid_response"),
])
def test_stream_failure_is_not_retried(content, code):
    calls = []
    def handler(request):
        calls.append(request)
        return httpx.Response(200, content=content)
    with LLMClient(configuration(), transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(LLMError) as error:
            list(client.stream({}, profile="default"))
        assert error.value.code == code and len(calls) == 1


def test_retry_policy_and_secret_safe_errors():
    calls, delays = [], []
    def handler(request):
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"retry-after": "0.25"})
        return httpx.Response(200, json=response_data("chat_completions"))
    with LLMClient(configuration(), transport=httpx.MockTransport(handler), sleep=delays.append) as client:
        assert LLMServices(client).generate("正文").text == "答复"
    assert len(calls) == 2 and delays == [0.25]
    for status in (401, 400):
        calls.clear()
        def fail(request):
            calls.append(request)
            return httpx.Response(status, text="echo synthetic-test-credential")
        with LLMClient(configuration(), transport=httpx.MockTransport(fail)) as client:
            with pytest.raises(LLMError) as error:
                LLMServices(client).generate("正文")
            assert "synthetic-test-credential" not in json.dumps(error.value.as_dict())
            assert len(calls) == 1


@pytest.mark.parametrize("failure,code,calls", [(httpx.ConnectError, "connection_failed", 3),
                                               (httpx.ReadTimeout, "outcome_unknown", 1)])
def test_transport_retry_boundary(failure, code, calls):
    attempts = []
    def handler(request):
        attempts.append(request)
        raise failure("synthetic transport error", request=request)
    with LLMClient(configuration(), transport=httpx.MockTransport(handler), sleep=lambda _: None) as client:
        with pytest.raises(LLMError) as error:
            LLMServices(client).generate("正文")
        assert error.value.code == code and len(attempts) == calls


@pytest.mark.parametrize("protocol", PROTOCOLS)
def test_quality_evidence_and_invalid_model_output(protocol):
    text = "𠀋古文　网站广告\n"
    issue = {"rule": "广告", "severity": "warning", "message": "疑似站点信息", "start_char": 5,
             "end_char": 8, "evidence": text[5:8]}
    replies = [json.dumps({"summary": "有疑点", "issues": [issue]}, ensure_ascii=False),
               json.dumps({"summary": "错误位置", "issues": [{**issue, "start_char": 0}]}), "not-json"]
    def handler(request):
        return httpx.Response(200, json=response_data(protocol, replies.pop(0)))
    with LLMClient(configuration(protocol), transport=httpx.MockTransport(handler)) as client:
        services = LLMServices(client)
        checked = services.quality_check(text, rules=["广告"])
        assert checked.issues[0].evidence == text[5:8]
        assert checked.prompt_version == "corpus-quality-v1"
        for _ in range(2):
            with pytest.raises(LLMError) as error:
                services.quality_check(text)
            assert error.value.code == "invalid_quality_result"
            assert error.value.response["raw"]["id"] == "reply-1"


def test_batch_is_bounded_ordered_and_keeps_individual_errors():
    active = maximum = consumed = completed = 0
    lock = threading.Lock()
    def handler(request):
        nonlocal active, maximum, completed
        with lock:
            active += 1
            maximum = max(maximum, active)
        body = json.loads(request.content)
        prompt = body["messages"][0]["content"]
        time.sleep(0.005 if prompt == "slow" else 0.001)
        with lock:
            active -= 1
            completed += 1
        return (httpx.Response(400) if prompt == "fail" else
                httpx.Response(200, json=response_data("chat_completions", prompt)))
    def items():
        nonlocal consumed
        for index in range(20):
            consumed += 1
            assert consumed - completed <= 4
            yield {"id": f"row-{index}", "prompt": "slow" if index == 0 else "fail" if index == 3 else str(index)}
    with LLMClient(configuration(), transport=httpx.MockTransport(handler)) as client:
        results = list(LLMServices(client).generate_batch(items()))
    assert [r.id for r in results] == [f"row-{index}" for index in range(20)]
    assert results[3].result is None and results[3].error["status_code"] == 400
    assert all(r.result is not None for i, r in enumerate(results) if i != 3)
    assert maximum == 2


def test_configuration_and_missing_key(tmp_path, monkeypatch):
    sample = Path(__file__).parents[1] / "examples/llm.toml"
    config = LLMConfig.load(sample)
    assert {ep.protocol for ep in config.endpoints.values()} == set(PROTOCOLS)
    assert set(config.credentials) == {"deepseek"}
    monkeypatch.setenv("DEEPSEEK_API_KEY", "synthetic-secret")
    assert "synthetic-secret" not in json.dumps(config.describe())
    assert all(ep["key_configured"] for ep in config.describe()["endpoints"].values())
    monkeypatch.delenv("MIDSTREAM_TEST_LLM_KEY")
    with LLMClient(configuration()) as client:
        with pytest.raises(LLMError) as error:
            LLMServices(client).generate("正文")
        assert error.value.code == "missing_credential"
    invalid = tmp_path / "bad.toml"
    invalid.write_text('bad = "private-secret"\nbroken = [')
    with pytest.raises(LLMError) as error:
        LLMConfig.load(invalid)
    assert "private-secret" not in str(error.value)
    with pytest.raises(LLMError):
        replace(configuration().endpoints["test"], timeout=float("nan"))


def test_llm_cli_works_without_database_and_handles_jsonl(monkeypatch, capsys, tmp_path):
    from midstream import cli
    from midstream.llm import cli as llm_cli
    sample = Path(__file__).parents[1] / "examples/llm.toml"
    def no_database():
        raise AssertionError("LLM CLI must not start PostgreSQL")
    monkeypatch.setattr(cli.server, "ensure_running", no_database)
    monkeypatch.setattr(sys, "argv", ["midstream", "llm", "config", "--config", str(sample)])
    assert cli.main() == 0
    assert "endpoints" in json.loads(capsys.readouterr().out)

    def handler(request):
        body = json.loads(request.content)
        return httpx.Response(200, json=response_data("chat_completions", body["messages"][0]["content"]))
    original = llm_cli.LLMClient
    monkeypatch.setattr(llm_cli, "LLMClient", lambda config: original(config, transport=httpx.MockTransport(handler)))
    config_path = tmp_path / "llm.toml"
    config_path.write_text(sample.read_text().replace("DEEPSEEK_API_KEY", "MIDSTREAM_TEST_LLM_KEY"))
    monkeypatch.setattr(sys, "stdin", io.StringIO('{"id":"a","prompt":"正文"}\nnot-json\n{"id":"b","prompt":"续文"}\n'))
    monkeypatch.setattr(sys, "argv", ["midstream", "llm", "generate-batch", "--config", str(config_path)])
    assert cli.main() == 1
    results = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert [r["id"] for r in results] == ["a", "1", "b"]
    assert results[1]["error"]["code"] == "invalid_input" and results[2]["result"]["text"] == "续文"


def test_different_service_defaults_and_quality_batch():
    base = configuration()
    config = LLMConfig(base.credentials,
                       {**base.endpoints, "messages": Endpoint("anthropic_messages", "https://example.test/anthropic/v1", "shared")},
                       {**base.profiles, "quality": Profile("messages", "quality-model")},
                       {"generation": "default", "quality_check": "quality"})
    def handler(request):
        if request.url.path == "/anthropic/v1/messages":
            assert json.loads(request.content)["model"] == "quality-model"
            return httpx.Response(200, json=response_data("anthropic_messages", '{"summary":"无疑点","issues":[]}'))
        return httpx.Response(200, json=response_data("chat_completions"))
    with LLMClient(config, transport=httpx.MockTransport(handler)) as client:
        services = LLMServices(client)
        assert services.generate("正文").protocol == "chat_completions"
        result = list(services.quality_check_batch([{"id": "q1", "text": "甲文"}, "乙文", {"text": ""}]))
        assert result[0].result.generation.profile == "quality" and result[1].error is None
        assert result[2].error["code"] == "invalid_input"


def test_stream_reads_utf8_across_chunks_and_closes_after_read_error():
    class BrokenStream(httpx.SyncByteStream):
        closed = False
        def __iter__(self):
            payload = 'data: {"choices":[{"delta":{"content":"𠀋"}}]}\n\n'.encode()
            for index in range(0, len(payload), 2):
                yield payload[index:index + 2]
            raise httpx.ReadError("synthetic interrupted response")
        def close(self):
            self.closed = True
    body = BrokenStream()
    with LLMClient(configuration(), transport=httpx.MockTransport(
            lambda _: httpx.Response(200, stream=body))) as client:
        events = client.stream({}, profile="default")
        assert next(events).data["choices"][0]["delta"]["content"] == "𠀋"
        with pytest.raises(LLMError) as error:
            list(events)
        assert error.value.code == "outcome_unknown" and body.closed


@pytest.mark.parametrize("raw", ["not-json", "[]", '{"model":"test","choices":[]}'])
def test_malformed_provider_response(raw):
    with LLMClient(configuration(), transport=httpx.MockTransport(
            lambda _: httpx.Response(200, text=raw))) as client:
        with pytest.raises(LLMError) as error:
            LLMServices(client).generate("正文")
        assert error.value.code == "invalid_response"


def test_quality_rejects_truncated_output_even_if_json_is_valid():
    raw = response_data("chat_completions", '{"summary":"未完成","issues":[]}', finish="length")
    with LLMClient(configuration(), transport=httpx.MockTransport(lambda _: httpx.Response(200, json=raw))) as client:
        with pytest.raises(LLMError) as error:
            LLMServices(client).quality_check("正文")
        assert error.value.code == "invalid_quality_result"
        assert error.value.response["finish_reason"] == "length"
