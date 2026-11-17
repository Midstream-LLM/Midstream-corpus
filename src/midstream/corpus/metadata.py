"""Typed metadata contract shared by staging, version writers and curation.

Year bounds are inclusive historical years, with no year zero. PostgreSQL range
strings and legacy two-element arrays are accepted only at conversion boundaries.
"""
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator, model_validator, create_model


class Model(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)


class YearRange(Model):
    start_year: int | None = None
    end_year: int | None = None

    @model_validator(mode='after')
    def valid(self):
        a, b = self.start_year, self.end_year
        if a == 0 or b == 0 or (a is None and b is None):
            raise ValueError('年份未知请使用 null；历史年份没有零年')
        if any(x is not None and not -999999 <= x <= 999999 for x in (a, b)):
            raise ValueError('年份超出允许范围')
        if a is not None and b is not None and a > b:
            raise ValueError('起始年份不能晚于结束年份')
        return self

    def database(self):
        a = '' if self.start_year is None else self.start_year
        b = '' if self.end_year is None else self.end_year + 1
        left = '(' if self.start_year is None else '['
        return f'{left}{a},{b})'


def years(value):
    if value is None:
        return None
    if isinstance(value, (list, tuple)) and len(value) == 2:
        value = dict(zip(('start_year', 'end_year'), value))
    if isinstance(value, str):
        match = re.fullmatch(r'([\[(])(-?\d*),(-?\d*)([\])])', value)
        if not match:
            raise ValueError('无效年份区间')
        left, a, b, right = match.groups()
        value = {'start_year': int(a) + (left == '(') if a else None,
                 'end_year': int(b) - (right == ')') if b else None}
    return YearRange.model_validate(value).model_dump()


STEPS = ('basic_clean', 'metadata_enrichment', 'tagging', 'text_cleanup',
         'segmentation', 'ai_curation', 't2s', 'manual_edit', 'quality_check',
         'collation_clean', 'rule_reprocess', 'curation_rollback')


def step_flags(value=None):
    value = {} if value is None else value
    if not isinstance(value, dict) or any(not re.fullmatch(r'[a-z][a-z0-9_]*', k)
                                         or type(v) is not bool for k, v in value.items()):
        raise ValueError('processed_steps 必须是 snake_case 键与 bool 值的对象')
    return {**dict.fromkeys(STEPS, False), **value}


class Contributor(Model):
    name: str = Field(min_length=1, max_length=300)
    role: Literal['author', 'compiler', 'commentator', 'translator', 'editor']
    entity_id: str | None = None


from .taxonomy import TAXONOMY


class Tag(Model):
    taxonomy: Literal['era', 'genre', 'class', 'region', 'register', 'topic']
    tag_id: str = Field(pattern=r'^[a-z][a-z0-9_.-]*$')

    @model_validator(mode='after')
    def registered(self):
        if self.tag_id not in TAXONOMY[self.taxonomy]:
            raise ValueError('未登记的 taxonomy/tag_id')
        return self


class Metadata(Model):
    quality_status: Literal['ok', 'flagged', 'quarantined'] = 'ok'
    quality_flags: list[str] = Field(default_factory=list)
    license: str | None = None
    title: str | None = None
    authors: list[str] | None = None
    composed_years: YearRange | None = None
    composed_label: str | None = None
    edition_years: YearRange | None = None
    genre: str | None = None
    category: str | None = None
    topics: list[str] | None = None
    legacy_category: str | None = None
    field_basis: dict[str, str] = Field(default_factory=dict)
    extra: dict = Field(default_factory=dict)
    contributors: list[Contributor] = Field(default_factory=list)
    tags: list[Tag] = Field(default_factory=list)
    tune_pattern: str | None = None
    processed_steps: dict[str, StrictBool] = Field(default_factory=step_flags)
    external_ids: dict[str, str] = Field(default_factory=dict)
    field_claims: dict[str, list[int]] = Field(default_factory=dict)

    @field_validator('composed_years', 'edition_years', mode='before')
    @classmethod
    def range_input(cls, value):
        return years(value)

    @field_validator('processed_steps', mode='before')
    @classmethod
    def flags(cls, value):
        return step_flags(value)

    @field_validator('external_ids')
    @classmethod
    def identifiers(cls, value):
        patterns = {'wikidata': r'Q[1-9]\d*', 'gutenberg': r'[1-9]\d*', 'cbdb': r'[1-9]\d*'}
        if any(k not in patterns or not re.fullmatch(patterns[k], v) for k, v in value.items()):
            raise ValueError('无效外部实体 ID')
        return value


