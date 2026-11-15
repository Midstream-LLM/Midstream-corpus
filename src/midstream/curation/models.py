"""Machine-readable curation input, research actions and claim contracts."""
from typing import Literal
from pydantic import Field, model_validator
from ..corpus.metadata import Model, Metadata


from ..corpus.taxonomy import TAXONOMY, TAXONOMY_VERSION
CURATION_VERSION = 'curation-v1'
FIELDS = {'title', 'authors', 'contributors', 'composed_years', 'composed_label',
          'edition_years', 'genre', 'category', 'topics', 'tags', 'tune_pattern', 'quality_flags', 'external_ids'}


class CurationConfig(Model):
    profile: str | None = None
    search_profile: str | None = None
    use_search: bool = True
    use_authorities: bool = True
    apply: bool = True
    overwrite: bool = False
    allow_text_edits: bool = False
    force: bool = False
    confidence_threshold: float = Field(default=0.90, ge=0, le=1)
    steps: list[Literal['metadata_enrichment', 'tagging', 'text_cleanup', 'segmentation']] = Field(
        default_factory=lambda: ['metadata_enrichment', 'tagging', 'text_cleanup', 'segmentation'], min_length=1)
    max_tool_calls: int = Field(default=6, ge=0, le=20)
    chunk_chars: int = Field(default=12000, ge=1000, le=30000)
    # 书目考证和打标签读取的正文窗口：开头加三个均匀片段。
    context_chars: int = Field(default=16000, ge=2000, le=60000)
    # 文本修复和切分逐段各调用一次模型；超过此字数的正文不做这两步。
    max_text_chars: int = Field(default=400000, ge=1000, le=50_000_000)

    @model_validator(mode='after')
    def unique_steps(self):
        if len(set(self.steps)) != len(self.steps):
            raise ValueError('步骤不能重复')
        return self


class ResumeOptions(Model):
    max_tool_calls: int | None = Field(default=None, ge=0, le=20)
    max_text_chars: int | None = Field(default=None, ge=1000, le=50_000_000)
    apply: bool | None = None


class Evidence(Model):
    key: str
    provider: str
    reference: str
    title: str = ''
    excerpt: str = Field(default='', max_length=20000)
    data: dict = Field(default_factory=dict)
    query: str | None = None
    retrieved_at: str | None = None


class TextPatch(Model):
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    before: str = Field(min_length=1)
    after: str

    @model_validator(mode='after')
    def valid(self):
        if self.end_char <= self.start_char or '\x00' in self.after:
            raise ValueError('无效文本补丁')
        return self


class Segment(Model):
    start_char: int = Field(ge=0)
    end_char: int = Field(gt=0)
    title: str | None = None
    unit_type: Literal['volume', 'chapter', 'article', 'poem', 'section']

    @model_validator(mode='after')
    def valid(self):
        if self.end_char <= self.start_char:
            raise ValueError('无效篇章边界')
        return self


class Claim(Model):
    kind: Literal['metadata', 'text_patch', 'segment'] = 'metadata'
    field: str
    value: object
    evidence_keys: list[str] = Field(default_factory=list)
    confidence: float = Field(ge=0, le=1)
    rationale: str = Field(min_length=1, max_length=3000)

    @model_validator(mode='after')
    def valid(self):
        if self.kind == 'metadata':
            if self.field not in FIELDS:
                raise ValueError('不允许修改该元数据字段')
            self.value = Metadata.model_validate({self.field: self.value}).model_dump()[self.field]
            if self.field == 'tags':
                for tag in self.value:
                    if tag['tag_id'] not in TAXONOMY[tag['taxonomy']]:
                        raise ValueError('未知 taxonomy 标签')
        elif self.kind == 'text_patch':
            if self.field != 'text':
                raise ValueError('文本补丁的 field 必须为 text')
            self.value = TextPatch.model_validate(self.value).model_dump()
        else:
            if self.field != 'segments':
                raise ValueError('篇章的 field 必须为 segments')
            self.value = Segment.model_validate(self.value).model_dump()
        return self


class ResearchResponse(Model):
    action: Literal['finish', 'search', 'fetch', 'wikidata', 'gutenberg']
    query: str = Field(default='', max_length=2000)
    claims: list[Claim] = Field(default_factory=list, max_length=200)
    summary: str = Field(default='', max_length=4000)

    @model_validator(mode='after')
    def valid(self):
        if self.action != 'finish' and (not self.query.strip() or self.claims):
            raise ValueError('工具请求需要 query，claims 应留空')
        return self
