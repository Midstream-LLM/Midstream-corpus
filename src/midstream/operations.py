"""长操作注册表：CLI、API 和 worker 共用同一份定义与参数模型。"""

import importlib
import inspect
from dataclasses import dataclass, field
from typing import Callable

from psycopg import Connection
from pydantic import BaseModel, ConfigDict

# 各领域包自己的 operations 模块；只在 load() 时导入。
MODULES = ('midstream.ingest.operations', 'midstream.curation.operations', 'midstream.dedup.operations',
           'midstream.datasets.operations', 'midstream.delivery.operations', 'midstream.ops.operations')


class Params(BaseModel):
    """操作参数的基类：拒绝未知字段，不做类型转换。"""
    model_config = ConfigDict(extra='forbid', strict=True)


@dataclass(frozen=True)
class Operation:
    name: str
    summary: str
    params: type[BaseModel]
    run: Callable[[Connection, BaseModel, 'Context'], dict | None]


@dataclass
class Context:
    actor: str
    reason: str
    progress: Callable[[int, int, str], None]
    follow_ups: list[tuple[str, dict]] = field(default_factory=list)


REGISTRY: dict[str, Operation] = {}


def operation(name, summary):
    def register(function):
        params = inspect.signature(function, eval_str=True).parameters
        model = list(params.values())[1].annotation
        REGISTRY[name] = Operation(name, summary, model, function)
        return function
    return register


def load() -> None:
    for module in MODULES:
        importlib.import_module(module)


def describe() -> list[dict]:
    load()
    return [{'name': op.name, 'summary': op.summary, 'params': op.params.model_json_schema()}
            for op in REGISTRY.values()]
