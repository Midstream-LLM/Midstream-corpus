# midstream-core 使用指南

本指南对应仓库当前的 Python CLI、数据库迁移和 Rust 查重扩展。命令在仓库根目录执行，并使用已激活的虚拟环境。示例中的 `/path/to/…` 均需替换为部署环境的实际路径。

独立 LLM 模块的接入配置、质检、自定义生成、批量与流式命令，见 [LLM 服务使用说明](llm.md)。`midstream llm …` 无需启动 PostgreSQL。

## 1. 理解数据对象

| 对象 | 含义 |
|---|---|
| 文档 | 来源内的一条稳定记录，由 `(source_id, source_key)` 唯一确定，使用 `doc_id` 标识。不同来源的相同正文仍是不同文档。 |
| 版本 | 一篇文档的正文和完整元数据快照。`version_no` 是文档内的版本号；`version_id` 是库内的版本标识。 |
| 原始版 | 适配器解析并解码后、技术清洗前的正文，即 `decoded`。它不等于原始文件的字节副本。 |
| 清洗版 | 从原始版生成的 `cleaned` 版本，包含技术清洗、质量判定和可用的朝代年份映射。 |
| 当前版本 | `is_current=true` 的版本。一篇文档最多一个；查询和导出筛选以它为准。 |
| 批次 | 一次来源入库，记录解析器版本、输入文件路径与 SHA-256、记录状态和处理运行。 |
| 作品 | 文献身份。一部作品可以关联多篇文档；当前入库流程按适配器给出的 `work_title` 建立作品及关联。 |
| 重复簇 | 一次查重产生的一组具体版本。目前只有文档级重复簇；仅保存最新快照。 |
| 重复片段 | 段落级查重记录的原文字符区间及出处，保存在 `dup_span`，仅保存最新快照。 |
| 数据集 | 一次命名的发布，固定到具体 `version_id`，保存成员、顺序、train/val/test 划分、配方和内容摘要。 |

首次发布一篇文档时生成版本 1（原始版）和版本 2（清洗版），清洗版成为当前版本。即使两版正文相同，也会保留两个版本。已发布版本除 `is_current` 外只增不改；正文或元数据的修订通过新版本表达。正文按 UTF-8 SHA-256 存储并使用 zstd 压缩，相同正文共用存储。

数据集发布后，成员、顺序和划分不再变化。之后入库、重新处理或查重不会改变旧数据集。重复簇快照和指纹缓存是派生数据，不具备这种不可变性。

未知元数据留空。已收录的朝代标签可由规则映射为成书年份区间，依据写入 `field_basis`；没有对应规则就保留未知。`wanli` 的事件日期保存在 `extra.event_date`，不作为成书年份。

## 2. 安装与建库

需要：

