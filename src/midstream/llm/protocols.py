"""DeepSeek 的四种生成接口：原生请求透传，服务调用使用统一文本结果。"""

from dataclasses import dataclass, field

from .config import Profile
from .errors import LLMError


@dataclass(frozen=True)
class GenerationResult:
    text: str
    reasoning: str
    model: str
    protocol: str
    profile: str
    response_id: str | None
    provider_request_id: str | None
    finish_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    usage: dict
    raw: dict = field(repr=False)


def generation_payload(protocol: str, profile: Profile, prompt: str, system: str | None,
                       parameters: dict | None = None) -> dict:
    if not isinstance(prompt, str) or not prompt.strip() or (system is not None and not isinstance(system, str)):
        raise LLMError("invalid_input", "prompt 必须是非空字符串，system 必须是字符串")
    if parameters is not None and not isinstance(parameters, dict):
        raise LLMError("invalid_input", "parameters 必须是对象")
    options = {**profile.parameters, **(parameters or {})}
    if {"model", "stream", "messages", "input", "instructions", "prompt", "system"} & options.keys():
        raise LLMError("invalid_input", "parameters 不能覆盖模型、输入或 stream")
    body = {"model": profile.model, **options}
    if protocol == "responses":
        body.setdefault("max_output_tokens", profile.max_tokens)
        body["input"] = prompt
        if system:
            body["instructions"] = system
    elif protocol == "fim_completions":
        body.setdefault("max_tokens", profile.max_tokens)
        body["prompt"] = f"{system}\n\n{prompt}" if system else prompt
    else:
        body.setdefault("max_tokens", profile.max_tokens)
        body["messages"] = [{"role": "user", "content": prompt}]
        if system:
            if protocol == "anthropic_messages":
                body["system"] = system
            else:
                body["messages"].insert(0, {"role": "system", "content": system})
    return body


def parse_response(protocol: str, profile: str, data: dict, request_id: str | None) -> GenerationResult:
    try:
        text, reasoning, finish = "", "", None
        usage = data.get("usage") or {}
        if protocol in ("chat_completions", "fim_completions"):
            choice = data["choices"][0]
            finish = choice.get("finish_reason")
            if protocol == "fim_completions":
                text = choice["text"]
            else:
                message = choice["message"]
                text = message.get("content") or ""
                reasoning = message.get("reasoning_content") or ""
            input_tokens, output_tokens = usage.get("prompt_tokens"), usage.get("completion_tokens")
        elif protocol == "anthropic_messages":
            text = "".join(block["text"] for block in data["content"] if block.get("type") == "text")
            reasoning = "".join(block["thinking"] for block in data["content"] if block.get("type") == "thinking")
            finish = data.get("stop_reason")
            input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
        else:
            if data.get("status") == "failed" or data.get("error"):
                raise LLMError("generation_failed", "服务商返回生成失败")
            for item in data["output"]:
                if item.get("type") == "message":
                    text += "".join(block["text"] for block in item.get("content", [])
                                    if block.get("type") == "output_text")
                elif item.get("type") == "reasoning":
                    reasoning += "".join(block["text"] for block in item.get("content", [])
                                         if block.get("type") == "reasoning_text")
            finish = (data.get("incomplete_details") or {}).get("reason") or data.get("status")
            input_tokens, output_tokens = usage.get("input_tokens"), usage.get("output_tokens")
        if (not isinstance(text, str) or not isinstance(reasoning, str) or not isinstance(usage, dict)
                or not isinstance(data["model"], str)):
            raise TypeError
        return GenerationResult(text, reasoning, data["model"], protocol, profile, data.get("id"),
                                request_id, finish, input_tokens, output_tokens, usage, data)
    except (KeyError, IndexError, TypeError, AttributeError) as exc:
        raise LLMError("invalid_response", "服务商返回的 JSON 不符合所选协议") from exc
