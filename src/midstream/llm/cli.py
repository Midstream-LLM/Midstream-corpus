"""不启动 PostgreSQL 的独立 LLM 命令，JSON 输入及 JSON/JSONL 输出。"""

import json
import sys
from dataclasses import asdict
from pathlib import Path

from .config import LLMConfig
from .errors import LLMError
from .services import LLMServices
from .client import LLMClient


def add_commands(commands):
    parent = commands.add_parser("llm", help="LLM 质检、生成、原生协议及批量调用")
    actions = parent.add_subparsers(dest="llm_action", required=True)
    for action in ("config", "generate", "quality-check", "generate-batch", "quality-check-batch", "request", "stream"):
        parser = actions.add_parser(action)
        parser.add_argument("--config", type=Path, help="TOML 接入配置，默认 MIDSTREAM_LLM_CONFIG")
        if action != "config":
            parser.add_argument("--profile")
            parser.add_argument("--input", default="-", help="JSON 文件，批量用 JSONL；- 表示 stdin")
        if action.endswith("-batch"):
            parser.add_argument("--workers", type=int)


def _json(line):
    try:
        return json.loads(line)
    except ValueError as exc:
        raise LLMError("invalid_input", "输入不是有效 JSON") from exc


def _items(handle):
    for line in handle:
        if line.strip():
            try:
                yield _json(line)
            except LLMError:
                # 让单条输入错误也经批量结果返回，不丢掉其他已执行条目的结果。
                yield {"invalid_json_line": True}


def run(args) -> int:
    def output(value):
        print(json.dumps(value, ensure_ascii=False), flush=True)

    try:
        config = LLMConfig.load(args.config)
        if args.llm_action == "config":
            output(config.describe())
            return 0
        with LLMServices(LLMClient(config)) as services:
            handle = sys.stdin if args.input == "-" else Path(args.input).open(encoding="utf-8")
            try:
                if args.llm_action.endswith("-batch"):
                    items = _items(handle)
                    method = (services.generate_batch if args.llm_action == "generate-batch"
                              else services.quality_check_batch)
                    failed = False
                    for result in method(items, profile=args.profile, workers=args.workers):
                        output(asdict(result))
                        failed |= result.error is not None
                    return 1 if failed else 0
                data = _json(handle.read())
                if not isinstance(data, dict):
                    raise LLMError("invalid_input", "输入必须是 JSON 对象")
                if args.llm_action in ("request", "stream"):
                    profile, _, _ = config.resolve("generation", args.profile)
                    if args.llm_action == "stream":
                        for event in services.client.stream(data, profile=profile):
                            output(asdict(event))
                    else:
                        output(asdict(services.client.request(data, profile=profile)))
                else:
                    method = services.generate if args.llm_action == "generate" else services.quality_check
                    fields = ({"prompt", "system", "parameters"} if args.llm_action == "generate"
                              else {"text", "rules", "parameters"})
                    required = "prompt" if args.llm_action == "generate" else "text"
                    if set(data) - fields or required not in data:
                        raise LLMError("invalid_input", "输入字段缺失或包含未知字段")
                    output(asdict(method(profile=args.profile, **data)))
                return 0
            finally:
                if handle is not sys.stdin:
                    handle.close()
    except LLMError as exc:
        output({"status": "error", **exc.as_dict()})
        return 2
    except (OSError, UnicodeError):
        output({"status": "error", "code": "input_error", "message": "无法读取输入文件"})
        return 2
