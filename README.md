# midstream-core

古汉语语料管理系统，用于大模型预训练语料的入库、版本管理、查询、查重和可复现导出。

## 核心能力与当前状态

- 来源适配器：已支持 `ccc_corpus` 和 `wanli`，按批次解析、暂存、验证和发布。
- 版本管理：保存原始版和清洗版；重新处理生成新版本，保留历史正文和元数据。
- 查询：按来源、标题前缀、作者、体裁、分类和成书年份筛选，读取当前或指定版本。
- 文档级查重：归一化后的精确重复和近重复检测，入库发布后自动运行。
- 数据集：按配方固定成员、顺序和划分，按作品和重复簇分组导出 Parquet，校验正文并登记训练使用。

尚未实现段落级查重、人工管理、批次管理命令、全文检索、API 和前端。

## 快速开始

需要 PostgreSQL 17（支持 LZ4 压缩）、Python 3.12 和 Rust 工具链（含 Cargo）。Python 主包要求 `>=3.12`，当前 Rust 扩展要求 `>=3.12,<3.13`，因此完整安装使用 Python 3.12。以下命令在仓库根目录执行，假设 PostgreSQL 已启动，当前数据库角色可建库。

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -e .
pip install ./rust

createdb -T template0 -E UTF8 --locale-provider=builtin --builtin-locale=C.UTF-8 --locale=C.UTF-8 midstream
export MIDSTREAM_DSN="dbname=midstream"
export MIDSTREAM_ALEMBIC_URL="postgresql+psycopg:///midstream"
export MIDSTREAM_JISHUI_RAW="/path/to/Jishui/dataset/raw"
export MIDSTREAM_EXPORT_ROOT="/path/to/exports"
alembic upgrade head

# 需自行准备输入文件；替换为实际路径
midstream ingest wanli --path /path/to/wanli_shilu_corpus.jsonl
midstream search --source wanli --limit 10
midstream stats
```

安装、配置、查询、重新处理、查重、导出和添加来源，见[使用指南](docs/user-guide.md)。

## 测试

```bash
PYO3_PYTHON="$PWD/.venv/bin/python" \
LD_LIBRARY_PATH="$(.venv/bin/python -c 'import sysconfig; print(sysconfig.get_config_var("LIBDIR"))')" \
cargo test --manifest-path rust/Cargo.toml
.venv/bin/python -m pytest -q
MIDSTREAM_TEST_DSN="dbname=midstream_test" .venv/bin/python -m pytest
```

Rust 测试显式指定 `PYO3_PYTHON` 为 `.venv/bin/python`，避免链接到错误的系统 Python。设置 `MIDSTREAM_TEST_DSN` 后才会运行端到端测试；测试会清空该库的 `public` schema，只能指向专用测试库。

## 代码结构

| 路径 | 职责 |
|---|---|
| `src/midstream/cli.py` | 命令行入口 |
| `src/midstream/config.py` | 环境变量配置 |
| `src/midstream/adapters/` | 来源适配器和解析记录接口 |
| `src/midstream/ingest.py` | 批次登记、暂存、验证和发布 |
| `src/midstream/clean.py`、`eras.py` | 技术清洗、质量判定和朝代年份映射 |
| `src/midstream/textcodec.py` | 正文哈希与 zstd 压缩 |
| `src/midstream/query.py` | 元数据查询、正文读取和统计 |
| `src/midstream/reprocess.py`、`rules.py` | 重新处理流程和规则注册 |
| `src/midstream/dedup.py` | 指纹缓存和查重快照 |
| `rust/` | PyO3 查重扩展：归一化、OPH、LSH 和 Jaccard 验证 |
| `src/midstream/export.py` | 数据集成员、划分、Parquet 导出和校验 |
| `recipes/` | 导出配方 |
| `migrations/` | Alembic 迁移，DDL 在 `versions/*.sql` |
| `tests/` | 单元测试和数据库端到端测试 |
| `scripts/` | 只读分析脚本 |

## 许可证

项目代码采用 [GPL-3.0](LICENSE)（`GPL-3.0-only`）。`rust/data/` 下的 OpenCC 数据采用 Apache-2.0，见 [rust/data/LICENSE-OpenCC](rust/data/LICENSE-OpenCC)。
