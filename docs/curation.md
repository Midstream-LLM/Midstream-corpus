# AI 辅助语料整理

本地实现，迁移 `0018`。入库清洗与库内补处理共用 `curation.pipeline`，由已有任务队列执行。部署前须在专用 PostgreSQL 测试库验证迁移及事务；本轮未部署、未对真实语料调用付费服务。

## 使用入口

**新数据：** 导入页面勾选“入库时完成 AI 清洗与标注”，选择模型和搜索 profile。任务在暂存阶段完成书目、标签、文本质检及篇章识别，再提供发布预览。还有记录未完成清洗时默认不能发布；操作人在批次页勾选确认后可以发布，这些记录以 `ai_curation=false` 入库，留到“数据整理”里补清洗，审计事件记下放行条数（CLI/API：`batch_publish` 的 `allow_uncurated`）。未启用 AI 的导入保留基础清洗流程。

**已有数据：** “数据整理”页面可选择来源、文档 ID 和数量，默认筛选 `processed_steps.ai_curation=false`。也可填写已暂存批次 ID。处理结果自动应用，原文修复单独启用；“重新处理”可显式重跑已完成记录。

每个工作项可查看候选、置信度、证据和应用状态，按需确认或否决结论。审核与应用独立：确认会登记审核，不自动提升低置信度候选或撤销已应用结果。字段纠正走文档修订；撤销整次整理会追加恢复版本，要求当前版本仍是该次结果，以保留后续修订。

## 配置与调用

沿用 `llm.toml` 的凭据、endpoint 和 profile，不另存密钥。服务默认值支持：

```toml
[services]
generation = "default"
curation = "default"
search = "default"
```

`curation` 可选任一支持文本生成的现有 profile；`search` 须指向 DeepSeek Search API 或兼容网关。官方 DeepSeek 接入点的路径会统一到 `/v1/tools/search`、`/v1/tools/fetch`，不受生成协议影响。关闭 `use_search` 可以只用结构化查询与 LLM。

```bash
midstream run curate --params '{"filters":{"sources":["example"]},"limit":20}' --actor operator --reason '小样本整理' --queue
midstream run curate --params '{"doc_ids":[1,2],"config":{"apply":false}}' --actor operator --reason '生成候选'
midstream run curate --params '{"batch_id":12}' --actor operator --reason '暂存清洗' --queue
midstream run curate --params '{"resume_run_id":42,"resume_options":{"max_tool_calls":10}}' --actor operator --reason '增加预算并恢复' --queue
```

批次运行的恢复同时提供原 `batch_id`。恢复固定原目标及策略，`resume_options` 允许调整调用上限与 `apply`；其他策略变化使用新运行。成功返回并保存的模型/检索调用可重放；接口响应尚未保存时的中断，仍可能需要重新调用。调用分两轮：

- 书目考证和打标签：每条记录一次对话，只读开头加三个均匀片段，共 `context_chars`（默认 16,000）字；模型可请求工具，默认最多 6 次。成本与正文长短无关。
- 文本修复和篇章切分：按 `chunk_chars`（默认 12,000）逐段各调用一次模型，不联网。正文超过 `max_text_chars`（默认 40 万字）时直接记为 `curation_too_long`，不调用模型；只选前两个步骤可以处理任意长度的正文。

工具超限保留未完成状态。取消在记录及调用边界生效。

`ingest` 注册操作也支持可选 `curation` 对象；`POST /api/v1/import/start` 和 `/import/register` 使用相同配置。`midstream operations` 和 `/api/v1/curation/schema` 提供完整 JSON Schema。

## 两种检索的衔接

- 结构化查询：Wikidata 书名/人物候选与 QID 回查；Gutenberg ebook ID 的 RDF 书目。已有外部 ID 优先。Gutenberg 电子书发布日期不作为成书年份。
- 自主网页研究：模型决定 `search`、`fetch` 或再次进行结构化查询，最后返回结构化结论。Fetch 只读取已检索到的链接；工具与模型调用均受数量上限控制。
- 按查询复用七天缓存，同作品标题的不同页共享书目检索结果；各记录的调用日志另行保留，恢复不依赖缓存是否过期。

