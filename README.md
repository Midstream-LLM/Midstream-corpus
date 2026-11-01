# 中流语料库 Midstream Corpus

为击水语言模型（Jishui）预训练建立的语料管理与存储系统。新数据经整理与校验后入库，入库后系统支持按标题、作者、年代、体裁和来源检索，并可按需导出数据集供训练与评测使用。

**项目开发中。** 批次入库、元数据查询、重新处理、查重和数据集导出的主干已打通，API 和前端尚未完成。

## 核心能力

- 版本管理：对于同一部作品，保存包括原始版和清洗版的多个版本；对语料进行任何处理都生成新版本，并保留历史正文和元数据。
- 查询：按来源、标题前缀、作者、体裁、分类和成书年份筛选，读取当前或指定版本。
- 查重：包括文档级查重与段落级查重，入库发布后依次自动运行。
- 数据集：多条件选材、只读试算、按字符数/篇数整组划分和保留验证集；固定发布后可多次导出 Parquet、JSONL、PKL 或切分样本，附 manifest。见 [使用指南](docs/user-guide.md#8-配方导出校验与训练登记)。
- 人工管理：对当前版本 ID 审核质量、修正元数据、追加版本，并且可通过人工确认作品间是否存在关联。
- 批次管理：每次入库登记为一批，可查看每次入库的进度与失败记录；中途失败的批次支持断点续跑，不需要的批次可以放弃并回收空间。
- LLM 服务：独立提供质检、自定义 prompt 生成及批量调用，统一管理接入点、凭据引用和模型配置；支持 DeepSeek 的 Chat Completions、Responses、Anthropic Messages 和 Beta FIM 生成接口。见 [LLM 使用说明](docs/llm.md)。

实现中的功能包括全文检索、API 和前端界面等。

## Web app

Web app 支持元数据优先的文档查看、年代筛选与统计、人工审核、批次管理、正文阅读和历史版本切换。React 客户端与 FastAPI 后端分离，未来 SwiftUI 可复用 HTTP API。运行方式见 [Web app 说明](docs/webapp.md)。

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

## 查重

入库发布后自动依次运行文档级和段落级查重，也可以单独运行。两级结果各自保存、互不覆盖：

```bash
midstream dedup          # 文档级：整篇相似度，连成重复簇
midstream dedup --spans  # 段落级：找共同片段，最短 64 个归一化字符
midstream dedup --full   # 忽略缓存，强制完整重算文档级查重
```

文档级判断整篇正文是否相近。正文归一化后取字符 8-gram 集合，用 128 桶 OPH 签名与 32×4 LSH 生成候选，签名相似度不足 0.6 的候选排除，其余计算 Jaccard 相似度，达到 0.8 即判为近重复并合并为簇。整篇内容高度相似时，这一级最有效；著作被拆分为章节后，整篇相似度往往低于阈值，需要段落级补充。

段落级查找跨文献成段复用的正文。每篇代表版本以 k=32、w=32 的 Winnowing 滑窗提取指纹，指纹相同的候选再逐字核对，片段最短为 64 个归一化字符。目录、版权页一类内容重复出现，会形成大量相同指纹，出现超过 1,000 次的不参与匹配以控制候选规模。结果保存在 dup_span 中，区间为清洗正文的 Unicode 字符偏移，出处记为 version_id 较小的一侧。数据集导出目前只使用文档级簇。

当前版本集合与参数都没有变化时，命令复用上次的结果，跳过计算。文档级另缓存逐对 Jaccard 分数，修改元数据或更换代表版本仍可复用；输入变化时，LSH 与连通分组需要重建。--full 忽略这些缓存，强制完整重算。

中间指纹与种子按 256 个区写入系统临时目录，计算结束后删除。解压与归一化由 Rayon 多线程完成，线程数由 RAYON_NUM_THREADS 设定。升级后须执行 midstream init（外部实例用 alembic upgrade head），并重新安装 Rust 扩展。


更多安装与使用上的文档，见[使用指南](docs/user-guide.md)。

## 代码结构

| 路径 | 职责 |
|---|---|
| `src/midstream/cli.py` | 命令行入口 |
| `src/midstream/config.py` | 环境变量配置 |
| `src/midstream/server.py` | 私有 PostgreSQL 初始化、调优和实例管理 |
| `src/midstream/adapters/` | 来源适配器和解析记录接口 |
| `src/midstream/ingest.py` | 批次登记、暂存、验证和发布 |
| `src/midstream/batches.py`、`management.py` | 批次恢复、放弃与人工审核审计 |
| `src/midstream/llm/` | LLM 用途服务、统一接入配置和协议请求执行 |
| `src/midstream/clean.py`、`eras.py` | 技术清洗、质量判定和朝代年份映射 |
| `src/midstream/textcodec.py` | 正文哈希与 zstd 压缩 |
| `src/midstream/query.py` | 元数据查询、正文读取和统计 |
| `src/midstream/reprocess.py`、`rules.py` | 重新处理流程和规则注册 |
| `src/midstream/dedup.py` | 指纹缓存和查重快照 |
| `rust/` | PyO3 查重扩展：归一化、OPH、LSH、Jaccard 验证和 Winnowing 片段匹配 |
| `src/midstream/export.py`、`exporters.py` | 选用试算、固定成员、划分、多格式分片与产物校验 |
| `recipes/` | 导出配方 |
| `migrations/` | Alembic 迁移，DDL 在 `versions/*.sql` |
| `tests/` | 单元测试和数据库端到端测试 |
| `scripts/` | PostgreSQL 构建和只读分析脚本 |

## 许可证

项目代码采用 [GPL-3.0](LICENSE)。`rust/data/` 下的 OpenCC 数据采用 Apache-2.0，见 [rust/data/LICENSE-OpenCC](rust/data/LICENSE-OpenCC)。

构建脚本使用的 [PostgreSQL](https://www.postgresql.org/about/licence/) 和 [pg_bigm](https://github.com/pgbigm/pg_bigm/tree/v1.2-20250903) 采用 PostgreSQL License，[LZ4 库](https://github.com/lz4/lz4/blob/v1.10.0/lib/LICENSE)采用 BSD-2-Clause，均与 GPL-3.0 兼容。

Web 批次管理支持新建入库：选择来源适配器、预览后端本地文件、确认登记，再继续处理与发布。使用范围与路径要求见 [Web 说明](docs/webapp.md)。

入库支持已有来源规则识别，以及复用 LLM 服务分析未知格式的字段映射建议；确认后的 TXT、JSON/JSONL、Parquet 和行文本映射随批次保存。需升级迁移 `0008`，覆盖范围和限制见 [Web 说明](docs/webapp.md#格式识别与智能映射)。

Web 侧栏提供 LLM 配置页面，可管理协议接入点、API 凭据、模型及用途默认值，与 CLI 共用后端配置；密钥不回显，支持环境变量或后端本地保存。

LLM 配置页提供 DeepSeek 快速预设和官方模型列表刷新，可自动填写协议地址、模型及基础参数。

Qwen 快速配置支持阿里云百炼三个区域的工作空间地址自动填写、模型目录刷新及共享凭据管理。
