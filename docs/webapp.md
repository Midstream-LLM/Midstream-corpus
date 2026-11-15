# Web app：启动与操作指南

前端为 React + TypeScript + Vite，后端为 FastAPI，直接复用 Python core。本文覆盖 M1–M10。数据库、API、worker 和语料文件放在运行后端的机器；浏览器和未来 SwiftUI 客户端通过 HTTP 访问。文件、分词器和导出路径均是**后端机器**的路径。Web 不提供浏览器上传、登录或多用户权限；远程使用 SSH 隧道。

安装 Python/Rust、获取源码和初始化数据库见 [使用指南第 2 节](user-guide.md#2-安装与建库)。CLI 全流程见 [CLI、任务与常用流程](user-guide.md#3a-cli任务与常用流程)。

## 启动后端与 Web

### 1. 初始化或升级数据库

在代码仓库根目录激活虚拟环境。私有实例首次初始化、已有实例升级都用：

```bash
source .venv/bin/activate
midstream init
midstream stats
```

外部 PostgreSQL 不运行 init；设置实际的 MIDSTREAM_DSN 和 MIDSTREAM_ALEMBIC_URL 后，执行 `alembic upgrade head`。M1–M10 的迁移需全部升级到 head（当前含 0010–0017）；不能只执行早期任务表迁移。外部库和私有实例是两条配置路径，详见 [外部 PostgreSQL](user-guide.md#使用外部-postgresql)。

所有终端使用相同的 MIDSTREAM_HOME、数据库和 MIDSTREAM_EXPORT_ROOT。环境变量不会从 .env 自动加载，修改后要重启进程。例如可在每个终端显式设置：

```bash
export MIDSTREAM_HOME=/path/to/midstream-home
export MIDSTREAM_EXPORT_ROOT=/path/to/exports
# 仅外部数据库需要；替换为实际连接信息
# export MIDSTREAM_DSN='host=/path/to/socket dbname=midstream user=operator'
# export MIDSTREAM_ALEMBIC_URL='postgresql+psycopg://operator@/midstream?host=/path/to/socket'
```

MIDSTREAM_HOME 应在首次 init 前确定，迁移 URL 与运行 DSN 必须指向同一库。已有实例使用原配置，不要为更新代码另建空目录。

### 2. 构建前端，启动服务

前端构建需要 Node.js/npm，版本须满足锁文件中 Vite 的 engines 要求。在仓库根目录执行：

```bash
source .venv/bin/activate
npm --prefix web ci
npm --prefix web run build
midstream serve --host 127.0.0.1 --port 8765
```

`midstream serve` 先确保内置 PostgreSQL 运行（设置了 MIDSTREAM_DSN 时不启动私有实例），再以子进程启动 worker，前台运行 API；Ctrl-C 退出时一并结束 worker，worker 正在执行的任务记为已取消（已提交阶段保留）。此进程同时提供 API 和已构建的 Web 页面，无需另开 Node 服务：

- Web：`http://127.0.0.1:8765`
- 接口文档：`http://127.0.0.1:8765/api/docs`
- OpenAPI：`http://127.0.0.1:8765/api/openapi.json`
- 存活检查：`http://127.0.0.1:8765/api/v1/health`，仅代表服务存活，不保证数据库可用。

默认静态目录为仓库 web/dist；安装到其他位置时显式使用 `--static-dir /path/to/web/dist`。若 API 可访问而首页 404，先确认已经构建并指定正确目录。更新前端源码后重新构建，再重启服务。

### 3. 只运行 API 时另开 worker

`midstream serve --no-worker` 或 `python -m midstream.api --host 127.0.0.1 --port 8765` 只运行 API，不启动 worker。此时需另开终端：

```bash
# 在仓库根目录，加载与 API 相同的环境变量
source .venv/bin/activate
midstream worker
```

数据库锁限制同库只有一个 worker，同一时间执行一个任务；已有 worker 时 serve 启动的 worker 会报“已有 worker 正在运行”并退出，API 不受影响。没有 worker 时任务保持 queued。服务前台运行，关闭终端会结束进程。

停止时在终端 Ctrl-C。任务中断后按任务中心的失败/恢复说明处理；数据库需要停止时，私有实例另用 `midstream server stop`。外部数据库不由该命令管理。

### 常驻运行：systemd 用户服务

需要在服务器上常驻时，可写一个 systemd 用户服务，不需要 root。以下仅为示例，路径换成实际值；仓库不会自动安装它。保存为 `~/.config/systemd/user/midstream.service`：

```ini
[Unit]
Description=Midstream corpus API and worker

[Service]
WorkingDirectory=/path/to/midstream-core
Environment=MIDSTREAM_HOME=/path/to/midstream-home
Environment=MIDSTREAM_EXPORT_ROOT=/path/to/exports
ExecStart=/path/to/midstream-core/.venv/bin/midstream serve --host 127.0.0.1 --port 8765
KillSignal=SIGINT
TimeoutStopSec=60
Restart=on-failure

[Install]
WantedBy=default.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now midstream.service
systemctl --user status midstream.service
journalctl --user -u midstream.service -f
# 退出登录后仍运行需要：loginctl enable-linger "$USER"
```

`KillSignal=SIGINT` 让停止服务与终端 Ctrl-C 一致：API 退出并结束 worker。私有 PostgreSQL 实例不随服务停止，需要时另用 `midstream server stop`。LLM 和备份配置从 MIDSTREAM_HOME 读取，凭据环境变量需要另写 `Environment=` 或 `EnvironmentFile=`（权限 0600），不要写进仓库。

### 远程浏览器访问

在自己的电脑另开终端建立隧道，USER@HOST 替换为实际 SSH 用户和主机或已有 SSH 别名：

```bash
ssh -N -L 8765:127.0.0.1:8765 USER@HOST
```

然后打开本机 `http://127.0.0.1:8765`。本机端口已占用可改为 `-L 18765:127.0.0.1:8765`，浏览器访问 18765。服务端仍监听 loopback，无需修改 API Host 白名单或跨域配置。

### 前端开发模式

`midstream serve` 同上运行。另一个终端在仓库根目录执行：

```bash
npm --prefix web ci
npm --prefix web run dev -- --port 5173 --strictPort
```

打开 `http://127.0.0.1:5173`，Vite 提供热更新，并把 /api 代理到同机 127.0.0.1:8765。更换 API 端口需同时调整 web/vite.config.js 中的代理目标。开发模式无需先生成 web/dist，任务仍需要 worker。

远程开发时 Vite 也在后端机器运行，再建立两个端口的隧道：

```bash
ssh -N -L 5173:127.0.0.1:5173 -L 8765:127.0.0.1:8765 USER@HOST
```

前端不保存数据库地址或 LLM/OSS 凭据；浏览器始终通过 API 操作。

## 任务中心（M1）

导航“任务中心”可看列表、阶段进度、结果、错误及最近 100 条日志，queued/running 时每 2 秒刷新。关闭页面不会取消任务。取消、重试填写操作人和依据：

- queued 可立即取消；running 只在阶段边界检查取消，当前阶段不强行中断，已提交阶段不回滚。最后阶段可能已完成，取消不保证来得及生效。
- failed/cancelled 可重试，保留旧记录并按原 kind/params 新建 job_id；succeeded 不可重试。
- 心跳每 5 秒更新，worker 启动及取任务前将超过 60 秒无心跳的 running 标为 failed，error=interrupted。刚中断后可能需等待过期，不会自动重试。
- 进度是阶段进度，不是剩余时间预测；批次验证结果 needs_review 可能对应任务 succeeded，应同时检查业务结果。

CLI 同样可用 `midstream jobs list/show/cancel/retry`，详见使用指南。CLI 长操作默认就地执行，也会出现在任务中心；不要在 worker 运行长操作时另开就地重任务争用内存。

批次发布成功后，查重作为后续任务 dedup、dedup_spans 写入队列，任务中心可见；它们的依据注明来自哪个任务。

## 导入与两层预览（M6）

批次管理默认展示全宽列表，显示排队/处理中状态与下一步操作；进入详情后检查预览、发布或放弃。页面每 5 秒更新状态。

点击“新建入库批次”进入三步向导：
- **选择文件夹**：浏览服务器目录，返回上级、收藏常用目录，选择整个文件夹或部分文件。默认包含子文件夹；也可在地址栏输入路径。最多选 200 个入口。
- **检查识别结果**：后台扫描并识别格式，显示进度、支持取消并保留已检查结果。按已识别来源（未知来源按父目录）、字段结构、编码和解析方案分组。可拆分文件到独立组，或移到相同结构及适配器的组。新文件和变化文件默认选中；已发布、已有待处理批次、本次内容重复的文件分别标记。重复判断使用 SHA-256；变化提示依据相同路径的历史记录。
- **预览并导入**：可视化选择正文字段、标题、作者、年代标签和分类，支持嵌套字段；JSON 收在高级设置中。优先展示元数据和填充率，正文展开查看。每批最多 2000 个文件，大组自动拆批；文件多时预览只在均匀抽取的 8 个文件上取样；确认后登记和排队验证在同一事务完成，按请求标识重试复用结果。部分批次已成功时保留其编号，继续提交剩余部分。

操作人可在设置中保存或在向导内记住（当前浏览器）；补充备注选填，常规动作自动生成审计说明。LLM 建议须点击“确认采用此映射”；登记记录保留请求者、模型、人工批准者，以及批准后的映射是否被编辑。

扫描仅浏览、读取服务器文件，不上传 Mac 文件，不移动原文件。隐藏条目和符号链接跳过；每次最多检查 50,000 个文件，达到上限会提示缩小范围。运行中的扫描及结果保存在服务进程内，最多保留 10 次、24 小时；服务重启后未完成扫描需重做。向导草稿及已获取结果、预览保存在当前浏览器 IndexedDB，切换页面后可继续；收藏和操作人保存在 localStorage。清空草稿不删除已登记批次。

1. **登记前预览**：按首、中、尾窗口抽样，用表格查看映射后的元数据、正文片段和抽样字段填充率。抽样不等于全量验证。JSONL/行文本支持 UTF-8、UTF-16、GB18030 字节定位；JSON 数组上限 64 MiB；TXT 展示三个片段；Parquet 取首中尾行组，单行组覆盖有限。未知元数据保留空，不推测记录 ID。
2. **确认导入**：服务核对文件大小、哈希和解析器方案，登记审计并提交验证任务。预览和登记的文件哈希仍是同步请求，大文件需要等待；“停止后续预览”在当前请求结束后停止。保持原文件位置和内容以便恢复。
3. **暂存并验证**：确认导入后自动提交 batch_stage；列表和详情提供任务入口，未完成批次可继续处理。验证通过停在 validated，界面为“已验证 · 待确认发布”；未通过为 needs_review，先查失败记录和方案。
4. **发布前预览**：回到批次预览，检查新增/更新/不变/失败的篇数与字数、质量、字数/年代分布、库内 text_hash 完全相同篇数；分页看暂存元数据、正文和清洗差异。失败字数未知时显示未知。正文最多展示 2000 字，差异每侧最多 10000 字并标截断。
5. **确认发布或放弃**：确认操作人，可补充备注，确认发布提交 batch_publish；只接受 validated。放弃清理未发布暂存分区并保留审计。预览只读暂存，不写已发布版本。

近重复与段落重复在发布后作为后续任务计算。查重失败只记在后续任务上，不回滚批次；重试失败的查重任务即可，重试已发布批次不会重复发布，结果提示另做 dedup。CLI `batch-resume` 和 `ingest` 默认就地一键处理到发布，并在同一进程接着跑完查重，不停在确认页；加 `--queue` 则交给 worker。

“使用 LLM 辅助映射”只对规则未匹配的本组首个文件调用已配置 LLM，需主动选择；仅发送受限内容样本，不发送绝对路径或完整文件。建议必须由人确认。支持 TXT/JSON/JSONL/Parquet/行文本，未知作者、年代、许可证不补猜。Gutenberg 书目关联、Erya 翻译配对、PDF/OCR、SFT 结构仍需后续支持。配置方法见 [LLM 指南](llm.md)。

### 自动识别与映射方案

扫描时每个文件依次尝试，前三级算已识别，后两级只是草稿，必须人工确认：

1. **内置解析器**：`ccc_corpus`、`wanli`，按字段结构匹配。
2. **已确认过的方案**：以前批次保存的映射方案。结构化文件按字段匹配，TXT 按文件名规则或所在目录匹配，命中后注明沿用哪个批次。所以一个新来源只需确认一次。
3. **内置来源方案**：`ingest/profiles.py` 里的声明式方案，覆盖明清文集、Wikisource、greathangpt、chinese-poetry、殆知阁、团队文本、Gutenberg、《东方杂志》和《申报》OCR。同一文件还列出非正文文件的跳过规则（说明文件、作者简介、清单、重复分片，以及暂不入库的 Erya），扫描结果显示为“非正文 · 已跳过”。
4. **规则推断**：按字段名同义词和取值挑出正文、ID、标题、作者、时代和分类；TXT 文件名按下划线分段且含已知朝代时起草文件名规则。
5. **LLM 建议**：主动点击才调用，输入是文件名、有限样本和规则草稿。

映射方案除正文、ID 和字段映射外，还可在“高级设置 · 映射 JSON”里填写：

| 项 | 作用 |
|---|---|
| 字段路径 | 嵌套用点分隔；`a\|b` 取第一个非空项；正文字段是对象数组时逐层展开成多篇，子记录用 `_parent.x` 引用上层字段 |
| `fields` | 可映射 `title`、`authors`、`composed_label`、`category`、`work_title` |
| `path_pattern` | 对文件路径搜索的正则，命名组即字段名；`year` 组写入年份；其他组名记入附加信息 |
| `head_patterns` | 对正文前 4000 字搜索的正则数组，命名组同上 |
| `trim_start`、`trim_end` | 去掉固定的头尾声明：正文从第一次匹配之后开始，到其后第一次匹配之前结束 |
| `accept` | 字段值必须整体匹配的正则，不匹配按未知处理；用于来源字段里混有无关取值的情况 |
| `title_format` | 标题模板，如 `{work_title} {date}`；可用记录字段、路径命名组和已得到的元数据 |
| `constants` | 整批相同的值，限 `license`、`composed_label`、`category`、`unit_type`、`work_title`；由确认方案的人负责 |

来源用来表示“不知道”的占位词（佚名、无名氏、群星、不详、未知、未分类、Unknown、Anonymous）一律按未知处理，原值仍保留在原始字段里。

元数据的依据分别记为 `source`（记录字段）、`path`（文件名）、`text`（正文开头）和 `import_plan`（常量）。同一字段多处都有值时，记录字段优先，其次正文开头、文件名、常量。

## 数据集工作台（M2–M5、M7、M8）

导航“数据集”列表显示名称、创建时间、总字数、各划分字数；有精确统计才显示 token 数。新建向导四步可来回切换：

1. **选语料**：来源、类别、体裁、质量多选并显示篇数/字数，年代沿用时代/世纪/区间控件。可增加 OR 条件组、排除组，按 doc_id 手动增删。版本选“主线”或“simplified”；后者必须先通过 CLI `reprocess t2s-v1` 预览、加 --apply 生成，缺失/过期报错，不回退。
2. **去重和划分**：none/document/span，来源优先级可原生拖动或按钮排序；设置比例、按字数/篇数、分层、val/test 上限、保留集。span 需要两类查重最新快照，设置最短裁剪字数。整组不拆，超大组可能使比例偏离，保留集也受上限约束。
3. **只读试算**：看候选、舍弃、实际比例及来源/类别/世纪分布，处理醒目警告。条件变化后防抖刷新，旧请求取消；普通试算只读元数据，span 需相关正文映射偏移。可填写后端 tokenizer.json 路径；无缓存为未知，部分覆盖明确显示“估算”和覆盖率，不会写成精确统计。
4. **确认发布**：核对名称、填写操作人和依据，有最新试算结果后提交 dataset_create 并跳任务中心。成功结果给出 release_id。该操作只固定成员，不自动导出文件。

可保存/载入/删除草稿配方，或从已有数据集复制配方。草稿修改不改变旧发布。重试同名同配方创建任务可复用已固定发布；新一次选材应换新名称。

### 数据集详情与导出

详情显示只读配方、固定成员摘要和字符数；后续入库、转换、查重、否决不改变它。

- **Token 统计**：填后端分词器路径、操作人/依据，提交 token_stats，完成后回详情看总量、分划分/来源/类别和分位数。统计按分词器 JSON 指纹区分，完整正文计数缓存可跨发布复用，裁剪正文直接计算。
- **校验**：提交 dataset_verify；默认核对固定成员摘要/字符数、已完成文件存在/大小/行数，选哈希校验才重读文件核对 SHA-256。
- **导出**：选择 parquet/jsonl/pkl、字段、document/sample、分片大小；sample 设置 max_chars/overlap_chars，JSONL 可选 zst。提交 dataset_export，成功后历史展示 manifest 和文件清单。PKL 只加载可信文件。多次导出使用相同固定成员，每次独立 export_id。详细参数见 [导出说明](user-guide.md#按已有发布多次导出)。
- **下载到本地**：按详情实际路径复制 rsync 模板，将 USER@HOST 替换为 SSH 连接信息，在自己的电脑执行；API 不接受输出路径，导出根由后端 MIDSTREAM_EXPORT_ROOT 决定。
- **登记训练**：填运行名、操作人、依据；仅登记使用，不启动训练。后续发布会据此检查 val/test 是否泄漏。

Token 二进制分片目前用 CLI `midstream tokenize <release_id> --tokenizer <path>` 或通用 jobs API 提交，结果和文件仍可从导出历史查看。词表不超过 65536 用 u16，否则 u32；只支持 document 单位，不自动加 BOS/EOS，详见 [token 口径](user-guide.md#8a-token-统计与分片)。

## 重复浏览与人工否决（M9）

“重复”页按完全/近重复、成员数和来源分页筛选，进入簇看成员、选两篇比较服务端限长差异（每侧最多 20000 字）。填写操作人/依据可提交文档或段落查重任务。

可否决两篇文档的重复关系，或某成员与该簇内其他成员的关系；界面保存否决后提交重算，等待完成再新建数据集。按 doc_id 对持久保存、记审计，重算和主线更新后仍生效。A/B 否决优先于 A~C~B 的传递合并，C 确定归入一侧，可能放弃部分正边；真实作品关系仍要求同一划分。已发布数据集不变。

文档面板正文可叠加最多 200 条重复高亮，不生成新版本。偏移为 Unicode 码点，过期派生版不套用当前主线坐标；当前逐字等长 simplified 可复用有效偏移。

## 备份设置（M10）

“设置”保留 LLM 配置，并提供阿里云 OSS 备份：endpoint、bucket、前缀、凭据、周期、保留份数和可选配置文件。先保存配置，再测试连接；测试连接会访问实际 OSS。留空凭据保留本地值，清除仅影响本地值，环境变量仍优先，接口不回显。

默认 schedule=none，绝不自动备份；手动启用 daily/weekly 后由 worker 检查并排队，没有近期任务时可能立即提交一次。填写操作人/依据可手动备份、查看历史、校验，均跳任务中心。备份流式上传 pg_dump -Fc，不在本地落完整 dump；可选配置文件可能包含本地凭据，不包括数据集导出目录。

校验使用 pg_restore --list，仅证明归档目录可读，不等同于完整恢复验证。恢复只提供 CLI，目标必须是不存在的新库，不能覆盖 midstream、源库或系统库。使用内置 PostgreSQL 工具；真实 OSS 联调尚未进行。配置示例、保留策略和恢复命令见 [备份指南](user-guide.md#8d-oss-备份m10)。

## 文档、统计与人工管理

文献页支持标题前缀、作者、来源、类别、体裁、质量和成书年份筛选，游标分页。默认看元数据，正文按需加载，每次 10000 字；超大单篇目前仍整篇解压再切片。版本列表显示 variant/过期提示，可切历史版查看，历史正文和元数据只读。

时代筛选按已收录标签，世纪/年份用成书区间相交规则；可包括未知年份，不猜测未知值。跨世纪区间在每个相交桶计数，因此分桶篇数合计可超过独立文档数。万历事件年月不等于成书年份。统计页显示当前主线文档/字数/质量、来源/类别、年代、存储和最近发布。

人工修正先预览，填写操作人/依据后追加新版本；可确认/拒绝作品关联、查审计。主线更新后此前 simplified 自动过期，需要重新生成才能在新发布中选用；已有发布的固定版本不受影响。

## HTTP 调用与接口索引

所有地址前缀为 /api/v1。写操作使用 `X-Midstream-Action: 1`，发送 Origin 时必须同源；无跨站 CORS。Host 默认限 localhost/loopback，可用 MIDSTREAM_ALLOWED_HOSTS 调整。此边界不等于登录认证。查询连接只读，写入复用 core；数据库不可用返回 503，不存在返回 404，参数错误返回 422，状态冲突通常为 409。完整模型以运行中的 /api/docs 为准。

例如先查看可执行的操作，再提交后台查重，用实际返回的 job_id 查询；这里仅为命令示例：

```bash
curl -sS http://127.0.0.1:8765/api/v1/operations
curl -sS http://127.0.0.1:8765/api/v1/jobs   -H 'Content-Type: application/json' -H 'X-Midstream-Action: 1'   -d '{"kind":"dedup","params":{},"actor":"operator","reason":"更新查重快照"}'
curl -sS http://127.0.0.1:8765/api/v1/jobs/123
```

| 范围 | 接口（省略 /api/v1） | 参数或行为 |
|---|---|---|
| 操作 | GET /operations | 已注册操作的名称、说明和参数 JSON Schema，与 `midstream operations` 相同 |
| 任务 | GET/POST /jobs；GET /jobs/{id}；POST /jobs/{id}/cancel、/retry | POST 含 kind、params、actor、reason，kind 可为任一已注册操作；取消/重试含 actor/reason |
| 入库 | GET /import/adapters；POST /import/browse、/scans、/detect、/preview、/register、/start | browse 为目录分页；scans 异步扫描，GET /import/scans/{id}?offset=N 读取结果，POST /import/scans/{id}/cancel 取消；start 原子登记并排队验证，request_token 保证重试复用；保留 register 兼容接口 |
| 批次 | GET /batches、/batches/{id}；POST /batches/{id}/stage、/publish、/resume、/abandon | stage/publish 返回任务；resume 在请求内就地执行（含查重后续任务）并返回结果；操作需 actor/reason |
| 暂存预览 | GET /batches/{id}/preview、/records、/records/{record_id}/diff | records 按 status=new/updated/unchanged/failed、after/limit 分页；只读暂存 |
| 选材 | GET /datasets/facets；POST /datasets/preview | facets 用 filters 或 recipe JSON 查询字符串，含来源/类别/体裁/时代/质量篇数与字数；preview 用 recipe，可选 tokenizer |
| 数据集 | GET/POST /datasets；GET /datasets/{id} | 创建体为 recipe、actor、reason；返回 dataset_create 任务；列表含已有 token 统计 |
| 配方草稿 | GET/POST /dataset-recipes；GET/PUT/DELETE /dataset-recipes/{name} | POST/PUT 为 recipe、actor、reason；DELETE 含 actor/reason；不改旧发布 |
| 导出 | GET/POST /datasets/{id}/exports | POST 为 options、actor、reason；返回 dataset_export；GET 含状态/manifest/文件 |
| Token 统计 | GET/POST /datasets/{id}/token-stats | POST 为 tokenizer、actor、reason；GET 是该发布精确统计 |
| 校验/训练 | POST /datasets/{id}/verify；GET/POST /datasets/{id}/training | verify 可选 hash_files；training 含 run_name、actor、reason |
| 重复簇 | GET /duplicates/clusters、/duplicates/clusters/{id}、/duplicates/compare | 列表 before/kind/source/min_size/max_size/limit；成员 after/limit；比较 left/right/length |
| 人工否决 | GET/POST /duplicates/overrides | POST 含 left_doc_id 和 right_doc_id 或 cluster_id，加 actor/reason |
| 备份 | GET/POST /backup/settings；POST /backup/test；GET/POST /backups；POST /backups/{id}/verify | settings 含 config/keys/clear_keys/revision；测试已保存连接；提交任务需 actor/reason |
| 文档 | GET /documents、/documents/{id}/metadata、/versions、/spans | 正文 GET /documents/{id}?version=2&offset=0&length=10000，返回 next_offset；不传 version 取当前版 |
| 审核/统计 | POST /documents/{id}/review、/works/review；GET /documents/{id}/works、/events、/sources、/stats、/years/options、/stats/years | review 需 expected_version_id、metadata、actor/reason；events 按 doc_id 或 batch_id |

通用 jobs 接受 GET /operations 列出的任一 kind；每种均加 actor/reason。params 按该操作的 JSON Schema 严格校验：未知字段、类型不符（如 "123" 代替 123）返回 422。当前操作：

| kind | params |
|---|---|
| ingest | {"source":"wanli","paths":["/path/to/file.jsonl"]}；paths 省略时用 MIDSTREAM_JISHUI_RAW 下的默认文件 |
| reprocess | {"rule":"t2s-v1","apply":false}；apply 为 false 只预览 |
| batch_resume / batch_stage / batch_publish | {"batch_id":123} |
| dedup / dedup_spans | {} 或 {"full":true} |
| dataset_create | {"recipe":{...}} |
| dataset_export | {"release_id":1,"options":{"format":"jsonl","compression":"zst","unit":"sample","max_chars":8192,"overlap_chars":256,"shard_mb":256}} |
| dataset_verify | {"release_id":1,"hash_files":false} |
| token_stats / tokenize | {"release_id":1,"tokenizer":"/path/to/tokenizer.json"}；tokenize 可加 shard_tokens |
| backup / backup_verify | {} / {"backup_id":1} |

## 常见问题与验证范围

- **任务一直排队**：确认用 `midstream serve` 启动（未加 --no-worker），或 worker 已在同一数据库运行，不要只启动 API 或 Vite。
- **网页更新后仍旧界面**：重新 build，确认 API 的 static-dir，重启并刷新浏览器。
- **查重后续任务失败**：批次已发布，只重试失败的 dedup/dedup_spans 任务，不重新入库。发布前失败则按暂存/验证状态恢复，不会产生后续任务。
- **分词器或文件不存在**：填写后端机器的路径；浏览器电脑上的路径无法直接使用。
- **返回 403**：检查写请求头、同源 Origin 和 Host 白名单；经 Vite 代理或 SSH 隧道使用同源地址。
- **取消后仍短暂运行**：协作式取消要等阶段边界；不自动撤销已提交数据。

M1–M10 曾在隔离 PostgreSQL 合成库验收，最后一次全量为 130 通过、2 个性能用例跳过，随后备份修复和配置并发用例单独通过；前端构建通过。数据集创建、试算/估算、发布、统计、导出的合成库浏览器主流程已走过；M6–M10 未追加完整浏览器验收。尚未做正式库性能验收、真实 OSS 或真实模型 API 联调。本次文档更新不代表重新执行这些验收。


## AI 整理工作区（迁移 0018，本地待验收）

新增“数据整理”页面：库内未处理筛选、暂存批次清洗、模型/搜索配置、预算、运行记录、候选证据、可选审核与撤销。导入页面可勾选发布前完成 AI 清洗，文档档案显示每步 bool 和整理证据。完整行为及验证范围见 [整理指南](curation.md)。
