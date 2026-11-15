# 中流语料库 Midstream Corpus

为击水语言模型（Jishui）预训练建立的语料管理与存储系统。对于新数据，入库时进行整理与校验，入库后系统支持按标题、作者、年代、体裁和来源检索，并可按需导出数据集供训练与评测使用。

**项目开发中。** 

## 核心能力

- 版本管理：对于同一部作品，保存包括原始版和清洗版的多个版本；对语料进行任何处理都生成新版本，并保留历史正文和元数据。
- 查询：按来源、标题前缀、作者、体裁、分类和成书年份筛选，读取当前或指定版本。
- 查重：包括文档级查重与段落级查重，入库发布后依次自动运行。
- 数据集：多条件选材、只读试算、按字符数/篇数整组划分和保留验证集；固定发布后可多次导出 Parquet、JSONL、PKL 或切分样本，附 manifest。见 [使用指南](docs/user-guide.md#8-配方导出校验与训练登记)。
- Token：本地 JSON 分词器、正文计数缓存、固定发布的精确统计与试算估算，以及 NPY 分片任务。统计/编码不进入入库和查重流程，见 [使用指南](docs/user-guide.md#8a-token-统计与分片)。
- 工作台：数据集四步向导、两层入库预览、显式确认发布、重复浏览和原文高亮；人工否决按文档对持久化，查重重算和导出都遵守。
- 繁转简：固定字符表第一候选，追加非当前的 simplified 派生版；配方显式选择，缺失或过期不回退。
- 备份：默认不自动运行；设置/配置文件共用 OSS 策略，流式 pg_dump 上传、目录校验与只恢复到新库，见 [使用指南](docs/user-guide.md#8d-oss-备份m10)。
- 人工管理：对当前版本 ID 审核质量、修正元数据、追加版本，并且可通过人工确认作品间是否存在关联。
- 批次管理：每次入库登记为一批，可查看每次入库的进度与失败记录；中途失败的批次支持断点续跑，不需要的批次可以放弃并回收空间。
- LLM 服务：独立提供质检、自定义 prompt 生成及批量调用，统一管理接入点、凭据引用和模型配置；支持 DeepSeek 的 Chat Completions、Responses、Anthropic Messages 和 Beta FIM 生成接口。见 [LLM 使用说明](docs/llm.md)。

全文检索、SFT 派生样本和 macOS 打包将在未来实现。

## Web app

Web app 支持文档查看、年代筛选与统计、人工审核、批次管理、正文阅读和历史版本切换。React 客户端与 FastAPI 后端分离。运行方式见 [Web app 说明](docs/webapp.md)。

## 快速开始

源码：[GitHub](https://github.com/Midstream-LLM/Midstream-corpus) · [OSRed](https://osredm.com/Midstream-AI/Midstream-corpus)。任选一个地址克隆：

```bash
git clone https://github.com/Midstream-LLM/Midstream-corpus.git
cd Midstream-corpus
```

需要 Python 3.12 和 Rust 工具链（含 Cargo）。Python 主包要求 `>=3.12`，当前 Rust 扩展要求 `>=3.12,<3.13`，因此完整安装使用 Python 3.12。以下命令在 Linux 上、仓库根目录执行，无需配置系统 PostgreSQL。

首次 `midstream init` 会下载并编译 PostgreSQL 18.6、静态 LZ4 1.10.0 和 pg_bigm `v1.2-20250903`。需要 Linux 构建环境和网络；构建脚本已固定依赖校验值，并在缺少 m4、Bison、Flex 时从源码构建，详见[安装说明](docs/user-guide.md#安装)。已有外部 PostgreSQL 17 可继续使用。

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

CLI 常用入口：`midstream --help`、`midstream <命令> --help`。入库、批次恢复、重新处理、查重、导出、token 统计/分片和备份等长操作默认在当前进程就地执行，同样写入任务表、留下进度和审计；加 `--queue` 只提交到任务队列、不等待。`midstream operations` 列出所有可执行操作及参数 JSON Schema，`midstream run <操作> --params '<json>' [--queue]` 按名称执行，供脚本和 LLM 使用。完整命令流程见[使用指南](docs/user-guide.md#3a-cli任务与常用流程)。OSS 默认不自动备份，见[备份说明](docs/user-guide.md#8d-oss-备份m10)。

也可使用已建好的外部 PostgreSQL（需支持 LZ4）；设置 `MIDSTREAM_DSN` 后 CLI 不会启动私有实例，迁移另设指向同一库的 `MIDSTREAM_ALEMBIC_URL`：

```bash
export MIDSTREAM_DSN="dbname=midstream host=/var/run/postgresql"
export MIDSTREAM_ALEMBIC_URL="postgresql+psycopg:///midstream?host=/var/run/postgresql"
alembic upgrade head
midstream stats
```

### 启动后端、Web 与 worker

数据库初始化或迁移完成后，在仓库根目录构建前端（需要 Node.js/npm）：

```bash
source .venv/bin/activate
npm --prefix web ci
npm --prefix web run build
midstream serve --host 127.0.0.1 --port 8765
```

`midstream serve` 确保内置 PostgreSQL 运行，以子进程启动 worker，并在前台运行 API；Ctrl-C 退出时一并结束 worker。API 同时托管 `web/dist`，打开 `http://127.0.0.1:8765`；接口文档为 `/api/docs`。

worker 同一时间只执行一个任务，同库只能有一个 worker。`--no-worker` 只运行 API（与 `python -m midstream.api` 相同），此时需另开终端用相同的虚拟环境和配置运行 `midstream worker`，否则任务保持排队。默认不安装系统服务，需要常驻时可用 systemd 用户服务，示例见 [Web 指南](docs/webapp.md#常驻运行systemd-用户服务)。远程访问用 SSH 隧道；开发时用 Vite 热更新，完整步骤见 [Web 启动与操作指南](docs/webapp.md#启动后端与-web)。

## 查重

入库发布成功后，文档级和段落级查重作为后续任务依次运行（就地执行时在同一进程接着跑完，worker 执行时排入队列），也可以单独运行：

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
| `src/midstream/cli.py`、`api/` | 命令行入口；HTTP API（按领域分路由，`python -m midstream.api` 只运行 API） |
| `src/midstream/operations.py`、`jobs.py` | 长操作注册表；任务队列、worker 与就地执行共用的执行路径 |
| `src/midstream/config.py` | 环境变量配置 |
| `src/midstream/store/` | 正文哈希与 zstd 压缩（`textcodec`），只读第二连接（`reader`） |
| `src/midstream/corpus/` | 元数据查询与统计（`query`）、人工审核审计（`management`）、追加版本（`versions`）、朝代年份映射（`eras`） |
| `src/midstream/ingest/` | 批次登记、暂存、验证和发布（`pipeline`），批次恢复/放弃（`batches`），导入识别与登记（`importing`、`detection`、`discovery`、`sampling`），来源适配器（`adapters/`） |
| `src/midstream/curation/` | 技术清洗与质量判定（`clean`）、重新处理流程和规则注册（`reprocess`、`rules`）、繁转简（`variants`） |
| `src/midstream/dedup/` | 指纹缓存和查重快照（`engine`），重复簇浏览与否决（`duplicates`） |
| `rust/` | PyO3 查重扩展：归一化、OPH、LSH、Jaccard 验证和 Winnowing 片段匹配 |
| `src/midstream/datasets/` | 配方、候选与选用、分组划分、试算、固定发布、配方草稿和段落裁剪 |
| `src/midstream/delivery/` | 文本格式（`formats`）、样本切分（`samples`）、分片导出与校验（`export`）、token 分片（`tokens`）、分词器接口与 token 统计（`tokenizing`、`token_stats`） |
| `src/midstream/ops/` | 私有 PostgreSQL 管理（`server`）、OSS 备份与配置（`backup`、`backup_settings`） |
| `src/midstream/llm/` | LLM 用途服务、统一接入配置和协议请求执行 |
| `recipes/` | 导出配方 |
| `migrations/` | Alembic 迁移，DDL 在 `versions/*.sql` |
| `tests/` | 单元测试和数据库端到端测试 |
| `scripts/` | PostgreSQL 构建和只读分析脚本 |

## 许可证

项目代码采用 [GPL-3.0](LICENSE)。`rust/data/` 下的 OpenCC 数据采用 Apache-2.0，见 [rust/data/LICENSE-OpenCC](rust/data/LICENSE-OpenCC)。

构建脚本使用的 [PostgreSQL](https://www.postgresql.org/about/licence/) 和 [pg_bigm](https://github.com/pgbigm/pg_bigm/tree/v1.2-20250903) 采用 PostgreSQL License，[LZ4 库](https://github.com/lz4/lz4/blob/v1.10.0/lib/LICENSE)采用 BSD-2-Clause，均与 GPL-3.0 兼容。