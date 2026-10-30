# midstream-core 使用指南

本指南对应仓库当前的 Python CLI、数据库迁移和 Rust 查重扩展。命令在仓库根目录执行，并使用已激活的虚拟环境。示例中的 `/path/to/…` 均需替换为部署环境的实际路径。

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
| 数据集 | 一次命名的发布，固定到具体 `version_id`，保存成员、顺序、train/val/test 划分、配方和内容摘要。 |

首次发布一篇文档时生成版本 1（原始版）和版本 2（清洗版），清洗版成为当前版本。即使两版正文相同，也会保留两个版本。已发布版本除 `is_current` 外只增不改；正文或元数据的修订通过新版本表达。正文按 UTF-8 SHA-256 存储并使用 zstd 压缩，相同正文共用存储。

数据集发布后，成员、顺序和划分不再变化。之后入库、重新处理或查重不会改变旧数据集。重复簇快照和指纹缓存是派生数据，不具备这种不可变性。

未知元数据留空。已收录的朝代标签可由规则映射为成书年份区间，依据写入 `field_basis`；没有对应规则就保留未知。`wanli` 的事件日期保存在 `extra.event_date`，不作为成书年份。

## 2. 安装与建库

需要：

- PostgreSQL 17，支持 LZ4 压缩；迁移会创建使用 LZ4 的暂存表。
- Python 3.12。主包允许 `>=3.12`，当前 Rust 扩展只允许 `>=3.12,<3.13`。
- Rust 工具链，含 Cargo；`pip install ./rust` 通过 maturin 构建扩展。

先启动 PostgreSQL，准备能建库的数据库角色。以下建库命令使用 PostgreSQL 客户端默认连接；若另设连接参数，需确保建库、迁移与 CLI 指向同一数据库。

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
midstream stats
```

迁移命令需在仓库根目录执行。迁移创建表和索引，不导入语料。输入语料需自行准备；入库流程读取输入文件，不改写它们。

## 3. 配置部署环境

配置通过进程环境变量读取。没有自动加载 `.env` 的逻辑。路径和并发参数在模块导入时读取，修改后需重新启动命令。

| 环境变量 | 默认值或行为 | 用途与部署设置 |
|---|---|---|
| `MIDSTREAM_DSN` | `dbname=midstream` | CLI 的 psycopg 连接串，默认走本机连接。设为部署数据库。 |
| `MIDSTREAM_ALEMBIC_URL` | `postgresql+psycopg:///midstream` | Alembic 的 SQLAlchemy URL。与 DSN 分开配置，须指向同一库。 |
| `MIDSTREAM_JISHUI_RAW` | 代码内置的开发环境原始数据目录 | 使用适配器默认路径时的根目录。部署时设置自己的路径；显式 `ingest --path` 可绕过默认输入路径。 |
| `MIDSTREAM_EXPORT_ROOT` | 代码内置的开发环境导出目录 | CLI 导出根目录。部署时设置为可写路径，数据集写到其下的配方名称目录。 |
| `MIDSTREAM_WORKERS` | `8` | 入库和重新处理时压缩正文的 Python 工作进程数，使用正整数。 |
| `MIDSTREAM_CHUNK_DOCS` | `2000` | Python 流程的分块记录数，也用于查重验证批量大小，使用正整数。 |
| `RAYON_NUM_THREADS` | Rayon 默认线程数 | Rust 查重扩展的线程数；与 `MIDSTREAM_WORKERS` 分开控制。 |
| `MIDSTREAM_TEST_DSN` | 未设置时跳过端到端测试 | 只用于测试。必须指向专用测试库，测试会清空其 `public` schema。 |

默认输入路径为：

| 来源 | 相对于 `MIDSTREAM_JISHUI_RAW` 的文件 |
|---|---|
| `ccc_corpus` | `chinese-classical-corpus/corpus.jsonl` |
| `wanli` | `wanli-dibao-corpus/wanli_shilu_corpus.jsonl` |

