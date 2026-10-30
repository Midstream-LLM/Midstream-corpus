# midstream-core

中流语料库的 core：Python 库、CLI 和数据库迁移。运行在 PostgreSQL 17 上。

设计和进度见上层 Project 的 `TASK.md` 与 `docs/`。

## 安装

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt -e .
.venv/bin/alembic upgrade head
```

建库时固定编码和排序规则：

```bash
createdb -T template0 -E UTF8 --locale-provider=builtin --builtin-locale=C.UTF-8 --locale=C.UTF-8 midstream
```

## 使用

```bash
midstream ingest wanli                     # 入库一个来源
midstream search --title 北齐书             # 查询当前版本
midstream search --year-from 800 --year-to 850 --include-unknown-years
midstream show 18418 --version 1           # 读取指定版本
midstream export recipes/small-sources-v1.json
midstream verify 1                         # 校验数据集
midstream record-training small-sources-v1 run-name
midstream stats
```

## 代码结构

| 路径 | 职责 |
|---|---|
| `src/midstream/adapters/` | 来源适配器，每个来源一个 |
| `src/midstream/ingest.py` | 批次流程：登记、暂存、验证、发布 |
| `src/midstream/clean.py` | 清洗规则与质量判定 |
| `src/midstream/eras.py` | 朝代到年份区间的规则映射 |
| `src/midstream/textcodec.py` | 正文哈希与 zstd 压缩 |
| `src/midstream/query.py` | 元数据查询和正文读取 |
| `src/midstream/export.py` | 数据集成员、划分、导出和校验 |
| `migrations/` | Alembic 迁移，DDL 在 `versions/*.sql` |
| `scripts/` | 一次性分析脚本，只读 |

## 添加来源

1. 在 `adapters/` 下实现 `name`、`parser_version`、`description`、`license_note`、`default_paths()` 和 `parse()`。
2. `parse()` 逐条产出 `ParsedRecord`。解析失败的记录也要产出，并填写 `error`。
3. 在 `adapters/__init__.py` 注册。

未知的 metadata 留空，不要用推测值补齐。

## 测试

```bash
.venv/bin/python -m pytest
MIDSTREAM_TEST_DSN="dbname=midstream_test" .venv/bin/python -m pytest
```

设置 `MIDSTREAM_TEST_DSN` 后才会运行端到端测试。测试会清空该库的 `public` schema，只能指向专用测试库。

## 配置

| 环境变量 | 默认值 |
|---|---|
| `MIDSTREAM_DSN` | `dbname=midstream` |
| `MIDSTREAM_ALEMBIC_URL` | `postgresql+psycopg:///midstream` |
| `MIDSTREAM_JISHUI_RAW` | 服务器 HDD 上的 `Jishui/dataset/raw` |
| `MIDSTREAM_EXPORT_ROOT` | 服务器 HDD 上的 `midstream/exports` |
| `MIDSTREAM_WORKERS` | `8` |
| `MIDSTREAM_CHUNK_DOCS` | `2000` |
