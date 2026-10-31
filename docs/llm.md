# LLM 服务模块

`midstream.llm` 按用途提供服务函数，共用接入配置和请求执行层。它可以由 core 或刷数据脚本独立调用，CLI 的 `llm` 子命令无需启动 PostgreSQL。

当前提供：

- `generate`：自定义 prompt，可附加 system 指令和协议参数。
- `quality_check`：古汉语语料质检，返回问题、原文位置和逐字证据，供调用者决定如何使用。
- `generate_batch`、`quality_check_batch`：有界并发的逐条请求，逐条返回结果或错误，保持输入顺序。
- `LLMClient.request`、`LLMClient.stream`：使用指定协议的原生 JSON 请求及 SSE 流式事件，保留原始响应，可传递多轮消息、工具调用、图像等协议字段。

模块返回结果，不自动写入语料库或改变质量状态。core 负责选材、关联文档版本及后续入库。API key、接入点、模型和请求控制集中管理。

## 协议

按 2026-10-31 核对的 DeepSeek 官方生成接口实现：

| `protocol` | 示例 `base_url` | 请求路径 |
|---|---|---|
| `chat_completions` | `https://api.deepseek.com` | `/chat/completions` |
| `responses` | `https://api.deepseek.com` | `/responses` |
| `anthropic_messages` | `https://api.deepseek.com/anthropic` | `/v1/messages` |
| `fim_completions` | `https://api.deepseek.com/beta` | `/completions` |

Chat/Responses 接入点也可配置包含 `/v1` 的兼容地址；Messages 若已带 `/v1`，不会重复追加。Chat Prefix Completion 使用 Chat 协议加 `/beta` 接入点，通过原生请求传入 prefix 等字段。

协议层提供生成请求和响应兼容，不自动执行模型返回的工具调用，也不包含 Files、Search/Fetch 等独立资源 API。原生请求保留协议字段；某些参数是否有效仍取决于模型与供应商。SSE 传递协议原始事件，保留推理、文本、工具调用和结束信息。