- Python 3.12。主包允许 `>=3.12`，当前 Rust 扩展只允许 `>=3.12,<3.13`。
- Rust 工具链，含 Cargo；`pip install ./rust` 通过 maturin 构建扩展。
- Linux 构建环境：gcc、GNU make、curl、tar/gzip 和常用 GNU 工具（含 `sha256sum`、`nproc`）。[PostgreSQL 18 官方要求](https://www.postgresql.org/docs/18/install-requirements.html)还包括 Flex、Bison 和 Perl。

无需配置系统 PostgreSQL。首次 `midstream init` 会从官网下载并编译 PostgreSQL 18.6，从 GitHub 下载并构建静态 LZ4 1.10.0 和 pg_bigm `v1.2-20250903`，约需几分钟。PostgreSQL 使用同目录官方 `.sha256` 文件校验；LZ4 和 pg_bigm 使用脚本内固定的 SHA256，当前占位值需由维护者在发布前填写。脚本是 [scripts/build-postgres.sh](../scripts/build-postgres.sh)，也可用 `bash scripts/build-postgres.sh /path/to/install` 单独构建。

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt -e .
pip install ./rust

midstream init
midstream stats
```

`init` 构建二进制、以 builtin `C.UTF-8` 和 UTF-8 初始化实例，创建 `midstream` 库并通过 Alembic 升级到 head。已初始化时只启动实例并运行迁移；不导入语料。后续可在任意工作目录使用 `midstream init`。输入语料需自行准备，通过 `--path` 或 `MIDSTREAM_JISHUI_RAW` 指定；入库流程读取输入文件，不改写它们。

### 实例管理与备份

所有持久数据默认位于 `MIDSTREAM_HOME`（`~/.local/share/midstream`）。可在首次初始化前设置该变量，例如 `export MIDSTREAM_HOME=/path/to/midstream`。

| 子目录 | 内容 |
|---|---|
| `pg/` | 私有 PostgreSQL 安装，含 `bin/pg_ctl`、`bin/pg_dump` 等工具和 pg_bigm 扩展文件 |
| `pgdata/` | 数据库文件；调优参数写到 `conf.d/midstream.conf`，由 `postgresql.conf` 的 `include_dir` 加载 |
| `run/` | Unix socket，目录权限 `0700` |
| `log/` | 服务日志 `postgresql.log` |
| `exports/` | 默认数据集导出目录，可用 `MIDSTREAM_EXPORT_ROOT` 覆盖 |

实例不监听 TCP，通过私有 socket 使用 trust 认证。普通数据库命令会自动启动已初始化的实例；显式管理命令为：

```bash
midstream server start
midstream server status  # running 返回 0，stopped 返回 1
midstream server stop
```

首次初始化按物理内存设置 `shared_buffers` 为 20%（上限 8 GiB）、`effective_cache_size` 为 50%、`maintenance_work_mem` 为 10%（上限 2 GiB），向下取整到 MiB。`work_mem=64MB`、`max_wal_size=16GB`、`checkpoint_completion_target=0.9`；WAL 和 TOAST 用 LZ4，`io_method=worker`，单次查询和维护最多使用 4 个并行 worker，全局并行 worker 数和进程池按 CPU 数设置。重跑 `init` 保留现有配置。

备份使用私有安装的 `pg/bin/pg_dump`。当前 CLI 没有内置备份和恢复命令：

```bash
midstream server start
midstream_home="${MIDSTREAM_HOME:-$HOME/.local/share/midstream}"
"$midstream_home/pg/bin/pg_dump" -h "$midstream_home/run" -d midstream > midstream.sql
```

### 使用外部 PostgreSQL

外部实例需支持 LZ4，并自行准备同样使用 builtin `C.UTF-8` 的 UTF-8 数据库。已有 PostgreSQL 17 部署可继续使用。设置 `MIDSTREAM_DSN` 后，普通 CLI 命令直接连接外部库，不自动启动私有实例。迁移 URL 单独设置，须指向同一数据库：

```bash
createdb -T template0 -E UTF8 --locale-provider=builtin --builtin-locale=C.UTF-8 --locale=C.UTF-8 midstream
export MIDSTREAM_DSN="dbname=midstream"
export MIDSTREAM_ALEMBIC_URL="postgresql+psycopg:///midstream"
alembic upgrade head
midstream stats
```

外部实例迁移在仓库根目录执行；`midstream init` 和 `midstream server` 专用于私有实例。已有外部库的数据不会自动迁入私有实例。

## 3. 配置部署环境

配置通过进程环境变量读取。没有自动加载 `.env` 的逻辑。路径和并发参数在模块导入时读取，修改后需重新启动命令。

| 环境变量 | 默认值或行为 | 用途与部署设置 |
|---|---|---|
| `MIDSTREAM_HOME` | `~/.local/share/midstream` | 私有 PostgreSQL、数据库、socket、日志及默认导出目录的根目录。 |
| `MIDSTREAM_DSN` | `midstream` 库，host 为 `MIDSTREAM_HOME/run` | CLI 的 psycopg 连接串。显式设置后跳过私有实例自动启动，用于外部实例或测试。 |
| `MIDSTREAM_ALEMBIC_URL` | `midstream` 库，host 为 `MIDSTREAM_HOME/run` | Alembic 的 SQLAlchemy URL。与 DSN 分开配置；使用外部库时两者须指向同一库。 |
| `MIDSTREAM_JISHUI_RAW` | 无默认值 | 默认输入的根目录。未设置时必须显式传入 `ingest --path`。 |
| `MIDSTREAM_EXPORT_ROOT` | `MIDSTREAM_HOME/exports` | CLI 导出根目录，数据集写到其下的配方名称目录。 |
| `MIDSTREAM_WORKERS` | `8` | 入库解析、清洗、质量判定和压缩，以及重新处理压缩正文的 Python 工作进程数，使用正整数。 |
| `MIDSTREAM_CHUNK_DOCS` | `2000` | 发布压缩和其他 Python 流程的分块记录数，也用于查重验证批量大小；JSONL 解析按约 8 MiB 原始字节分块，使用正整数。 |
| `RAYON_NUM_THREADS` | Rayon 默认线程数 | Rust 查重扩展的线程数；与 `MIDSTREAM_WORKERS` 分开控制。 |
| `MIDSTREAM_TEST_DSN` | 未设置时跳过端到端测试 | 只用于测试。必须指向专用测试库，测试会清空其 `public` schema。 |

设置 `MIDSTREAM_JISHUI_RAW` 后，默认输入路径为：

| 来源 | 相对于 `MIDSTREAM_JISHUI_RAW` 的文件 |
|---|---|
| `ccc_corpus` | `chinese-classical-corpus/corpus.jsonl` |
| `wanli` | `wanli-dibao-corpus/wanli_shilu_corpus.jsonl` |

测试命令及构建测试时的 `PYO3_PYTHON`、`LD_LIBRARY_PATH` 设置见 [README](../README.md#测试)。

## 4. 入库新来源

当前已注册 `ccc_corpus` 和 `wanli`。只有设置 `MIDSTREAM_JISHUI_RAW` 后才能省略 `--path`，使用上表中的默认文件；`--path` 可重复，用于一次导入同一来源的多个文件。

```bash
midstream ingest wanli --path /path/to/wanli_shilu_corpus.jsonl
midstream ingest ccc_corpus --path /path/to/corpus.jsonl
midstream ingest wanli --path /path/to/part-1.jsonl --path /path/to/part-2.jsonl
```

JSONL 按约 8 MiB、完整行边界分块；同一个 spawn 进程池并行解析、清洗、判定质量和计算哈希，并在发布时压缩正文。主进程按文件及块的原始顺序 COPY，空行跳过。现有两个适配器都要求非空 `id`；`ccc_corpus` 的正文字段为 `content`，`wanli` 为 `raw_text`。同一批次中的 `source_key` 必须唯一，包括多个输入文件之间；重复键触发 COPY 唯一约束错误，暂存事务回滚，批次标为 `failed`。

迁移 `0004` 要求 `staging_text` 为空，升级前应完成已有暂存批次。暂存的两步均保存 UTF-8 SHA-256 `text_hash` 和字符数 `char_count`；清洗正文与原始版相同时，清洗行的 `content` 为 NULL、哈希相同。发布仍生成原始版和清洗版两个版本。

### 查看入库结果和批次状态

成功时命令打印 JSON 报告，包括 `batch_id`、`status`、`records_ok`、`records_failed`、`new_documents`、`new_versions`、`unchanged`、新增正文的 `plain_bytes` / `stored_bytes`、`timings`（登记、暂存、验证、发布及 `publish.*` 各步骤的秒数）和 `dedup`（文档级）、`dedup_spans`（段落级）查重结果。字节统计只计算本批次新增到正文存储的内容，不是所有记录的正文总量。

| 状态 | 含义 |
|---|---|
| `received` | 已登记来源、批次和输入文件信息。 |
| `staged` | 已完成解析和暂存。 |
| `validated` | 至少一条成功记录，且每条成功记录都有原始和清洗后的暂存正文。 |
| `published` | 已发布版本、切换当前版本并删除该批次暂存分区。 |
| `failed` | 解析或暂存过程抛出异常；该阶段事务回滚，并登记失败状态。 |
| `needs_review` | 暂存验证未通过，不能发布；例如没有成功记录。 |
| `abandoned` | 人工放弃未发布批次，暂存分区已删除；登记和解析记录保留。 |

发布阶段发生异常不等于自动标记 `failed`；该标记只在解析、暂存异常处理分支中写入。

### 批次管理

```bash
midstream batch-list
midstream batch-list --include-published --limit 100
midstream batch-show 123
midstream batch-resume 123 --actor operator --reason '核对原文件后恢复'
midstream batch-abandon 123 --actor operator --reason '源文件不合格'
midstream events --batch-id 123
```

`batch-show` 返回状态、来源、解析器版本、登记文件路径/大小/SHA-256、成功和失败记录数、两步暂存数、是否满足基本验证条件、前 20 条失败记录及分区是否存在。它只检查数据库；恢复时才逐文件核对原路径、大小和哈希，并要求当前适配器的解析器版本与登记值一致。文件丢失或变化时需以新文件登记新批次，不能用旧批次身份继续。调用方可直接使用 `midstream.batches` 中的结构化函数。

`batch-resume` 按现有状态推进：`received`/`failed` 清理该批次残留记录后重新暂存，`staged`/`needs_review` 重新验证，`validated` 发布；验证仍不通过时返回 `needs_review`，不强行发布。发布事务会原子写版本并删除暂存分区，之后再运行两类查重。若发布后查重失败，批次已是 `published`；再次恢复返回 `already_published` 和 `next_action=dedup`，应单独运行 `midstream dedup` 与 `midstream dedup --spans`。不会再次发布同一批次。

`batch-abandon` 只接受未发布批次，删除其暂存分区并设为 `abandoned`；登记文件信息、解析记录和人工操作记录保留。重复放弃返回 `already_abandoned`。对已发布或已放弃批次恢复、对已发布批次放弃均返回可识别的错误。两类写操作都要求 `--actor` 与 `--reason`，并在 `management_event` 留痕；不要把未知来源信息猜作处理依据。

### 失败记录与重复入库

坏 JSON、缺少 ID、空正文等记录作为失败记录保存。正文含 NUL 或代理码点，或清洗后为空，也会转为失败记录。它们有 `source_record.parse_status='failed'` 和 `parse_error`，不生成文档版本。单条记录失败不一定阻止批次发布；验证只要求成功记录的暂存完整且至少一条成功。若适配器直接抛出异常，则整个暂存阶段失败并回滚。

可用 SQL 查看已保存的失败记录；将 `123` 换为报告中的批次 ID：

```sql
SELECT source_key, locator, parse_error
FROM source_record
WHERE batch_id = 123 AND parse_status = 'failed'
ORDER BY record_id;
```

重复入库分两层处理：

- 同一来源中，只要本次任一文件的 SHA-256 与已发布批次的任一文件相同，就跳过整次入库。CLI 输出“跳过”并以状态码 0 结束；更换文件名或解析器版本不会绕过此检查。
- 文件内容变化后可登记新批次。对同一 `source_key`，比较 `raw_fields`、原始正文和错误信息组成的记录哈希。与最近已发布批次中的该记录相同则不生成新版本；变化则复用文档身份，追加原始版和清洗版，并切换当前版本。新文件中没有出现的旧文档不会自动删除。

### 发布后自动查重

版本发布先提交，再依次用两个独立事务运行文档级和段落级查重。即使本批次没有产生新版本，成功发布后也会运行查重。若查重失败，命令抛出异常，但已发布数据保留。修复原因后执行：

```bash
midstream dedup
midstream dedup --spans
```

## 5. 查询和读取正文

`search` 查询当前版本的元数据，不检索正文。默认包含 `ok` 和 `flagged`，排除 `quarantined`。CLI 没有质量状态或最小字数参数；这两个筛选可在导出配方中使用。

| 参数 | 行为 |
|---|---|
| `--source NAME` | 来源名精确匹配；可重复，多个来源之间为“或”。省略则不限来源。 |
| `--title TEXT` | 标题前缀匹配。`%`、`_` 按字面字符处理，不作为通配符。 |
| `--author NAME` | 作者数组中包含这个完整姓名，不是姓名子串匹配。 |
| `--genre TEXT` | 体裁精确匹配。 |
| `--category TEXT` | 分类精确匹配。 |
| `--year-from YEAR` | 成书年份查询区间的下界，含端点。 |
| `--year-to YEAR` | 成书年份查询区间的上界，含端点。只提供一端时另一端不限。 |
| `--include-unknown-years` | 有年份筛选时，同时纳入成书年份未知的文档。 |
| `--quality STATUS` | 可重复指定 `ok`、`flagged`、`quarantined`；默认含前两类。 |
| `--limit N` | 最多返回条数，默认 `50`。 |
| `--after DOC_ID` | 只返回 `doc_id` 大于该值的文档，默认 `0`，按 `doc_id` 升序分页。 |

不同字段之间为“且”。年份按两个区间是否有交集匹配，不要求文档的整个年代范围落入查询区间。例如已映射为唐代（618—907）的文档，也会匹配 800—850。

存在年份条件时，默认排除未知成书年份；加 `--include-unknown-years` 才包含。没有年份条件时，未知年份本来就包含在结果中，这个开关不产生额外效果。年份以负数表示公元前，`-1` 表示公元前 1 年；年代规则不使用公元 0 年。

```bash
midstream search --source ccc_corpus --source wanli --limit 20
midstream search --title 北齐书
midstream search --author 李白
midstream search --year-from 800 --year-to 850 --include-unknown-years
# 将 123 换成上一页最后一条的 doc_id
midstream search --limit 20 --after 123
```

结果包括 `doc_id`、当前 `version_id`、来源、文档内版本号、标题、作者、成书标签及年份、分类、体裁、质量状态、标记和字符数。可用 `--quality flagged` 查找待核文档。读取正文时使用结果里的文档 ID：

```bash
midstream show 123
midstream show 123 --version 1
midstream stats
```

`show` 省略 `--version` 时读取当前版本；指定正整数时读取该文档内的 `version_no`，不是全库的 `version_id`。输出含正文及该版本的完整字段快照；文档或指定版本不存在时退出码为 1。`stats` 返回各来源当前文档数、质量与年份统计、版本数以及正文和数据库存储统计。

## 6. 重新处理已有文档

CLI 默认只预览，打印会变化的文档数量 `changed` 和最多 20 条样本，不写入新版本。样本包含正文片段及元数据的前后值。

```bash
midstream reprocess quality-v2
midstream reprocess quality-v2 --apply
midstream reprocess wanli-collation-v1
midstream reprocess wanli-collation-v1 --apply
midstream dedup
midstream dedup --spans
```

`--apply` 按规则处理当前版本。正文或元数据确有变化时，追加版本并将其设为当前版本，旧版本保留。只有元数据变化时复用原正文；正文变化时重新判定质量。没有变化的文档不新增版本。重新处理不会自动查重，应在导出前手动更新查重快照。

### 现有规则

| 规则 | 范围与行为 |
|---|---|
| `quality-v2` | 所有来源。重算质量状态和标记，不改正文。存在替换字符 `�`、汉字占非空白字符比例低于 0.3，或匹配嵌入校勘记的模式时标记为 `flagged`，否则为 `ok`。 |
| `wanli-collation-v1` | 仅 `wanli`。按正文中的重复片段、ASCII 冒号、句号和校勘来源前缀等模式删除部分嵌入校勘记。不是任意校勘记的通用删除规则，执行前应查看预览。 |

入库用的 `clean-v1` 将 CRLF、CR 统一为 LF，去除指定控制字符；繁简、异体字、标点和全角空格保持原样。它与查重时的 `compare-v1` 归一化不同。

### 添加规则

在 `src/midstream/rules.py` 中编写函数，按以下接口返回结果，并注册到 `RULES`：

- 函数接收当前版本字典，含版本字段、`source` 和解压后的 `text`。
- 无变化返回 `None`；有变化返回 `Change(text=新正文, metadata=字段覆盖字典)`。省略 `text` 表示正文不变。
- 创建 `Rule(version="新规则版本", sources=None, apply=函数)`；`sources=None` 表示所有来源，也可设为来源名列表。将它加入现有注册元组。
- 元数据覆盖只使用重新处理流程支持的字段：`quality_status`、`quality_flags`、`license`、`title`、`authors`、`composed_years`、`composed_label`、`edition_years`、`genre`、`category`、`topics`、`legacy_category`、`field_basis`、`extra`。覆盖 JSON 字段时需自行保留原有条目，流程不做嵌套合并。

给新逻辑使用新的规则版本名，先运行 `midstream reprocess 新规则版本` 检查，再加 `--apply`。未知元数据继续留空；规则推导值应同时记录字段依据。

## 6A. 人工审核

```bash
midstream search --quality flagged
midstream show 123
midstream review-document 123 456 --metadata '{"quality_status":"ok","quality_flags":[]}' --actor operator --reason '核对原扫描件'
midstream review-document 123 457 --metadata '{"title":"核定标题","authors":null}' --actor operator --reason '核对目录'
midstream work-links 123 --status candidate
midstream review-work 123 42 whole confirmed --actor operator --reason '核对书目'
midstream events --doc-id 123
```

`review-document` 的第二个位置参数是查看时取得的当前 `version_id`，用于乐观校验；不是 `version_no`。质量状态和元数据改动会生成新的完整版本快照，正文哈希不变，旧版本除 `is_current` 外不改。新版本号取该文档所有版本的最大值加一，父版本指向审核时的当前版本。改动字段的 `field_basis` 标为 `human`；操作人、依据及前后值保存在 `management_event`。改动后应在需要查重准确性的导出前重跑两类查重。

元数据 JSON 支持 `quality_status`、`quality_flags`、`license`、`title`、`authors`、`composed_years`、`composed_label`、`edition_years`、`genre`、`category`、`topics`、`legacy_category`、`extra`。年份区间使用 PostgreSQL `int4range` 字符串，如 `"[618,908)"`，未知值传 `null`；数组字段传字符串数组，`extra` 传完整对象，覆盖时不会自动深层合并。未知事实保持 `null`，不凭猜测填写。无变化返回 `no_change`，当前版本不匹配返回 `conflict`。

`work-links` 列出文档已有的作品关联，可按状态过滤并取得 `work_id`、关系及依据。`review-work` 只审核这些已有的 `document_work` 关联，决定为 `confirmed` 或 `rejected`；默认要求先前状态是 `candidate`，可用 `--expected-status confirmed` 等显式覆盖。关联原 `basis` 保留，人工决议另记事件。作品和关系不存在返回 `not_found`；状态不同返回 `conflict`。

CLI 成功时输出 JSON；业务错误时输出 `{"status":"error","code":"...","message":"..."}` 并以状态码 2 结束。库函数抛出带 `code` 的 `ManagementError`，适合未来 API 复用。审计事件可用 `events --doc-id` 或 `events --batch-id` 查询。

## 7. 查重

```bash
midstream dedup
```

`midstream dedup` 只运行文档级查重；`midstream dedup --spans` 只运行段落级查重。两者覆盖所有当前版本，不按质量状态筛选，也不删除正文或文档。文档级返回 JSON 报告，含指纹新增数、文档数、代表数、精确组数、重复簇数、成员数、候选对数、通过验证的对数和耗时。

### compare-v1 归一化

比较时使用 OpenCC 1.1.9 `TSCharacters` 的逐字首候选映射，将映射表中的繁体字转换为对应字；保留支持的 CJK 汉字和 ASCII 字母数字，忽略空白、标点及其他字符，保留 ASCII 字母大小写。它不是完整的 OpenCC 词组转换，也不修改已保存或导出的正文。

精确分组按归一化文本的比较哈希判断，因此繁简和标点差异可能被归为精确重复，并不要求原始正文相同。比较哈希使用 XXH3-128，与存储正文用的 SHA-256 不同。归一化后不足 8 字符的正文只参加精确分组。

### 近重复与阈值

长度足够的归一化正文生成字符 8-gram 集合，用 128 桶 OPH 签名（rotation densification）和 32 bands × 4 rows 的 LSH 生成候选。签名估算相似度低于 0.6 的对排除；剩余候选用 8-gram 集合的 Jaccard 相似度验证，达到 0.8 才合并。阈值和这些参数固定在代码中，CLI 没有调整参数。

候选最多迭代 6 轮，是近似检索，不保证找出所有达到阈值的文档对。簇按通过验证的边连通合并，因此同一簇内任意两篇不一定都达到 0.8。数据库成员字段虽使用 `near_candidate` 这个名称，近重复合并仍须通过 Jaccard 验证。

### 快照和指纹恢复

文档级查重在同一事务内更新当前结果，只有新增、删除或类型变化的成员才写入持久表。未变簇保留 `cluster_id` 和 `run_id`；`dup_cluster.run_id` 表示该簇最后变化的运行，当前结果可能来自多个运行。运行信息保存在 `processing_run`，`dedup_snapshot` 分别指向文档和段落当前结果对应的完整运行。导出读取全部当前文档簇，不按最大 `run_id` 筛选。旧数据集已固定到版本，不依赖旧簇。入库后自动更新快照，重新处理后需手动更新；导出不会自行刷新快照。

每次先计算完整当前版本集合的摘要，并核对算法参数。输入及参数相同则直接返回 `reused_snapshot=true` 和原 `run_id`，不新建处理运行、不读取正文。结果规模字段沿用快照，本次新增计算和写入计数为 0。

指纹按 `(text_hash, rule_version)` 缓存；文档候选的精确 Jaccard 分数按比较规则及一对排序后的 `compare_hash` 缓存，包括未通过阈值的候选。改元数据或换代表版本时，只要比较正文相同就能复用分数。阈值变化重新判定缓存分数，比较规则变化使用独立缓存。每次输入变化仍重建 LSH 和连通分组，避免桥接文本移除后保留失效的簇。

文档报告增加 `cached_pairs`（复用分数的候选对数）、`computed_pairs`（实际重新计算的候选对数）、`clusters_changed`、`members_written`、`members_deleted`。`verified_pairs` 统计本次候选中达到阈值的对数，包含复用的分数；直接复用整份快照时为 0。

`text_fingerprint` 和 `dedup_pair` 均为 UNLOGGED 缓存，崩溃恢复后可能清空。需要重算时会自动补齐；输入未变且持久结果仍有效时可以继续复用快照。也可主动完整重算：

```bash
midstream dedup --full
midstream dedup --spans --full
```

`--full` 忽略输入快照及候选分数缓存，已有正文指纹仍可复用，缺失指纹自动重建。文档级完整重算也只持久化变化的成员。升级旧部署需要迁移 `0006_incremental_dedup` 并重新安装 Rust 扩展。

### 段落级：span-v1

```bash
midstream dedup --spans
```

段落级用于书与章节等粒度不同、文档 Jaccard 难以匹配的情况。每个 `compare_hash` 仅处理最小 `version_id` 的代表版本，归一化后不足 64 字符的跳过；精确重复的其他版本仍由文档级簇表达。

复用 compare-v1 归一化，以字符 32-gram 滚动哈希和宽 32 的单调队列窗口选择最右最小值。Winnowing 保证共同片段达到 `k+w-1=63` 字符时有共同指纹；实际只保留至少 64 字符的片段。同一哈希的指纹记录超过 1,000 条时当作模板跳过，因此只含这类指纹的片段可能漏检。其余按文档对、对角线合并种子，逐字验证并向两侧扩展，哈希碰撞不会造成误报。

`version_id` 较小者作为出处，较大者的片段标记为重复；这只是固定方向，不表示历史上的真实原始出处。存储在 `dup_span`，字段为 `run_id`、`version_id`、`start_char`、`end_char`、`source_version_id`、`source_start_char`。位置均为清洗后正文的 Unicode 字符偏移（Python 字符串索引），从 0 开始，区间左闭右开；边界取首末归一化字符在原文中的位置，不包含边界外的标点。片段长度阈值按归一化字符计。

匹配保证归一化字符逐字相同；原文切片仍可能有繁简、标点差异。目标片段为 `text[start_char:end_char]`，出处从 `source_start_char` 开始，取相同数量的归一化字符。删除被完全覆盖的重复区间；不同出处的部分重叠区间保留，确保每条记录的整段出处都能核验，不能将它们的原文长度直接相加作为重复字符总数。

指纹、种子分 256 区写到 `tempfile.mkdtemp` 创建的目录，结束或异常时删除。通常分区直接读入排序；超过 64 MiB 时按固定块排序、64 路归并，避免模板集中造成内存膨胀。正文先以压缩形式暂存，种子围绕每对中正文较大的文档、按其连续编号聚集，同一文档的候选尽量在同批确认。每批涉及的文档各解压归一化一次，以共享只读结构供 Rayon 并行扩展匹配；线程内缓冲结果后按块写出，最后按目标文档统一删除被覆盖区间。文本批次预算仍为 256 MiB，单篇超大正文仍需容纳至少一对文档，批次结束即释放归一化正文。Rust 线程由 `RAYON_NUM_THREADS` 控制，例如服务器设为 `20`。临时磁盘可用 `TMPDIR` 选择；种子数量随重复分布增长，可能远大于指纹数量。

报告包括 compare-v1 新缓存数、代表数、足够长的文档数、指纹数、种子数、模板哈希数、确认匹配数、最终区间数，以及缓存、指纹、种子、确认、写入和总耗时。当前 `representatives` 只统计原文至少 64 字符的代表。输入及参数未变时直接复用快照；输入变化或指定 `--full` 时，仍在独立事务内完整重算、`TRUNCATE dup_span` 并通过二进制 COPY 写入最新快照，运行信息保存为 `processing_run.rule_version='span-v1'`。正文和结果行均由 Rust 解析或生成，Python 只传递 COPY 字节。

段落快照与文档簇相互独立。入库自动刷新两者；重新处理后需分别手动运行两个命令。目前导出仍只使用文档级簇，不按 `dup_span` 删减正文或约束划分。

## 8. 配方导出、校验与训练登记

### 编写配方

配方是 JSON 文件。仓库提供 [small-sources-v1.json](../recipes/small-sources-v1.json)：

```json
{
  "name": "small-sources-v1",
  "filters": {
    "sources": ["ccc_corpus", "wanli"],
    "quality": ["ok"]
  },
  "splits": {"train": 0.9, "val": 0.05, "test": 0.05},
  "seed": "small-sources-v1"
}
```

| 顶层字段 | 要求与含义 |
|---|---|
| `name` | 必填，数据集名称，库内唯一。CLI 也将它作为导出根目录下的目录名，应使用简单目录名。 |
| `filters` | 可省略；筛选当前版本，默认 `quality=["ok"]`。 |
| `splits` | 必填，各划分的比例。键只允许 `train`、`val`、`test`，可省略其中某项，省略视为 0；比例之和必须为 1。填写 0 到 1 之间的数值。 |
| `seed` | 必填，字符串。用于确定组的划分和成员顺序。 |

`filters` 支持以下全部字段；未知筛选字段会报错。

| 字段 | 类型与默认值 | 行为 |
|---|---|---|
| `sources` | 字符串数组，默认 `[]` | 指定来源，空数组表示不限来源。 |
| `title_prefix` | 字符串，默认空 | 标题前缀，对应 CLI 的 `--title`。 |
| `author` | 字符串，默认空 | 作者数组中的完整姓名。 |
| `genre` | 字符串，默认空 | 体裁精确匹配。 |
| `category` | 字符串，默认空 | 分类精确匹配。 |
| `year_from` | 整数，默认 `null` | 成书年份下界，含端点。 |
| `year_to` | 整数，默认 `null` | 成书年份上界，含端点。 |
| `include_unknown_years` | 布尔值，默认 `false` | 有年份筛选时是否包含未知年份，规则与 search 相同。 |
| `quality` | 字符串数组，导出默认 `["ok"]` | 可显式选择 `ok`、`flagged`、`quarantined`；空数组不会选中任何文档。 |
| `min_chars` | 整数，默认 `null` | 当前版本字符数下限，含端点；是保存正文的字符数，不是归一化后长度。 |

未设置的字符串筛选可省略或用 `null`。筛选之间为“且”；导出只选择当前版本，没有选择历史版本的配方字段。

### 创建数据集并导出

```bash
midstream dedup
midstream export recipes/small-sources-v1.json
```

命令先在数据库中固定成员和字符数，再生成 Parquet 文件，输出 `release_id`、名称、总字符数 `char_count`、各划分字符数 `split_char_counts` 和文件路径。文件写到 `MIDSTREAM_EXPORT_ROOT/<name>/`，每个非空划分一个文件：`train.parquet`、`val.parquet`、`test.parquet`。空划分不生成文件。

每行包含 `ordinal`、`version_id`、`doc_id`、`title`、`text`，文件内按 `ordinal` 排序。`ordinal` 是整个数据集的成员序号，因此单个划分中的序号可能不连续。Parquet 使用 zstd 压缩，写临时文件后再改名；数据库记录文件 SHA-256、PyArrow 版本等导出信息。

没有选中任何文档会报错。同名数据集不能再次创建；要基于当前库状态发布新数据集，应改用新名称。

### 查看数据集字符数

```bash
midstream release-stats 1
```

输出发布时保存的统计，例如：

```json
{
  "release_id": 1,
  "name": "small-sources-v1",
  "char_count": 10000,
  "split_char_counts": {"train": 9000, "val": 500, "test": 500}
}
```

以上数字仅用于说明格式。字符数按发布时所选版本正文的 Unicode 码点计算，与 Python `len(text)` 一致，包括标点、空格、换行及补充平面字符；组合字符按各码点分别计数。标题和元数据不计入，也不做繁简或其他归一化。相同正文的不同成员分别计数，空划分为 0，总数等于三个划分之和。

统计与成员在同一事务内保存，后续入库或重新处理不会改变旧发布的字符数。已有部署先执行 `midstream init`，外部实例用 `alembic upgrade head`；迁移 `0005_release_char_count` 按旧发布固定的成员版本自动回填，不解压正文。

### 按作品和重复簇分组划分

选中的文档通过非 `rejected` 的作品关联和最新一次查重的文档级簇形成连通组。同一组整体落入一个划分；同作品或同重复簇的已选文档不会跨 train/val/test。作品关联不只使用 `confirmed`，`candidate` 也参与。

组以组内最小 `doc_id` 标识，结合 `seed` 决定划分；成员顺序结合 `seed` 和 `version_id` 决定。比例是分组哈希的分配比例，不保证文档数或字符数精确达到比例，小数据集可能有空划分。查重分组用于避免跨划分重复，不会在重复簇中自动只保留一篇。

### 训练历史检查

创建数据集时，会检查将进入 val/test 的文档是否出现在已登记训练使用的数据集的 train 划分中，也会检查它们是否与这些训练文档属于同一作品（双方关联状态均非 `rejected`）。命中则报 `TrainingLeak`，本次数据集不创建；不会自动把这些文档改分到 train。

这个检查只依据已登记的训练历史，通过文档身份和作品关联反查，不沿历史重复簇或相同正文哈希扩展检查。未登记的训练不会被系统识别。

### verify 与复现范围

```bash
# 将 1 换成 export 输出的 release_id
midstream verify 1
```

`verify` 重算数据库内成员顺序、版本、划分和正文哈希组成的摘要，核对总字符数及各划分字符数，再解压数据集涉及的正文逐一核对 SHA-256 和正文长度。通过时打印“通过”并返回 0，摘要、字符数或正文哈希不符时打印“未通过”并返回 1；数据集不存在或解压错误会抛出异常。它不读取 Parquet 文件，也不核对磁盘文件是否被删除或改写。

复现保证的是已发布数据集的成员、顺序、划分和正文内容一致，不保证重新写出的 Parquet 文件字节一致。文件格式或工具版本变化可能影响字节。相同配方在库内容、当前版本或分组变化后再次筛选，也不保证得到同一数据集；固定下来的发布记录才是复现依据。

当前 CLI 没有按 `release_id` 重建文件的命令。若导出写文件失败，数据库发布记录可能已经提交；可通过已有 Python 接口按原发布重写文件，避免创建同名数据集：

```python
from pathlib import Path

import psycopg
from midstream import config
from midstream.export import write_parquet

with psycopg.connect(config.dsn()) as conn:
    write_parquet(conn, release_id=1, out_dir=Path("/path/to/rebuilt-export"))
```

### 登记训练使用

```bash
midstream record-training small-sources-v1 run-name
```

第一个参数是数据集名称，不是 `release_id`；第二个参数是训练运行名称。命令登记当前时间，不启动训练。相同数据集与运行名称的组合只能登记一次，重复执行会因唯一约束报错。登记后，后续创建数据集的训练历史检查会使用该记录。

## 9. 添加来源适配器

适配器需实现 `src/midstream/adapters/base.py` 中的接口：

| 成员 | 含义 |
|---|---|
| `name` | 来源名，用于注册和 CLI 的 source 参数。 |
| `parser_version` | 解析器版本，写入批次和解析运行。 |
| `description` | 来源说明。 |
| `license_note` | 数据来源的许可证记录；未知情况如实说明。 |
| `default_paths()` | 返回默认输入文件的 `list[Path]`。 |
| `iter_chunks(path)` | 按文件顺序产出可 pickle 的原始块，在主进程运行。 |
| `parse_chunk(chunk)` | 将一个原始块解析成 `list[ParsedRecord]`，在工作进程运行。 |
| `parse(path)` | 依次调用上述两方法，逐条产出 `ParsedRecord`；供直接读取适配器使用。 |

`ParsedRecord` 的字段为 `source_key`、`locator`、`raw_fields`、`text`、`error`、`metadata`。成功记录需有稳定且在批次内唯一的来源键、正文和可用元数据。失败记录也要产出，填写 `error`，以便登记；不要为单条可记录的解析错误直接终止整个迭代器。

`JsonlAdapter` 提供公共 `iter_chunks()` 和 `parse()` 实现：按字节读取约 8 MiB，在完整行边界切开，`JsonlChunk` 包含原始 `data`、起始行号 `line` 和字节偏移 `offset`。`iter_jsonl(chunk)` 保留每行在原文件中的行号与字节偏移，空行照常计数，坏行作为错误产出。其他格式自行实现这三个方法。适配器类及块类型需定义在可导入模块顶层，以便 spawn 进程 pickle；块只携带原始数据，不携带打开的文件或数据库连接。

下面是读取 UTF-8 JSONL 的最小示例。输入使用非空字符串 `id` 和 `text`，可选 `title`。保存为 `src/midstream/adapters/example.py`：

```python
from pathlib import Path

from ..config import JISHUI_RAW
from .base import JsonlAdapter, JsonlChunk, ParsedRecord, iter_jsonl


class ExampleAdapter(JsonlAdapter):
    name = "example"
    parser_version = "example-v1"
    description = "示例 JSONL 来源"
    license_note = "未知"

    def default_paths(self) -> list[Path]:
        return [JISHUI_RAW / "example.jsonl"] if JISHUI_RAW else []

    def parse_chunk(self, chunk: JsonlChunk) -> list[ParsedRecord]:
        records = []
        for line_no, offset, record, error in iter_jsonl(chunk):
            locator = {"line": line_no, "offset": offset}
            if record is None:
                records.append(ParsedRecord(f"invalid:{line_no}", locator, {}, error=error))
                continue
            key = record.get("id")
            text = record.get("text")
            fields = {k: v for k, v in record.items() if k != "text"}
            if not isinstance(key, str) or not key:
                records.append(ParsedRecord(
                    f"invalid:{line_no}", locator, fields, error="缺少字符串 id"
                ))
                continue
            if not isinstance(text, str) or not text:
                records.append(ParsedRecord(key, locator, fields, error="正文为空或不是字符串"))
                continue
            title = record.get("title")
            records.append(ParsedRecord(
                key, locator, fields, text=text,
                metadata={"title": title if isinstance(title, str) else None},
            ))
        return records
```

示例中的 `invalid:` 前缀须为错误记录保留，不能与有效 ID 冲突；多文件批次也须保证键不重复。之后在 `src/midstream/adapters/__init__.py` 导入 `ExampleAdapter`，将 `ExampleAdapter()` 加入构建 `ADAPTERS` 的现有实例元组。注册后即可运行：

```bash
midstream ingest example --path /path/to/example.jsonl
```

`metadata` 中可提供标题、作者数组、成书标签、分类、体裁、主题数组、许可证、`field_basis` 和 `extra` 等版本字段。若来源明确给出作品信息，可用 `work_title`、`work_relation`（`whole`、`part`、`excerpt` 或 `contains`）建立关联；`unit_type` 保存到文档身份。元数据未知则省略或留空，不编造作者、年代或许可证。

记录哈希不包含解析器输出的 `metadata` 本身，因此只修改元数据推导逻辑或 `parser_version` 不一定产生新版本；原文件还可能先被重复文件检查跳过。对已入库文档的规则修订，应走重新处理流程。添加依赖前核对许可证与项目 GPL-3.0 的兼容性；来源数据的许可也应单独如实记录。

## 10. 常见问题

### 安装 Python 包后，为什么命令报找不到 midstream_dedup？

CLI 会导入 Rust 查重扩展。安装主包之外，还需在同一 Python 3.12 虚拟环境执行 `pip install ./rust`；该步骤需要 Rust 工具链。

### 为什么查询能看到文档，默认导出却没有？

`search` 默认包含 `ok` 和 `flagged`，导出默认只含 `ok`。也要核对配方的来源、标题、年代和字数条件。可用 `show` 查看质量标记；现有质量规则不会自动生成 `quarantined`。

### wanli 明明有年月日，为什么按年份查不到？

来源中的年月日是事件日期，不写入成书年份。带年份条件时需加 `--include-unknown-years` 才能纳入成书年份未知的文档。

### 查重后，为什么导出的重复文档还在？

查重只写重复簇或重复片段快照，不删除文档。导出利用簇和作品关联约束划分，不自动从簇内选代表。

### 为什么原文件未变，更新解析器后仍然跳过？

重复文件检查按同一来源的文件 SHA-256 判断，不看解析器版本。已入库数据的修订可添加重新处理规则，预览后再应用。

### 入库命令报查重失败，是否要重新入库？

若已进入发布后的查重阶段，发布已提交。修复查重原因后依次运行 `midstream dedup` 和 `midstream dedup --spans` 即可；重新导入同内容文件会被跳过。其他阶段的异常需按批次状态判断，当前没有自动恢复命令。

### verify 通过，是否说明导出的文件也没问题？

它只验证库内发布记录和正文，不检查导出文件。文件丢失时可用已有发布记录调用 `write_parquet` 重建，见上文。
