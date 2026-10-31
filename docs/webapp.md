# Web app：元数据、统计与管理

前端为 React + TypeScript + Vite，后端为 FastAPI，直接调用现有 Python core。浏览器与未来 SwiftUI 客户端共用 `/api/v1`；未来 macOS App 可启动同一 Python 后端及私有 PostgreSQL，并连接 loopback。目前未实现 macOS 打包或进程托管。

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

批次恢复目前在请求所属服务线程执行；页面显示处理中并定期读取批次状态。若网络中断，应刷新核对实际状态，不盲目重复操作；进程终止后的恢复仍依赖 core 批次状态，不具备独立持久任务队列。发布后查重失败不会回滚已经发布的数据。

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