官方依据：[Chat Completions](https://api-docs.deepseek.com/api/create-chat-completion/)、[Responses](https://api-docs.deepseek.com/guides/responses_api/)、[Anthropic Messages](https://api-docs.deepseek.com/guides/anthropic_api/)、[FIM](https://api-docs.deepseek.com/api/create-completion/)。

## 接入配置

配置使用 TOML，完整示例见 [examples/llm.toml](../examples/llm.toml)。可以直接传路径、用 `MIDSTREAM_LLM_CONFIG` 指定，或放到 `MIDSTREAM_HOME/llm.toml`（默认 `~/.local/share/midstream/llm.toml`）。

```toml
[credentials]
deepseek = "DEEPSEEK_API_KEY"

[endpoints.main]
protocol = "chat_completions"
base_url = "https://api.deepseek.com"
credential = "deepseek"
concurrency = 4
max_retries = 2
timeout = 120
requests_per_second = 2

[profiles.default]
endpoint = "main"
model = "deepseek-flash"
max_tokens = 4096

[profiles.default.parameters]
thinking = { type = "disabled" }

[services]
generation = "default"
quality_check = "default"
```

- `credentials` 的值是环境变量名。实际 key 由运行环境设置，多个接入点可共用同一引用；配置查看不会输出实际 key。
- `endpoints` 管协议、地址和执行限制；`concurrency` 为该客户端内每个接入点的同时请求上限，`requests_per_second=0`（默认）表示不限制启动速率。不同客户端/进程的限制独立。
- `profiles` 将接入点、模型及默认参数组成命名配置。`max_tokens` 在 Responses 下转换为 `max_output_tokens`；`parameters` 使用所选协议的原生参数名，调用时可覆盖。
- `services` 分别指定生成与质检的默认 profile；函数和 CLI 也可显式选择。

未知配置字段、无效引用和缺失 key 会返回明确错误。示例模型名不是自动发现的模型版本，换模型时更新配置即可。

## Python 调用

```python
from midstream.llm import LLMServices

with LLMServices.from_config() as llm:
    generated = llm.generate("请解释：学而时习之。")
    print(generated.text)
    print(generated.model, generated.input_tokens, generated.output_tokens)

    checked = llm.quality_check("学而时习之。网站广告", rules=["检查混入的网站广告"])
    for issue in checked.issues:
        print(issue.start_char, issue.end_char, issue.evidence)

    for item in llm.generate_batch([
        {"id": "doc-1", "prompt": "解释：学而时习之。"},
        {"id": "doc-2", "prompt": "解释：有朋自远方来。"},
    ]):
        if item.error:
            print(item.id, item.error)
        else:
            print(item.id, item.result.text)
```

`generate` 接收 `prompt`、可选 `system`、`parameters` 和 `profile`。`quality_check` 接收 `text`、可选非空 `rules` 列表、`parameters` 和 `profile`。不传规则时使用网站杂质、乱码和异常重复的默认检查规则。

统一生成结果包含文本、推理文本（若供应商提供）、实际返回模型名、协议/profile、响应 ID、HTTP 请求 ID、结束原因、输入/输出 token 数、供应商原始 usage 和完整原始 JSON。结果是 dataclass，可用 `dataclasses.asdict()` 保存。缺失的 usage 数值留空，不估算。

质检提示词版本为 `corpus-quality-v1`。结果中的 `issues` 每项有 `rule`、`severity`（`info`/`warning`/`error`）、`message`、`start_char`、`end_char`、`evidence`。位置按 Python Unicode 字符索引、从 0 开始、左闭右开；必须满足 `text[start_char:end_char] == evidence`。格式错误、位置越界、证据不匹配或生成被截断时返回 `invalid_quality_result`，并在错误 `response` 中保留实际生成结果，供检查及复核。它不把模型判定记录成人工批准。

批量方法接受字符串或对象的 iterable；字符串直接作为 prompt/text，对象可带 `id` 及该服务的输入字段。未提供 ID 时使用从 0 开始的输入序号；调用者应为自行提供的 ID 保证唯一性。`workers` 可控制线程数，接入点的并发上限仍有效。在途条目最多约为线程数的两倍；输入、future 和响应不会全部预加载。返回迭代器需在客户端关闭前消费。

这里的批量是受限并发的独立请求。核对的 DeepSeek 官方接口目录未提供原生 Batch 作业端点，因此本版不提交供应商批处理作业，不宣称具有供应商 Batch 的计费或时效特性。

## 原生协议与流式请求

```python
with LLMServices.from_config() as llm:
    response = llm.client.request(
        {"input": "解释：学而时习之。", "reasoning": {"effort": "none"}},
        profile="responses",
    )
    for event in llm.client.stream(
        {"messages": [{"role": "user", "content": "解释：学而时习之。"}]},
        profile="default",
    ):
        print(event.event, event.data)
```

原生请求补齐 profile 的默认模型、输出上限及参数，请求中显式提供的字段优先。`request()` 为非流式；`stream()` 强制流式，逐个返回原协议 SSE 事件。调用者可保存原始响应中的多轮/工具字段并在下一次请求传回。流式接收发生错误或缺少协议结束事件时会报错，不会重新播放已输出内容。

## CLI

```bash
# 不需要启动 PostgreSQL；检查配置和 key 是否已设置（不输出 key）
midstream llm config --config examples/llm.toml

# 单条：输入为 JSON 对象
midstream llm generate --input prompt.json --profile responses
midstream llm quality-check --input text.json

# 批量：每行一个 JSON 对象，按输入顺序输出逐条 JSONL
midstream llm generate-batch --input prompts.jsonl --workers 4
midstream llm quality-check-batch --input texts.jsonl

# 原生协议 JSON 和 SSE（流事件以 JSONL 输出）
midstream llm request --profile messages --input request.json
midstream llm stream --profile default --input request.json
```

每个操作均可加 `--config`，`--input -`（默认）读取 stdin。生成输入如 `{"prompt":"解释：学而时习之。"}`；质检输入如 `{"text":"正文","rules":["检查广告"]}`。批量条目可附加 `id`；无效条目也输出对应错误，其余条目继续。单条命令不能直接输入裸文本。

退出码：0 表示成功，1 表示批量中至少一条失败，2 表示配置、输入或单条调用错误。错误也为 JSON。模型输出可能包含语料正文，调用方应按项目数据边界保存，示例中的结果不写入 Git。

## 请求恢复与验证范围

连接建立失败和 HTTP 429/502/503/504 按配置重试，`max_retries` 是初次请求之外的次数；短数字 `Retry-After` 会被采用，单次等待上限 30 秒。读取/写入中断或超时标为 `outcome_unknown`，不会自动重试，因为供应商可能已执行。批量中的错误独立返回，调用方可选择后续处理方式；不保证外部请求只发生一次。

测试使用 HTTPX MockTransport 和合成响应，验证四种协议的请求、认证、结果解析、原生字段、SSE、错误与重试、受限并发、逐条失败以及 CLI。尚未使用真实 key 调用 DeepSeek，真实接入的兼容性需另外联调。

HTTPX 是共用 HTTP 客户端，采用 [BSD-3-Clause](https://github.com/encode/httpx/blob/0.28.1/LICENSE.md)，与本项目 GPL-3.0 兼容。模块不引入独立数据库任务系统。