当前实现采用应用侧控制的检索循环。DeepSeek 原生服务端研究模式、CBDB/CTP 适配、作品级结论的集中存储与有依据的跨页传播属于后续扩展。

## 元数据契约

`corpus.metadata` 是暂存、版本写入、部分修改、API 和导出的共享契约；字段列表由模型产生。旧的年份数组和 PostgreSQL range 输入仍可读，新接口统一输出闭区间：

```json
{
  "composed_years": {"start_year": 800, "end_year": 810},
  "edition_years": null,
  "contributors": [{"name": "示例作者", "role": "author", "entity_id": null}],
  "tags": [{"taxonomy": "genre", "tag_id": "poetry"}],
  "external_ids": {},
  "field_claims": {},
  "processed_steps": {"basic_clean": true, "metadata_enrichment": true, "tagging": true,
    "text_cleanup": true, "segmentation": true, "ai_curation": true, "t2s": false}
}
```

年份未知为 `null`，无零年；单侧边界可空。责任者支持 author/compiler/commentator/translator/editor，`authors` 作为 author 角色的兼容投影同步维护。层级书目分类、文体、主题、语体使用分开的 taxonomy 与稳定 tag_id；词牌单列 `tune_pattern`。现有 `genre/category/topics` 保留作来源兼容字段，多标签以 `tags` 表达。原始数据继续存放在 `source_record.raw_fields`。

布尔标记表达已完成的处理历史，普通后续修订继承标记。缺键等价于 false；失败、仅生成候选和部分步骤未完成，不将完整 AI 整理标记设为 true。正常完成但无可确认的修改可以保留未知，并标记完成。低置信度与冲突候选保存为 withheld/conflict；完成的是考证与决策流程。人工审核是否完成独立记录。

`record_operation` 与批量登记入口为基础清洗、规则处理、繁转简、人工修订、AI 整理记录操作；正文不变时复用 blob。入库重遇原始记录未变的内容，会复用当前整理版，保留已有人工修订；发布前再次检查其版本。

## 证据与版本

- `curation_item` 固定输入、步骤、摘要、状态与输出；已入库目标直接引用不可变版本，暂存输入保留元数据快照及去重后的正文 blob。
- `curation_call` 保存已完成的外部调用；不保存凭据或模型隐藏推理。
- `curation_claim` 存字段/补丁/篇章候选、模型自评置信度、简短理由、应用状态及独立审核状态。当前置信度类型为 `model_self_reported`，没有把它标作校准概率。
- `curation_evidence` 按摘要去重；claim/evidence 为多对多关系。`field_claims` 让版本字段直接指向其结论记录，人工改变该字段时清除旧关联。
- `curation_review_event` 保留每次审核；确认模型结论保留其 `source_kind=llm`。
- `document_segment` 保存版本内 Unicode 字符区间和 claim ID；原文补丁逐字核对，重叠补丁作为冲突保留。

元数据与运行列表不解压正文，证据按需读取。配方 Filters 支持 `processed_steps`；文档列表支持 `ai_curated`。JSONL 导出保留对象/数组/bool，Parquet 使用 struct/list/map 类型。可选字段包括年份区间、处理标记、责任者、标签、外部 ID 和来源依据。

## 验证范围

新增测试覆盖强类型、日期保真、证据引用、查询→搜索→回查、调用预算、Unicode 补丁、冲突策略和 Parquet 类型。数据库测试覆盖自动应用、可选审核、恢复、撤销、并发版本冲突、暂存发布绑定和 bool 约束，需要专用 `MIDSTREAM_TEST_DSN`。

本轮未进行真实数据库、真实搜索/模型和浏览器点击验收。篇章识别目前限单记录内完整区间；跨页重建、扫描影像 OCR、基于人工 Gold Set 的置信度校准及质量准确率评估尚未实现。数据库迁移和这些能力的边界应在扩大运行范围前分别验收。

本轮最终检查：`pytest -q` 为 **106 passed / 69 skipped**（69 项依赖专用 PostgreSQL，其中 9 项为新增整理事务测试）；TypeScript/Vite 构建通过，Python 编译与 `git diff --check` 通过。外部 API 使用模拟响应验证，未计入真实调用准确率。
