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

- `credentials` 的值是环境变量名。实际 key 可由运行环境设置，也可通过 Web 保存到同一 TOML 的 `local_keys` 表；环境变量优先，多个接入点可共用同一引用。配置查看不会输出实际 key。
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

测试使用 HTTPX MockTransport 和合成响应，验证四种协议的请求、认证、结果解析、原生字段、SSE、错误与重试、受限并发、逐条失败以及 CLI。2026-11-01 用真实 key 调用 DeepSeek `deepseek-flash`：四种协议的非流式生成、Chat 流式和质检均成功。Prefix Completion、工具调用和批量未联调。思考占用 `max_tokens`，额度很小且未关闭思考时可能返回空文本。

HTTPX 是共用 HTTP 客户端，采用 [BSD-3-Clause](https://github.com/encode/httpx/blob/0.28.1/LICENSE.md)，与本项目 GPL-3.0 兼容。模块不引入独立数据库任务系统。

格式识别用途服务 `LLMServices.identify_import(sample, profile=...)` 复用共用接入配置；可设置 `[services] import_detection = "default"`。它返回声明式字段映射建议或无法识别，不执行代码、不猜测书目常量。Web 仅在用户选择 LLM 识别时发送未知文件的有限样本，规则识别不调用模型。模拟 API 已覆盖合法建议和物理格式篡改拒绝，未使用真实 key 联调。


Web 的「LLM 配置」可编辑凭据、全部四种协议接入点、超时/并发/限速/重试、模型及参数、三种用途默认值。API 使用 `GET/POST /api/v1/llm/settings`，无需数据库。保存到 `MIDSTREAM_LLM_CONFIG` 或 `MIDSTREAM_HOME/llm.toml`，写入前验证全部引用并检测修订冲突，采用文件锁与原子替换，文件权限为 0600。保存会重写 TOML，注释和排版不会保留。

环境变量引用保持兼容；从 Web 输入的 key 存在本地 `[local_keys]`（凭据名称到实际 key），该文件含明文凭据，不进入 Git。读取接口仅返回是否配置、环境变量/本地来源；密码输入留空保留本地值，勾选清除则移除本地值，移除凭据同时移除对应本地 key。环境变量仍由后端启动环境管理。保存不会调用付费 API或验证实际 key 有效性；后续新建的 LLM 请求读取新配置，已在运行的请求继续原配置。

### DeepSeek 快速配置

Web 的「DeepSeek 快速配置」按协议自动填入官方 Base URL、`DEEPSEEK_API_KEY` 引用、模型 ID 和基础参数，新增独立接入点/profile，不覆盖已有配置。空配置时同时设置三个用途默认值。预设默认关闭思考，输出预算 4096 tokens；模型允许的最大输出与本地预算分别显示，FIM 按官方指南另限 4096。

离线候选 `deepseek-flash`、`deepseek-v4-pro` 依据 2026-11-01 官方文档，不代表账户实时可用列表。「从 DeepSeek 刷新模型」通过后端固定地址 `GET https://api.deepseek.com/models` 获取模型 ID、显示名称、可选上下文/输出限制和思考等级。可以使用页面新输入的 key，也可使用已保存的凭据；获取模型不会保存配置或发送正文生成请求。请求不跟随跳转，不回显上游错误正文或凭据；失败时仍可使用离线候选或手工填写。

接口：`GET /api/v1/llm/deepseek`、`POST /api/v1/llm/deepseek/models`（`credential`、可选 `api_key`）。模型发现已用真实 key 调用成功（2026-11-01），Web 页面的完整流程未用真实 key 验证。

官方依据：[初次调用与模型 ID](https://api-docs.deepseek.com/en/)、[模型发现及元数据](https://api-docs.deepseek.com/api/list-models/)、[Responses](https://api-docs.deepseek.com/guides/responses_api/)、[Messages](https://api-docs.deepseek.com/guides/anthropic_api/)、[FIM](https://api-docs.deepseek.com/guides/fim_completion/)。

### Qwen 快速配置（阿里云百炼）

Web 支持北京、新加坡、弗吉尼亚三个区域，填写百炼业务空间 ID 后自动生成工作空间域名与 `/compatible-mode/v1` 地址，复用 `chat_completions` 协议。模型 ID 可从候选选择或手工填写。添加时创建独立接入点及 profile，输出预算默认 4096，附加参数留空；空配置下同时设置用途默认值。暂未增加 DashScope 原生协议、Coding Plan、视觉或音频专用流程。

凭据按区域命名，默认环境变量分别为 `DASHSCOPE_API_KEY_BEIJING`、`DASHSCOPE_API_KEY_SINGAPORE`、`DASHSCOPE_API_KEY_VIRGINIA`，也可在下方编辑成标准 `DASHSCOPE_API_KEY` 或选择已有引用。key 必须与区域匹配；选择区域时清空快速表单中的 key，防止将上一地域的新输入误用到另一地域。密钥保存和优先级沿用公共配置模块。

「从百炼刷新模型」调用官方模型目录 `/api/v1/models`，筛选 Qwen 文本生成模型，解析显示名称及可选上下文/输出限制，按页读取，最多 5 页，未取完时明确标记截断。新加坡模型目录使用官方文档列出的 `dashscope-intl.aliyuncs.com` 区域入口，其他两个区域使用工作空间域名。服务端验证区域和业务空间标识，仅构造官方域名，不跟随跳转；不发送生成请求、不回显上游正文或 key。目录结果不代表该账户已开通全部模型的推理权限。

新增接口：`GET /api/v1/llm/qwen` 与 `POST /api/v1/llm/qwen/models`，后者接收 `region`、`workspace`、`credential` 及可选 `api_key`。离线候选以 2026-11-01 官方示例中的 `qwen3.8-max` 为起点，不硬编码模型容量或价格。

参考：[OpenAI 兼容调用及工作空间域名](https://www.alibabacloud.com/help/en/model-studio/compatibility-of-openai-with-dashscope)、[官方模型目录](https://help.aliyun.com/en/model-studio/list-models)。
