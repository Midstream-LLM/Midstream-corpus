"""数据集配方：输入模型、选用规则和错误类型。"""

from dataclasses import dataclass, field
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field

from ..corpus.query import Filters

SPLIT_NAMES = ("test", "train", "val")


class ExportError(Exception):
    pass


class TrainingLeak(ExportError):
    pass


class _Input(BaseModel):
    model_config = ConfigDict(extra='forbid')


class _ReservedRelease(_Input):
    release_id: int = Field(gt=0)
    split: Literal['train', 'val', 'test']


class _Reserved(_Input):
    doc_ids: list[Annotated[int, Field(gt=0)]] = Field(default_factory=list)
    releases: list[_ReservedRelease] = Field(default_factory=list)


class _Limit(_Input):
    max_chars: int | None = Field(default=None, ge=0)
    max_documents: int | None = Field(default=None, ge=0)


class _RecipeInput(_Input):
    name: str = Field(min_length=1, max_length=200, pattern=r'^[^/\\\x00]+$')
    seed: str
    splits: dict[Literal['train', 'val', 'test'], Annotated[float, Field(ge=0, le=1)]]
    filters: dict = Field(default_factory=dict)
    selection: dict = Field(default_factory=dict)
    include: list[dict] | None = None
    exclude: list[dict] = Field(default_factory=list)
    documents: dict[Literal['add', 'remove'], list[Annotated[int, Field(gt=0)]]] = Field(default_factory=dict)
    split_by: Literal['chars', 'documents'] = 'chars'
    stratify: list[Literal['source', 'category']] = Field(default_factory=list)
    limits: dict[Literal['train', 'val', 'test'], _Limit] = Field(default_factory=dict)
    reserved: dict[Literal['val', 'test'], _Reserved] = Field(default_factory=dict)
    variant: Literal['simplified'] | None = None


@dataclass(frozen=True)
class Selection:
    """重复文本的选用规则；默认不去重，保留筛选结果的全部文档。"""

    dedup: str = "none"
    source_priority: tuple[str, ...] = ()
    min_cut_chars: int = 64
    scripts: str = "separate"
    # 已有发布：其中与候选同字形的成员视为已保留且优先，候选与之重复的部分舍弃或裁掉；这些成员本身不进新发布。
    dedup_against: tuple[int, ...] = ()

    @classmethod
    def load(cls, data: dict) -> "Selection":
        unknown = set(data) - set(cls.__dataclass_fields__)
        if unknown:
            raise ExportError(f"未知的选用字段：{sorted(unknown)}")
        selection = cls(data.get("dedup", "none"), tuple(data.get("source_priority", ())), data.get('min_cut_chars',64),
                        data.get('scripts', 'separate'), tuple(data.get('dedup_against', ())))
        if type(selection.min_cut_chars) is not int or selection.min_cut_chars < 1:
            raise ExportError('min_cut_chars 必须为正整数')
        if selection.scripts not in ("separate", "merge"):
            raise ExportError(f"未知的繁简处理方式：{selection.scripts}")
        if selection.dedup not in ("none", "document", "span"):
            raise ExportError(f"未知的去重方式：{selection.dedup}")
        if any(type(i) is not int or i < 1 for i in selection.dedup_against):
            raise ExportError('dedup_against 必须是发布编号列表')
        if selection.dedup_against and selection.dedup == 'none':
            raise ExportError('dedup_against 需要 dedup 为 document 或 span')
        return selection


@dataclass(frozen=True)
class Recipe:
    name: str
    filters: Filters
    splits: dict[str, float]
    seed: str
    raw: dict
    selection: Selection = Selection()
    include: tuple[Filters, ...] = ()
    exclude: tuple[Filters, ...] = ()
    documents: dict = field(default_factory=dict)
    split_by: str = 'chars'
    stratify: tuple[str, ...] = ()
    limits: dict = field(default_factory=dict)
    reserved: dict = field(default_factory=dict)
    variant: str | None = None

    @classmethod
    def load(cls, data: dict) -> "Recipe":
        body = _RecipeInput.model_validate(data)
        splits = body.splits
        if abs(sum(splits.values()) - 1) > 1e-9:
            raise ExportError("划分比例之和必须为 1")
        if body.name in ('.', '..'):
            raise ExportError('数据集名不能是路径')
        filters = Filters.from_dict({"quality": ["ok"], **body.filters})
        includes = (filters,) if body.include is None else tuple(
            Filters.from_dict({'quality': ['ok'], **item}) for item in body.include)
        excludes = tuple(Filters.from_dict({'quality': ['ok', 'flagged', 'quarantined'], **item})
                         for item in body.exclude)
        return cls(body.name, filters, splits, body.seed, data, Selection.load(body.selection),
                   includes, excludes, body.documents, body.split_by, tuple(body.stratify),
                   {key: value.model_dump(exclude_none=True) for key, value in body.limits.items()},
                   {key: value.model_dump() for key, value in body.reserved.items()}, body.variant)
