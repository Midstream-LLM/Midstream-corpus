"""正文模板：导出时把标题、作者、时代等拼进正文，写法固定、可复现。

语法：
- `{字段}` 替换为该字段的值；作者多人用“、”连接。
- `[...]` 是可选段：其中任一字段为空时整段省略，未知不写成“未知”。
- `{text}` 必须恰好出现一次，且不能在可选段里。
- `ratio` 是套用模板的文档比例，按 `seed` 和 doc_id 的哈希固定选取，同一配置每次导出结果相同。

示例：`[《{title}》][{authors}]\\n{text}`、`[【{era}·{genre}】]{text}`。
"""
import hashlib
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

FIELDS = ('title', 'authors', 'era', 'category', 'genre', 'tune_pattern', 'source', 'text')
_PLACEHOLDER = re.compile(r'\{(\w+)\}')
_PART = re.compile(r'\[([^\[\]]*)\]|([^\[\]]+)')


class Template(BaseModel):
    model_config = ConfigDict(extra='forbid')
    format: str = Field(min_length=1, max_length=500)
    ratio: float = Field(default=1.0, ge=0, le=1)
    seed: int = 0

    @model_validator(mode='after')
    def valid(self):
        if ''.join(m.group(0) for m in _PART.finditer(self.format)) != self.format:
            raise ValueError('可选段的方括号不能嵌套或不成对')
        names = _PLACEHOLDER.findall(self.format)
        if unknown := set(names) - set(FIELDS):
            raise ValueError(f'模板只能使用这些字段：{"、".join(FIELDS)}；未知字段：{"、".join(sorted(unknown))}')
        optional = [n for m in _PART.finditer(self.format) if m.group(1) is not None
                    for n in _PLACEHOLDER.findall(m.group(1))]
        if names.count('text') != 1 or 'text' in optional:
            raise ValueError('{text} 必须恰好出现一次，且不能放在可选段里')
        return self

    def applies(self, doc_id):
        if self.ratio >= 1:
            return True
        digest = hashlib.sha256(f'{self.seed}:{doc_id}'.encode()).digest()
        return int.from_bytes(digest[:8], 'big') < self.ratio * 2 ** 64

    def render(self, document):
        values = {}
        for name in FIELDS:
            value = document.get(name)
            if isinstance(value, list):
                value = '、'.join(v for v in value if v)
            values[name] = value.strip() if isinstance(value, str) and name != 'text' else value
        out = []
        for part in _PART.finditer(self.format):
            chunk, optional = (part.group(1), True) if part.group(1) is not None else (part.group(2), False)
            needed = _PLACEHOLDER.findall(chunk)
            if optional and any(not values.get(n) for n in needed):
                continue
            out.append(_PLACEHOLDER.sub(lambda m: str(values.get(m.group(1)) or ''), chunk))
        return ''.join(out)
