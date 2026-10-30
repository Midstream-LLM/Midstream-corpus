# midstream-core

古汉语语料管理系统，用于大模型预训练语料的入库、版本管理、查询、查重和可复现导出。

## 核心能力与当前状态

- 来源适配器：已支持 `ccc_corpus` 和 `wanli`，按批次解析、暂存、验证和发布。
- 版本管理：保存原始版和清洗版；重新处理生成新版本，保留历史正文和元数据。
- 查询：按来源、标题前缀、作者、体裁、分类和成书年份筛选，读取当前或指定版本。
- 查重：文档级精确重复和近重复检测；段落级寻找归一化后逐字相同的长片段，记录原文字符偏移。入库发布后依次自动运行。
- 数据集：按配方固定成员、顺序和划分，按作品和重复簇分组导出 Parquet，校验正文并登记训练使用。

尚未实现人工管理、批次管理命令、全文检索、API 和前端。

## 快速开始

需要 Python 3.12 和 Rust 工具链（含 Cargo）。Python 主包要求 `>=3.12`，当前 Rust 扩展要求 `>=3.12,<3.13`，因此完整安装使用 Python 3.12。以下命令在 Linux 上、仓库根目录执行，无需配置系统 PostgreSQL。

首次 `midstream init` 会从 PostgreSQL 官网下载并编译 PostgreSQL 18.6，同时构建静态 LZ4 1.10.0 和 pg_bigm `v1.2-20250903`，约需几分钟。需要 gcc、GNU make、curl、tar/gzip、常用 GNU 工具（含 `sha256sum`、`nproc`）；[PostgreSQL 18 官方构建要求](https://www.postgresql.org/docs/18/install-requirements.html)还包括 Flex、Bison 和 Perl。维护者需先填写 [构建脚本](scripts/build-postgres.sh) 中 LZ4 和 pg_bigm 的 SHA256 占位值。

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -e .
pip install ./rust

midstream init

# 需自行准备输入文件；替换为实际路径
midstream ingest wanli --path /path/to/wanli_shilu_corpus.jsonl
midstream search --source wanli --limit 10
midstream stats
```

二进制、数据库、socket、日志和导出默认都在 `MIDSTREAM_HOME`（`~/.local/share/midstream`）下的 `pg/`、`pgdata/`、`run/`、`log/`、`exports/`。实例只监听权限为 `0700` 的 Unix socket；普通数据库命令会自动启动已初始化的实例。用 `midstream server start|stop|status` 管理，重复运行 `midstream init` 会启动实例并迁移到最新版。输入文件必须通过 `--path` 或 `MIDSTREAM_JISHUI_RAW` 指定。

备份使用私有安装的 `pg/bin/pg_dump`，示例见[使用指南](docs/user-guide.md#实例管理与备份)。当前没有内置备份命令。

也可使用已建好的外部 PostgreSQL（需支持 LZ4）；设置 `MIDSTREAM_DSN` 后 CLI 不会启动私有实例，迁移另设指向同一库的 `MIDSTREAM_ALEMBIC_URL`：

```bash
export MIDSTREAM_DSN="dbname=midstream host=/var/run/postgresql"
export MIDSTREAM_ALEMBIC_URL="postgresql+psycopg:///midstream?host=/var/run/postgresql"
alembic upgrade head
midstream stats
```

查重也可单独运行，两个命令各自替换对应的最新快照：

```bash
midstream dedup          # 文档级
midstream dedup --spans  # 段落级 span-v1，最短 64 个归一化字符
```

段落级使用 k=32、w=32 的 Winnowing，候选逐字验证，忽略出现超过 1,000 次的模板指纹。指纹和种子按 256 区落盘，正文按需解压；临时目录使用 Python 的系统临时路径（可通过 `TMPDIR` 指向空间充足的磁盘），结束后删除。服务器 20 线程可设 `RAYON_NUM_THREADS=20`。已有部署需先执行 `midstream init`（外部实例用 `alembic upgrade head`）并重新安装 Rust 扩展。

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
| `src/midstream/server.py` | 私有 PostgreSQL 初始化、调优和实例管理 |
| `src/midstream/adapters/` | 来源适配器和解析记录接口 |
| `src/midstream/ingest.py` | 批次登记、暂存、验证和发布 |
| `src/midstream/clean.py`、`eras.py` | 技术清洗、质量判定和朝代年份映射 |
| `src/midstream/textcodec.py` | 正文哈希与 zstd 压缩 |
| `src/midstream/query.py` | 元数据查询、正文读取和统计 |
| `src/midstream/reprocess.py`、`rules.py` | 重新处理流程和规则注册 |
| `src/midstream/dedup.py` | 指纹缓存和查重快照 |
| `rust/` | PyO3 查重扩展：归一化、OPH、LSH、Jaccard 验证和 Winnowing 片段匹配 |
| `src/midstream/export.py` | 数据集成员、划分、Parquet 导出和校验 |
| `recipes/` | 导出配方 |
| `migrations/` | Alembic 迁移，DDL 在 `versions/*.sql` |
| `tests/` | 单元测试和数据库端到端测试 |
| `scripts/` | PostgreSQL 构建和只读分析脚本 |

## 许可证

项目代码采用 [GPL-3.0](LICENSE)（`GPL-3.0-only`）。`rust/data/` 下的 OpenCC 数据采用 Apache-2.0，见 [rust/data/LICENSE-OpenCC](rust/data/LICENSE-OpenCC)。

构建脚本使用的 [PostgreSQL](https://www.postgresql.org/about/licence/) 和 [pg_bigm](https://github.com/pgbigm/pg_bigm/tree/v1.2-20250903) 采用 PostgreSQL License，[LZ4 库](https://github.com/lz4/lz4/blob/v1.10.0/lib/LICENSE)采用 BSD-2-Clause，均与 GPL-3.0 兼容。