测试命令及构建测试时的 `PYO3_PYTHON`、`LD_LIBRARY_PATH` 设置见 [README](../README.md#测试)。

## 4. 入库新来源

当前已注册 `ccc_corpus` 和 `wanli`。省略 `--path` 使用上表中的默认文件；`--path` 可重复，用于一次导入同一来源的多个文件。

```bash
midstream ingest wanli
midstream ingest ccc_corpus --path /path/to/corpus.jsonl
midstream ingest wanli --path /path/to/part-1.jsonl --path /path/to/part-2.jsonl
```

适配器按 JSONL 逐行解析，空行跳过。现有两个适配器都要求非空 `id`；`ccc_corpus` 的正文字段为 `content`，`wanli` 为 `raw_text`。同一批次中的 `source_key` 必须唯一，包括多个输入文件之间。

### 查看入库结果和批次状态

成功时命令打印 JSON 报告，包括 `batch_id`、`status`、`records_ok`、`records_failed`、`new_documents`、`new_versions`、`unchanged`、新增正文的 `plain_bytes` / `stored_bytes` 和 `dedup` 查重结果。字节统计只计算本批次新增到正文存储的内容，不是所有记录的正文总量。

| 状态 | 含义 |
|---|---|
| `received` | 已登记来源、批次和输入文件信息。 |
| `staged` | 已完成解析和暂存。 |
| `validated` | 至少一条成功记录，且每条成功记录都有原始和清洗后的暂存正文。 |
| `published` | 已发布版本、切换当前版本并删除该批次暂存分区。 |
| `failed` | 解析或暂存过程抛出异常；该阶段事务回滚，并登记失败状态。 |
| `needs_review` | 暂存验证未通过，不能发布；例如没有成功记录。 |

状态由入库流程自动推进，当前没有批次列表、审核、恢复或重试管理命令。发布阶段发生异常不等于自动标记 `failed`；该标记只在解析、暂存异常处理分支中写入。

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

版本发布先提交，再用独立事务查重。即使本批次没有产生新版本，成功发布后也会运行查重。若查重失败，命令抛出异常，但已发布数据保留。修复原因后执行：

```bash
midstream dedup
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

结果包括 `doc_id`、来源、文档内版本号、标题、作者、成书标签及年份、分类、体裁、质量状态和字符数。读取正文时使用结果里的文档 ID：

```bash
midstream show 123
midstream show 123 --version 1
midstream stats
```

`show` 省略 `--version` 时读取当前版本；指定正整数时读取该文档内的 `version_no`，不是全库的 `version_id`。输出含正文、版本号、标题、质量状态和标记；文档或指定版本不存在时退出码为 1。`stats` 返回各来源当前文档数、质量与年份统计、版本数以及正文和数据库存储统计。

## 6. 重新处理已有文档

CLI 默认只预览，打印会变化的文档数量 `changed` 和最多 20 条样本，不写入新版本。样本包含正文片段及元数据的前后值。

```bash
midstream reprocess quality-v2
midstream reprocess quality-v2 --apply
midstream reprocess wanli-collation-v1
midstream reprocess wanli-collation-v1 --apply
midstream dedup
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

## 7. 文档级查重

```bash
midstream dedup
```

查重覆盖所有当前版本，不按质量状态筛选，也不删除正文或文档。返回 JSON 报告，含指纹新增数、文档数、代表数、精确组数、重复簇数、成员数、候选对数、通过验证的对数和耗时。

### compare-v1 归一化

比较时使用 OpenCC 1.1.9 `TSCharacters` 的逐字首候选映射，将映射表中的繁体字转换为对应字；保留支持的 CJK 汉字和 ASCII 字母数字，忽略空白、标点及其他字符，保留 ASCII 字母大小写。它不是完整的 OpenCC 词组转换，也不修改已保存或导出的正文。

精确分组按归一化文本的比较哈希判断，因此繁简和标点差异可能被归为精确重复，并不要求原始正文相同。比较哈希使用 XXH3-128，与存储正文用的 SHA-256 不同。归一化后不足 8 字符的正文只参加精确分组。

### 近重复与阈值

长度足够的归一化正文生成字符 8-gram 集合，用 128 桶 OPH 签名（rotation densification）和 32 bands × 4 rows 的 LSH 生成候选。签名估算相似度低于 0.6 的对排除；剩余候选用 8-gram 集合的 Jaccard 相似度验证，达到 0.8 才合并。阈值和这些参数固定在代码中，CLI 没有调整参数。

候选最多迭代 6 轮，是近似检索，不保证找出所有达到阈值的文档对。簇按通过验证的边连通合并，因此同一簇内任意两篇不一定都达到 0.8。数据库成员字段虽使用 `near_candidate` 这个名称，近重复合并仍须通过 Jaccard 验证。

### 快照和指纹恢复

每次成功查重在事务中替换全部重复簇及成员，只保留最新快照；运行信息保存在 `processing_run`。旧数据集已固定到版本，不依赖旧簇。入库后自动更新快照，重新处理后需手动更新；导出不会自行刷新快照。

指纹按 `(text_hash, rule_version)` 缓存，重复运行只计算当前正文中缺失的指纹。`text_fingerprint` 是 UNLOGGED 表，数据库崩溃恢复后可能被清空；再次执行 `midstream dedup` 会自动从已存正文重算缺失项，不需要重新入库。

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

命令先在数据库中固定成员，再生成 Parquet 文件，输出 `release_id` 和文件路径。文件写到 `MIDSTREAM_EXPORT_ROOT/<name>/`，每个非空划分一个文件：`train.parquet`、`val.parquet`、`test.parquet`。空划分不生成文件。

每行包含 `ordinal`、`version_id`、`doc_id`、`title`、`text`，文件内按 `ordinal` 排序。`ordinal` 是整个数据集的成员序号，因此单个划分中的序号可能不连续。Parquet 使用 zstd 压缩，写临时文件后再改名；数据库记录文件 SHA-256、PyArrow 版本等导出信息。

没有选中任何文档会报错。同名数据集不能再次创建；要基于当前库状态发布新数据集，应改用新名称。

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

`verify` 重算数据库内成员顺序、版本、划分和正文哈希组成的摘要，再解压数据集涉及的正文逐一核对 SHA-256。通过时打印“通过”并返回 0，摘要或正文哈希不符时打印“未通过”并返回 1；数据集不存在或解压错误会抛出异常。它不读取 Parquet 文件，也不核对磁盘文件是否被删除或改写。

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
| `parse(path)` | 逐条产出 `ParsedRecord` 的迭代器。 |

`ParsedRecord` 的字段为 `source_key`、`locator`、`raw_fields`、`text`、`error`、`metadata`。成功记录需有稳定且在批次内唯一的来源键、正文和可用元数据。失败记录也要产出，填写 `error`，以便登记；不要为单条可记录的解析错误直接终止整个迭代器。

下面是读取 UTF-8 JSONL 的最小示例。输入使用非空字符串 `id` 和 `text`，可选 `title`。保存为 `src/midstream/adapters/example.py`：

```python
from pathlib import Path
from typing import Iterator

from ..config import JISHUI_RAW
from .base import ParsedRecord, iter_jsonl


class ExampleAdapter:
    name = "example"
    parser_version = "example-v1"
    description = "示例 JSONL 来源"
    license_note = "未知"

    def default_paths(self) -> list[Path]:
        return [JISHUI_RAW / "example.jsonl"]

    def parse(self, path: Path) -> Iterator[ParsedRecord]:
        for line_no, offset, record, error in iter_jsonl(path):
            locator = {"line": line_no, "offset": offset}
            if record is None:
                yield ParsedRecord(f"invalid:{line_no}", locator, {}, error=error)
                continue
            key = record.get("id")
            text = record.get("text")
            fields = {k: v for k, v in record.items() if k != "text"}
            if not isinstance(key, str) or not key:
                yield ParsedRecord(
                    f"invalid:{line_no}", locator, fields, error="缺少字符串 id"
                )
                continue
            if not isinstance(text, str) or not text:
                yield ParsedRecord(key, locator, fields, error="正文为空或不是字符串")
                continue
            title = record.get("title")
            yield ParsedRecord(
                key, locator, fields, text=text,
                metadata={"title": title if isinstance(title, str) else None},
            )
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

查重只写重复簇，不删除文档。导出利用簇和作品关联约束划分，不自动从簇内选代表。

### 为什么原文件未变，更新解析器后仍然跳过？

重复文件检查按同一来源的文件 SHA-256 判断，不看解析器版本。已入库数据的修订可添加重新处理规则，预览后再应用。

### 入库命令报查重失败，是否要重新入库？

若已进入发布后的查重阶段，发布已提交。修复查重原因后运行 `midstream dedup` 即可；重新导入同内容文件会被跳过。其他阶段的异常需按批次状态判断，当前没有自动恢复命令。

### verify 通过，是否说明导出的文件也没问题？

它只验证库内发布记录和正文，不检查导出文件。文件丢失时可用已有发布记录调用 `write_parquet` 重建，见上文。
