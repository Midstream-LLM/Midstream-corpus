# Web app：元数据、统计与管理

## 备份设置（M10）

导航“设置”保留 LLM 配置，并增加阿里云 OSS 备份一节：endpoint、bucket、前缀、凭据、周期、保留份数、可选配置文件。留空凭据保留本地值，清除仅影响本地值，环境变量仍优先。默认不自动备份。测试连接针对已保存配置；手动备份和历史校验填写操作人/依据，跳转任务中心。

GET/POST /api/v1/backup/settings 共用 backup.toml，POST 为 config、keys、clear_keys、revision；POST /backup/test 测试已保存连接。GET/POST /backups 查看历史/提交 backup 任务；POST /backups/{id}/verify 提交 backup_verify。写操作都要求 browser_write，任务写操作使用 actor/reason。恢复仅提供 CLI，且只允许全新库。详见 [使用指南](user-guide.md#8d-oss-备份m10)。

最终全量 130 项通过、2 个已另验收性能例跳过，构建通过；之后备份键唯一性/调度连接小修，策略测试通过，返回值解包修复后主流程定向通过。OSS 全是假传输，真实 PostgreSQL 工具在指定隔离容器运行；无真实 OSS、正式库或部署，本轮未新增备份浏览器验收。另新增配置并发替换防覆盖用例，单独通过，未重复全量。

## 重复浏览（M9）

导航“重复”可按完全/近重复、成员数和来源分页查看文档簇，分页读成员元数据、选两篇做服务端限长差异。可提交文档/段落查重任务；文档正文页可叠加最多 200 条重复高亮，不生成版本，补充平面字符按 Unicode 码点坐标显示。

GET /api/v1/duplicates/clusters 支持 before/kind/source/min_size/max_size/limit；/{id} 支持成员 after/limit；/duplicates/compare 接受 left/right/length（最多每侧 20000 字）。GET /documents/{id}/spans 返回两侧映射后的原文坐标，可指定 version；过期派生版不叠加当前坐标。

POST /duplicates/overrides 使用 left_doc_id、right_doc_id 或 cluster_id，加 actor/reason；指定簇时否决该成员与簇内每个其他成员。按文档对保存，写 management_event 审计。界面否决后提交重算任务；否决使快照过期，完成重算前不能继续按旧簇发布选用/划分。

负约束优先：在有已验证正边的前提下，按固定哈希/文档顺序合并；A~C~B 但 A/B 被否决时，C 可确定地归到其中一侧，不能把两侧重新连起来，部分允许的合并会因此放弃。真实作品关联仍强制同一划分，否决重复关系不豁免作品泄漏隔离。段落裁剪也跳过被否决的文档对。重算不修改已经固定的发布。

新增 3 项测试，全量 127 通过、2 个性能例跳过，构建通过。覆盖否决重算/版本更新持久性、传递边、导出、审计和双侧原文 Unicode 坐标；未新增浏览器流程或部署。

## 导入两层预览（M6）

登记前按首、中、尾窗口取样，展示映射字段与抽样填充率表格。JSONL/行文本按字节定位（支持 UTF-8、UTF-16、GB18030）；JSON 数组扫描顶层分隔，仅解析保留的记录，沿用 64 MiB 上限；TXT 为三个正文片段。Parquet 按首、中、尾行组取样，单行组时样本覆盖有限。抽样不等于全量校验；缺显式来源 ID 的行样本不猜测真实记录序号。

登记后点击“暂存并验证”，提交 batch_stage，成功停在 validated（界面“已验证 · 待确认发布”）。查看新增/更新/不变/失败、字符与质量/年代分布、库内完全相同正文，以及分页暂存元数据和正文/清洗差异后，填写操作人、依据，点击“确认发布”，提交 batch_publish。后者只接受 validated；重试已发布批次不会重复发布。近重复和段落重复仍在发布后计算。

GET /api/v1/batches/{id}/preview 只读暂存并关联已发布记录哈希用于分类；失败记录无字符数时返回 null。GET /batches/{id}/records 支持 status、after、limit，正文最多展示 2000 字；GET /batches/{id}/records/{record_id}/diff 最多对比各侧 10000 字并标记截断。POST /batches/{id}/stage、/publish 使用 actor/reason 和 browser_write。CLI batch-resume 保留一键跑完行为；放弃沿用 abandon。

新增 3 项测试。全量运行 117 通过/1 失败/1 性能例跳过；编码抽样失败修复后，原映射恢复和扩展多编码抽样定向 2 项通过。构建通过；本轮未追加浏览器验收，未部署。

前端为 React + TypeScript + Vite，后端为 FastAPI，直接调用现有 Python core。数据库和后端长期留在校园服务器，浏览器与未来 SwiftUI 远程客户端共用 `/api/v1`，经 SSH 隧道访问。目前未实现 macOS 打包或进程托管。

## 已提供

- 文献列表、游标分页；标题前缀、作者、来源、部类、质量与成书年份筛选。
- 文档详情、历史版本列表和切换；保留原文空白、标点及异体字。
- 正文每段最多 20,000 字符，UI 每次读取 10,000 字符。当前后端仍需解压整篇后切片，超大单篇的解压优化待做。
- 加载、空结果、连接失败与重试状态；桌面双栏，窄屏上下排列。
- 元数据为文档默认视图，正文按需加载；展示字段依据、版本关系和关联来源记录。
- 人工修正提供变更预览，填写操作人和依据后追加新版本；支持作品关联确认/拒绝与审计查看。
- 统计概览展示当前文档/字符/质量、来源与部类分布、存储和最近50次数据集发布。
- 批次支持分页查看、详情、原文件校验信息、失败样本、恢复/放弃和操作记录。
- 数据集制作与 LLM 操作面板后续接入。

## 运行

在代码仓库根目录、已初始化好数据库的环境中：

```bash
source .venv/bin/activate
pip install -r requirements.txt
npm --prefix web ci
npm --prefix web run build
python -m midstream.api --port 8765
```

打开 `http://127.0.0.1:8765`。默认使用现有私有 PostgreSQL；可用 `MIDSTREAM_DSN` 指向其他实例。构建后由 Python 托管 `web/dist`，运行阶段不需要 Node 服务。安装到其他位置时可用 `--static-dir /path/to/web/dist` 指定前端构建目录。

Linux 部署时可从客户端建立 SSH 隧道，将本机端口转发到服务器的 `127.0.0.1:8765` 后访问。默认只监听 loopback，当前没有登录或多用户权限系统。此次开发没有部署到正式服务器。

开发时，先运行 API，再运行 `npm --prefix web run dev`；Vite 将 `/api` 代理到 `127.0.0.1:8765`。前端不存数据库地址或 LLM key。

## HTTP 接口

- `GET /api/v1/health`：服务版本和存活状态，不代表数据库可用。
- `GET /api/v1/sources`：来源名列表。
- `GET /api/v1/documents`：筛选当前版本，返回 `items` 和 `next_cursor`；下页传 `after`。默认每页 30，最多 100。
- `GET /api/v1/documents/{doc_id}/versions`：全部历史版本摘要，倒序。
- `GET /api/v1/documents/{doc_id}?version=2&offset=0&length=10000`：指定版本及正文分段，不传 version 时取当前版。
- `/api/docs` 和 `/api/openapi.json`：接口文档。

年代筛选沿用 core 的成书年份区间相交规则；未记录年代的文档不会命中指定年份区间。未知元数据显示为未知。正文按 Python Unicode 码点切片；API 会返回 `next_offset`，无后续文本时为 null。长文本调用方宜固定 version，避免多次请求之间当前版本变化。

查询连接使用只读事务，写操作使用独立连接并复用 core；每次请求后释放。数据库不可用返回 503，查不到文档或版本返回 404，参数无效返回 422。当前无全文检索，标题搜索仅为前缀匹配。

## 验证

本地独立 PostgreSQL 17 合成库中 66 个 Python 测试通过，覆盖真实版本与元数据读取、写入和审计、冲突、批次恢复/放弃以及年份分组；前端 TypeScript 检查和生产构建通过。实际浏览器验证了元数据默认视图、变更预览与新版本保存、审计记录、统计页面、批次恢复、世纪范围查询和包含未知年份。尚未在正式库上验证性能。

使用 FastAPI（MIT）、Uvicorn（BSD-3-Clause）、React/Vite（MIT）、TypeScript（Apache-2.0）。构建依赖在 `web/package-lock.json` 中固定。

## 持久任务与 worker

先在运行后端的机器上执行迁移 `0010`（私有实例用 `midstream init`，外部实例用 `alembic upgrade head`）。worker **必须另外手动启动**，API 不会代为启动：

```bash
midstream worker
midstream jobs list --limit 30
midstream jobs show 123
midstream jobs cancel 123 --actor operator --reason '停止本次处理'
midstream jobs retry 123 --actor operator --reason '原因已修复，按原参数重试'
```

worker 与 API 使用同一 `MIDSTREAM_DSN` 或私有数据库配置。数据库锁限制为一份 worker，同一时间只执行一个任务，按编号取队列；重复启动会退出。任务开始后每 5 秒写一次独立心跳。启动时及后续取任务前，将超过 60 秒没有心跳的 running 任务设为 failed，`error=interrupted`，不会自动重试。刚中断后立即重启，可能需等到旧心跳超过 60 秒。

批次页“继续处理 / 发布”现在提交 `batch_resume` 任务并跳到 `#jobs/{id}`。任务中心显示列表、阶段进度、结果及最近 100 条日志，排队或运行时每 2 秒刷新；关闭页面不会停止任务。取消和重试均需填写操作人、依据，记录在任务事件中。重试只接受 failed/cancelled，保留旧任务，按原 kind/params 新建任务。

取消排队任务立即生效；运行中仅在下一阶段开始前检查，当前阶段不会强行中断，也不回滚已提交阶段。最后阶段完成前发出的取消请求可能来不及生效。阶段进度表示阶段数，不表示预计剩余时间。批次验证未通过时任务可正常结束，结果为 `needs_review`，需继续检查批次。

批次失败沿用同步恢复逻辑：暂存失败为 failed；验证/发布中断保留此前已提交状态；发布后的查重失败仍为 published。重试已发布批次返回 `already_published` 和 `next_action=dedup`，不会重复发布，应另行提交文档与段落查重任务。原有 CLI 及 `/batches/{id}/resume` 仍同步执行。

HTTP 接口（写请求继续要求 `X-Midstream-Action: 1`，并检查同源）：

- `GET /api/v1/jobs?limit=30&before=123`：返回 `items`、`next_cursor`，按编号倒序。
- `GET /api/v1/jobs/{id}`：任务字段及 `events`，最近 100 条按发生顺序返回。
- `POST /api/v1/jobs`：`{"kind":"batch_resume","params":{"batch_id":123},"actor":"operator","reason":"继续处理"}`。白名单为 batch_resume、dedup、dedup_spans、dataset_create、dataset_export、token_stats、tokenize；两类查重 params 为 `{}` 或 `{"full":true}`，数据集/token 任务参数见下方。
- `POST /api/v1/jobs/{id}/cancel`、`POST /api/v1/jobs/{id}/retry`：请求体为 `{"actor":"operator","reason":"操作依据"}`；retry 返回新任务。

本轮在指定隔离 PostgreSQL 17 容器中全量 103 项 Python 测试通过（新增 5 项），包含真实 worker 被杀后的恢复、独立心跳、单实例限制、阶段取消与事务回滚、不重复发布；前端构建通过。浏览器走过一次合成批次提交、取消、重试至完成与日志显示；未连接正式库、部署或调用真实 LLM API。

## 数据集后端接口（M2 / M3）

先执行迁移 0011、0012。配方和导出参数见 [使用指南](user-guide.md#8-配方导出校验与训练登记)。当前提供后端能力，尚无选材/导出表单；任务中心可显示创建和导出任务。

- `GET /api/v1/datasets/facets`：`filters` 查询参数为 JSON 对象字符串，或传完整配方的 `recipe` JSON 字符串；返回当前选用条件下 sources/categories/genres/eras 各值的 documents/chars，未知值为 null，按去重前候选统计。
- `POST /api/v1/datasets/preview`：`{"recipe":{...}}`，使用只读数据库连接，返回候选/舍弃数、划分比例和分布、warnings，不写发布或文件。
- `POST /api/v1/datasets`：`{"recipe":{...},"actor":"operator","reason":"发布依据"}`，返回排队的 dataset_create 任务；完成结果包含 release_id。该任务重试时，同名同配方复用已固定发布，同名异配方报错。
- `GET /api/v1/datasets?limit=30&before=123`：发布列表及 next_cursor；`GET /api/v1/datasets/{id}` 返回固定配方、content_digest、字符数和各划分篇数/字数。
- `GET /api/v1/dataset-recipes`、`GET /api/v1/dataset-recipes/{name}`：草稿配方。`POST /api/v1/dataset-recipes` 新增，`PUT /api/v1/dataset-recipes/{name}` 修改（可改名），请求体与创建数据集相同；`DELETE /api/v1/dataset-recipes/{name}` 请求体为 actor/reason。删改草稿不影响已发布配方。
- `POST /api/v1/datasets/{id}/exports`：`{"actor":"operator","reason":"导出依据","options":{"format":"jsonl","compression":"zst","unit":"sample","max_chars":8192,"overlap_chars":256,"shard_mb":256}}`，返回 dataset_export 任务；`GET /api/v1/datasets/{id}/exports` 返回该发布全部导出状态、manifest 路径和分片记录。

创建、导出和配方写操作均使用现有 browser_write 同源保护；API 不接收输出路径，使用后端 MIDSTREAM_EXPORT_ROOT。通用 `/jobs` 的 dataset_create params 为 `{"recipe":{...}}`，dataset_export params 为 `{"release_id":123,"options":{...}}`。worker 仍需手动启动。

本轮最终隔离库全量 110 项测试通过；百万行试算用例另行验收，常规全量运行跳过。前端构建通过，仅补任务类型名称，未新增数据集前端或重做浏览器验收。

## Token 统计接口（M4）

先执行迁移 0013，安装新增依赖，另行启动 worker。分词器路径是后端机器上的本地路径，不是浏览器上传或下载地址；可给 JSON 或所在目录。

- `POST /api/v1/datasets/{id}/token-stats`：`{"tokenizer":"/path/to/tokenizer.json","actor":"operator","reason":"统计依据"}`，经 browser_write 保护，返回 token_stats 任务。
- `GET /api/v1/datasets/{id}/token-stats`：返回该固定发布的全部精确统计记录，含 unit、tokenizer_fingerprint/name、total、by_split、by_source、by_category、percentiles、created_at。unit 本轮固定 document；不同 JSON 指纹分别记录。
- `POST /api/v1/datasets/preview` 增加可选 `tokenizer`：`{"recipe":{...},"tokenizer":"/path/to/tokenizer.json"}`。只读返回 token_stats 的 exact/estimated/coverage 和 status；无缓存时为 unknown、数字为 null。估算不会写入精确表。
- 通用 `/api/v1/jobs` 的 token_stats/tokenize params 为 `{"release_id":123,"tokenizer":"/path/to/tokenizer.json"}`；tokenize 还可传 shard_tokens（默认 200000000），完成结果包含 export_id、manifest 和 total_tokens。导出记录继续从 `/datasets/{id}/exports` 查询。

任务中心增加“Token 统计”和“Token 分片”类型名称；当前没有独立 tokenizer 配置/统计表单。缓存覆盖、统计单位和特殊 token 口径见 [使用指南](user-guide.md#8a-token-统计与分片)。M4 隔离库最终全量 114 项通过、1 项性能例按设计跳过，前端构建通过；新增 4 项合成分词器验收，无真实 tokenizer/训练联调或浏览器验收。


## 数据集工作台（M5）

导航“数据集”提供发布列表和四步创建向导：多组包含/排除条件、手动文档 ID、年代、去重与来源优先级原生拖放、比例/分层/上限/保留集、只读试算和确认。条件变化后防抖 500 ms，旧请求取消；提交必须有最新试算结果、名称、操作人和依据。无 token 缓存时显示未知，部分覆盖显示“估算”和覆盖率。支持保存/载入草稿和复制已发布配方。

详情显示固定成员摘要、配方、精确 token 记录；可提交统计、校验和导出任务，选择字段、单位、分片预算，查看历史与文件清单。rsync 模板的 USER@HOST 是占位符，目录来自实际导出记录。训练登记记录运行名、操作人/依据，立即参与训练泄漏检查。

补充接口：POST /datasets/{id}/verify 请求 actor/reason、可选 hash_files，返回 dataset_verify 任务；GET/POST /datasets/{id}/training 查看/登记训练，POST 含 run_name、actor、reason。facets 增加 quality；列表附已有 token 统计，固定详情仍与动态统计分开。

本地构建通过；合成库浏览器单次主流程覆盖创建、估算、配方保存、发布、统计和导出。全量运行 114 通过、1 失败、1 性能例跳过；动态统计混入固定详情的失败已修复，该用例单独通过，未重复全量。本轮无新增依赖，无正式库/部署。

## 年份粒度与统计口径

查询可选不限、朝代/时代标签、世纪、具体年份、自定义区间、仅看年份未知。朝代来自现有 `composed_label` 值，按原标签精确匹配，不替无年份语料推算时间。世纪按 1—100、101—200 等划分；公元前1世纪为 -100 至 -1，没有公元0年。按年份查询继续使用成书区间相交语义，可以显式包含年份未知。

统计页可切换时代标签、世纪、逐年，另有未知年份数和开放边界数。时代标签分组按原记录统计；世纪与逐年按区间相交统计，同一文档可能进入多个栏位，合计不是独立文档数量。默认最多显示最近100栏并标明窗口；可指定窗口，手动请求最多500栏。元数据页将 PostgreSQL 年份区间转换为可读的包含两端年份，同时保留高级字段原始值。

新增接口：`GET /api/v1/years/options`、`GET /api/v1/stats/years?granularity=century`；documents 新增 `era`、`include_unknown_years`、`only_unknown_years`。统计可传 `year_from` / `year_to`；`dynasty` 标签统计不接受年份窗口。

## 元数据与写操作

- `GET /api/v1/documents/{id}/metadata?version=2` 独立读取元数据，不访问压缩正文。关联来源记录为文档级最近20条，每条保留批次标识；并不把所有来源记录都当作所选版本的直接输入。
- `POST /api/v1/documents/{id}/review`：`expected_version_id`、`metadata` 字段覆盖、`actor`、`reason`。历史版本只读，修正以当前版本为基础；冲突返回409，格式错误返回422。
- `GET /api/v1/documents/{id}/works` 和 `POST /api/v1/documents/{id}/works/review`：复用已有作品关联审核。
- `GET /api/v1/events?doc_id=…` 或 `batch_id=…`：最近100条操作记录。
- `GET /api/v1/stats`：语料和发布统计。源没有文档时，版本数为0。
- `GET /api/v1/batches?include_published=true&before=…`、`GET /api/v1/batches/{id}`：批次分页及详情。
- `POST /api/v1/batches/{id}/resume` / `abandon`：操作人及原因。恢复核对原文件后继续处理；已发布批次不重发。放弃仅清理未发布的暂存层并留下审计。

浏览器写请求带 `X-Midstream-Action: 1`，且只接受同源 Origin；没有配置跨站 CORS。原生客户端也需此请求头。默认 Host 白名单为 localhost/loopback（另含 TestClient 的 testserver），可通过 `MIDSTREAM_ALLOWED_HOSTS` 设置。这是本机/隧道应用的请求边界，不是多用户登录认证。

Web 暂存验证与确认发布使用持久任务队列，处理完成后查看暂存预览再确认发布。网络中断后可在任务中心查看状态；CLI batch-resume 仍同步一键处理。发布后查重失败不会回滚已经发布的数据。

### 新建入库批次

在「批次管理」展开「新建入库批次」，选择 CCC 或万历格式，填写**后端机器**上的原文件绝对路径（每行一个，最多 20 个）。目前不包含浏览器上传、目录递归或任意 JSON 字段映射。

先检查文件并预览：流式计算 SHA-256，每文件最多抽看 3 条原始解析样本、正文前 500 字及元数据；抽样不等于全量验证。填写操作人和入库依据后确认登记，服务会再次核对文件大小、哈希和解析器版本；登记与审计同一事务提交。原文件保持原位，需要保留以便继续或恢复。

登记成功自动打开批次详情，再按「继续处理 / 发布」执行 core 的暂存、验证、发布与查重。原文件已发布过会拒绝重复登记。大文件计算哈希和处理当前仍为同步请求，不提供持久后台任务；请求中断后先刷新批次状态再决定是否重试。

新增接口：`GET /api/v1/import/adapters`、`POST /api/v1/import/preview`、`POST /api/v1/import/register`。两个 POST 同样受同源写操作限制。适用于可信单机/SSH 隧道操作环境，不应直接暴露公网。

### 格式识别与智能映射

「识别已有格式」按结构和已知目录匹配已有来源；「识别并用 LLM 分析未知格式」仅对未匹配文件调用已配置 LLM。每文件最多读取 32 KiB 文件头用于文本识别，发送模型的内容样本最多 6000 字符，不发送完整文件或绝对路径。Parquet 读取一个记录样本；PDF、压缩包、token 二进制需先提取正文，不发送模型。

新增 `POST /api/v1/import/detect`：`paths`、`use_llm`（默认 false）、可选 `profile`。LLM 用途服务为 `identify_import`，配置 `[services] import_detection = "profile-name"`，默认沿用 `default` profile。结果包含识别方法、模型名及提示词版本，模型只建议字段映射，不生成或执行代码；可返回无法识别。

已知规则覆盖 CCC、万历以及现有目录中的明清文集、诗词 JSON、GreatHan/维基文库 Parquet、殆知阁/第二批 TXT、Gutenberg TXT、Erya 行文本。目录规则还检查结构，不能代替逐来源全量验收；诗词其他 JSON 结构仍可能需要不同映射。Gutenberg 书目清单关联、Erya 翻译配对、SFT 角色结构和 PDF/OCR 提取尚未实现，不把这些数据声明为完整支持。

采用方案后可编辑来源名称及映射 JSON，再预览、确认登记。同一批次一个方案，混合格式需要分批；映射支持点分隔嵌套字段、字符串数组以换行拼正文，作者字符串变列表。支持 TXT、JSON、JSONL、Parquet、逐行文本，编码支持 UTF-8、UTF-16、GB18030；编码初判需核对预览。JSON 文件上限 64 MiB，较大文件需转换为 JSONL。

迁移 `0008` 为批次增加 `import_plan` 并允许登记审计；方案内容哈希进入解析器版本，恢复从库内方案创建读取器。映射文档 ID 使用绝对文件路径加来源 ID/记录序号，文件移动会改变身份，需保留原路径。模型生成的方案经人确认后才登记；仅映射原字段，未知作者、年代和许可证保留未知，暂不做文件名自动书目解析。模型信息显示于识别结果，批次目前固定最终映射方案，不保存完整模型响应。

### LLM 配置页面

侧栏「LLM 配置」支持新增/编辑/移除凭据、协议接入点和模型 profile，配置用途默认值（生成、质检、入库识别），并调整模型参数、输出上限、超时、并发和请求速率。数据保存在后端机器，与 CLI 共用，无需数据库。

凭据既可引用环境变量，也可从密码框输入后存入后端配置文件（0600）。接口不返回密钥，留空保留已存值，支持清除本地密钥；环境变量优先。页面保存只校验配置格式及引用关系，不代表供应商实际连接或 key 已验证。冲突会拒绝覆盖，请重新加载后编辑。保存会重写 TOML 并移除原注释；具体说明见 [LLM 文档](llm.md)。

DeepSeek 快速配置支持选择协议/模型后一键填入地址、凭据引用、输出预算及参数，再检查并保存。支持用新输入或已保存的 key 从官方刷新模型列表；模型元数据只作显示，不自动扩大输出预算。预设与刷新不会覆盖已有接入点/profile，首次建立配置时会设定用途默认值。详细范围见 [LLM 说明](llm.md#deepseek-快速配置)。

Qwen 快速配置接入阿里云百炼 OpenAI 兼容 Chat Completions：选择北京/新加坡/弗吉尼亚及业务空间，自动填写区域地址、凭据引用、模型和输出预算，支持官方模型目录刷新与手工输入模型 ID。区域 key 不通用，尚未加入 Coding Plan 或 DashScope 原生协议。使用范围见 [Qwen 说明](llm.md#qwen-快速配置阿里云百炼)。