class RecordMetadata(Metadata):
    unit_type: str | None = None
    work_title: str | None = None
    work_relation: Literal['whole', 'part', 'excerpt', 'contains'] | None = None

    @model_validator(mode='after')
    def source_roles(self):
        if self.authors and not self.contributors:
            self.contributors = [Contributor(name=name, role='author') for name in self.authors]
            if 'authors' in self.field_basis:
                self.field_basis.setdefault('contributors', self.field_basis['authors'])
        elif self.contributors:
            names = [p.name for p in self.contributors if p.role == 'author'] or None
            if self.authors is None:
                self.authors = names
            elif self.authors != names:
                raise ValueError('来源的 authors 与 contributors 不一致')
        return self


VERSION_FIELDS = tuple(Metadata.model_fields)
EDITABLE_FIELDS = tuple(k for k in VERSION_FIELDS if k not in ('field_basis','field_claims','processed_steps'))
RANGES = ('composed_years', 'edition_years')
ARRAYS = ('quality_flags', 'authors', 'topics')
JSON_FIELDS = ('field_basis', 'extra', 'contributors', 'tags', 'processed_steps', 'external_ids', 'field_claims')


class _Patch(Model):
    @field_validator('composed_years', 'edition_years', mode='before', check_fields=False)
    @classmethod
    def range_input(cls, value):
        return years(value)

    @model_validator(mode='after')
    def validate_fields(self):
        payload = self.model_dump(exclude_unset=True)
        if not payload:
            raise ValueError('元数据修改不能为空')
        Metadata.model_validate(payload)
        return self


MetadataPatch = create_model('MetadataPatch', __base__=_Patch,
    **{name:(Metadata.model_fields[name].annotation, None) for name in EDITABLE_FIELDS})


def public_metadata(values):
    result = dict(values)
    for field in RANGES:
        if field in result:
            result[field] = years(result[field])
    result['processed_steps'] = step_flags(result.get('processed_steps'))
    return result


def consistent_patch(current, patch):
    """The legacy authors field is a projection of contributor roles when supplied."""
    patch = dict(patch)
    basis = {**(current.get('field_basis') or {}), **patch.get('field_basis', {})}
    if 'contributors' in patch:
        if not patch['contributors'] and patch.get('authors'):
            patch['contributors'] = [{'name': name, 'role': 'author', 'entity_id': None}
                                     for name in patch['authors']]
        authors = [p['name'] for p in patch['contributors'] if p['role'] == 'author'] or None
        if 'authors' in patch and patch['authors'] != authors:
            raise ValueError('authors 与 contributors 中的 author 角色不一致')
        patch['authors'] = authors
        if 'contributors' in basis:
            basis['authors'] = basis['contributors']
    elif 'authors' in patch:
        known = {p['name']:p for p in current.get('contributors', []) if p['role'] == 'author'}
        patch['contributors'] = [p for p in current.get('contributors', []) if p['role'] != 'author'] + [
            known.get(name, {'name': name, 'role': 'author', 'entity_id': None}) for name in patch['authors'] or []]
        if 'authors' in basis:
            basis['contributors'] = basis['authors']
    patch['field_basis'] = basis
    claims = dict(patch.get('field_claims', current.get('field_claims', {})))
    for field, value in patch.items():
        if field not in ('field_claims', 'field_basis', 'processed_steps') and value != current.get(field):
            if (field not in patch.get('field_claims', {}) or
                    patch['field_claims'][field] == current.get('field_claims', {}).get(field)):
                claims.pop(field, None)
    patch['field_claims'] = claims
    Metadata.model_validate({**{k: current[k] for k in VERSION_FIELDS if k in current}, **patch})
    return patch


def database_patch(patch):
    """Validate just the supplied fields; never fill missing fields in a patch."""
    return {k: YearRange(**v).database() if k in RANGES and v is not None else v
            for k, v in Metadata.model_validate(patch).model_dump(exclude_unset=True).items()}


def version_sql(meta):
    expressions = []
    for key in VERSION_FIELDS:
        item = f"{meta}->'{key}'"
        if key in RANGES:
            expressions.append(f"CASE WHEN jsonb_typeof({item})='object' THEN int4range("
                               f"({item}->>'start_year')::int,({item}->>'end_year')::int,'[]')"
                               f" WHEN jsonb_typeof({item})='array' THEN int4range("
                               f"({item}->>0)::int,({item}->>1)::int,'[]') END")
        elif key in ARRAYS:
            expr = f"CASE WHEN jsonb_typeof({item})='array' THEN ARRAY(SELECT jsonb_array_elements_text({item})) END"
            expressions.append(f"coalesce({expr},'{{}}'::text[])" if key == 'quality_flags' else expr)
        elif key in JSON_FIELDS:
            default = '[]' if key in ('contributors', 'tags') else '{}'
            expressions.append(f"coalesce(nullif({item},'null'::jsonb),'{default}'::jsonb)")
        elif key == 'quality_status':
            expressions.append(f"coalesce({meta}->>'{key}','ok')")
        else:
            expressions.append(f"{meta}->>'{key}'")
    return ', '.join(expressions)
