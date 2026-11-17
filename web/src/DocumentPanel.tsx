import { rangeLabel, yearLabel } from "./Years";
import { savedActor } from "./importDraft";
import { stepLabels, CurationItem } from "./Curation";
import RepeatHighlight from "./RepeatHighlight";
import { useState } from "react";
import {
  api,
  useLoad,
  State,
  quality,
  show,
  count,
  date,
  Events,
  type Audit,
} from "./shared";
type Metadata = {
  doc_id: number;
  version_id: number;
  version_no: number;
  is_current: boolean;
  variant: string | null;
  variant_stale: boolean;
  parent_version_id: number | null;
  created_at: string;
  source: string;
  title: string | null;
  authors: string[] | null;
  char_count: number;
  quality_status: string;
  quality_flags: string[];
  field_basis: Record<string, unknown>;
  document: Record<string, unknown>;
  processing: Record<string, unknown>;
  source_records: Record<string, unknown>[];
  [key: string]: unknown;
};
type Version = { version_no: number; version_id: number; is_current: boolean; variant: string|null; variant_stale: boolean };
type Work = {
  work_id: number;
  canonical_title: string;
  relation: string;
  basis: string;
  status: string;
};
const labels: Record<string, string> = {
  title: "标题",
  authors: "作者",
  contributors: "责任者与角色",
  tags: "标准标签",
  tune_pattern: "词牌",
  external_ids: "外部实体",
  composed_label: "成书时代标签",
  composed_years: "成书年份区间",
  edition_years: "刊刻年份区间",
  genre: "体裁",
  category: "部类",
  topics: "主题",
  license: "许可",
  legacy_category: "历史分类",
  quality_status: "质量状态",
  quality_flags: "质量疑点",
  extra: "补充元数据",
};
const basis: Record<string, string> = {
  source: "来源记载",
  rule: "规则处理",
  human: "人工审核",
};
const ranges = ["composed_years", "edition_years"];
const common = [
  "title",
  "authors",
  "composed_years",
  "composed_label",
  "edition_years",
  "genre",
  "category",
  "topics",
  "license",
  "quality_status",
  "quality_flags",
];
type Range = { start_year: number | null; end_year: number | null };
function initial(m: Metadata) {
  const form: Record<string, string> = {};
  for (const k of common) {
    if (ranges.includes(k)) {
      const r = m[k] as Range | null;
      form[k + ".from"] = String(r?.start_year ?? "");
      form[k + ".to"] =
        r && r.end_year !== r.start_year ? String(r.end_year ?? "") : "";
    } else
      form[k] = Array.isArray(m[k])
        ? (m[k] as string[]).join("\n")
        : String(m[k] ?? "");
  }
  return form;
}
// 只填起始年表示单一年份；两格都空表示未知。
function parseRange(label: string, from: string, to: string): Range | null {
  if (!from && !to) return null;
  const a = from ? Number(from) : null,
    b = to ? Number(to) : a;
  for (const n of [a, b])
    if (n !== null && (!Number.isInteger(n) || n === 0))
      throw new Error(`${label}须为非零整数，公元前用负数`);
  if (a !== null && b !== null && a > b)
    throw new Error(`${label}的起始年不能晚于结束年`);
  return { start_year: a, end_year: b };
}
function titleYear(title: string) {
  const m = title.match(/(?<!\d)(\d{3,4})\s*年/);
  return m ? m[1] : "";
}
function Editor({
  metadata,
  onDone,
  onCancel,
}: {
  metadata: Metadata;
  onDone: () => void;
  onCancel: () => void;
}) {
  const [form, setForm] = useState(initial(metadata)),
    [advanced, setAdvanced] = useState("{}"),
    [actor, setActor] = useState(savedActor),
    [reason, setReason] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [preview, setPreview] = useState<Record<string, unknown> | null>(null);
  function changes() {
    const base = initial(metadata);
    const patch: Record<string, unknown> = {};
    for (const k of common) {
      if (ranges.includes(k)) {
        if (form[k + ".from"] !== base[k + ".from"] || form[k + ".to"] !== base[k + ".to"])
          patch[k] = parseRange(labels[k], form[k + ".from"], form[k + ".to"]);
      } else if (form[k] !== base[k])
        patch[k] = ["authors", "topics", "quality_flags"].includes(k)
          ? form[k]
              .split("\n")
              .map((s) => s.trim())
              .filter(Boolean)
          : form[k] || null;
    }
    const extra = JSON.parse(advanced);
    if (!extra || typeof extra !== "object" || Array.isArray(extra))
      throw new Error("高级字段必须是 JSON 对象");
    return { ...patch, ...extra };
  }
  async function save() {
    if (!preview) return;
    setBusy(true);
    setError("");
    try {
      await api(`documents/${metadata.doc_id}/review`, undefined, {
        expected_version_id: metadata.version_id,
        metadata: preview,
        actor,
        reason,
      });
      onDone();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  function set(values: Record<string, string>) {
    setPreview(null);
    setForm({ ...form, ...values });
  }
  const hint = titleYear(form.title);
  function yearField(k: string) {
    const from = form[k + ".from"],
      to = form[k + ".to"];
    let text = "两格都留空表示年份未知";
    try {
      const r = parseRange(labels[k], from, to);
      if (r) text = rangeLabel(r);
    } catch (e) {
      text = (e as Error).message;
    }
    return (
      <div key={k} className="year-edit" role="group" aria-label={labels[k]}>
        <span>{labels[k].replace("区间", "")}</span>
        <div>
          <input
            type="number"
            step="1"
            aria-label={labels[k] + "起始年"}
            placeholder="年份，如 2003"
            value={from}
            onChange={(e) => set({ [k + ".from"]: e.target.value })}
          />
          <i>—</i>
          <input
            type="number"
            step="1"
            aria-label={labels[k] + "结束年"}
            placeholder="结束年（单一年份留空）"
            value={to}
            onChange={(e) => set({ [k + ".to"]: e.target.value })}
          />
        </div>
        <small>
          {text}
          {k === "composed_years" && hint && hint !== from && (
            <button
              type="button"
              className="link"
              onClick={() => set({ [k + ".from"]: hint, [k + ".to"]: "" })}
            >
              填入标题中的{yearLabel(Number(hint))}
            </button>
          )}
        </small>
      </div>
    );
  }
  return (
    <div className="editor">
      <h3>修正元数据 · 基于 v{metadata.version_no}</h3>
      <p>未修改的字段保持原值；提交后生成新版本。</p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          setError("");
          try {
            const p = changes();
            if (!Object.keys(p).length) throw new Error("请先修改至少一个字段");
            setPreview(p);
          } catch (e) {
            setError((e as Error).message);
          }
        }}
      >
        <div className="edit-grid">
          {common.map((k) => ranges.includes(k) ? yearField(k) : (
            <label key={k}>
              {labels[k]}
              {k === "quality_status" ? (
                <select
                  value={form[k]}
                  onChange={(e) => {
                    setPreview(null);
                    setForm({ ...form, [k]: e.target.value });
                  }}
                >
                  {Object.entries(quality).map(([k, v]) => (
                    <option key={k} value={k}>
                      {v}
                    </option>
                  ))}
                </select>
              ) : ["authors", "topics", "quality_flags"].includes(k) ? (
                <textarea
                  rows={2}
                  placeholder="每行一项"
                  value={form[k]}
                  onChange={(e) => {
                    setPreview(null);
                    setForm({ ...form, [k]: e.target.value });
                  }}
                />
              ) : (
                <input
                  value={form[k]}
                  onChange={(e) => {
                    setPreview(null);
                    setForm({ ...form, [k]: e.target.value });
                  }}
                />
              )}
            </label>
          ))}
        </div>
        <details>
          <summary>高级字段修正</summary>
          <p>
            以 JSON 补充历史分类、extra 等字段，或只知一端的年份区间。例如{" "}
            {`{"composed_years":{"start_year":618,"end_year":null}}`} 表示 618 年以后；此处的值优先于上方表单。
          </p>
          <textarea
            aria-label="高级元数据 JSON"
            rows={4}
            value={advanced}
            onChange={(e) => {
              setAdvanced(e.target.value);
              setPreview(null);
            }}
          />
        </details>
        <div className="edit-grid">
          <label>
            操作人
            <input
              required
              value={actor}
              onChange={(e) => setActor(e.target.value)}
            />
          </label>
          <label>
            修改依据
            <input
              required
              value={reason}
              onChange={(e) => setReason(e.target.value)}
            />
          </label>
        </div>
        {error && (
          <p role="alert" className="error">
            {error}
          </p>
        )}
        <div className="actions">
          {error && <button type="button" disabled={busy} onClick={onDone}>重新载入当前版（放弃草稿）</button>}
            <button type="button" disabled={busy} onClick={onCancel}>
            取消
          </button>
          <button disabled={busy || !actor.trim() || !reason.trim()}>
            预览变更
          </button>
        </div>
      </form>
      {preview && (
        <div className="change-preview">
          <h4>即将修改 {Object.keys(preview).length} 个字段</h4>
          <table>
            <thead>
              <tr>
                <th>字段</th>
                <th>原值</th>
                <th>新值</th>
              </tr>
            </thead>
            <tbody>
              {Object.entries(preview).map(([k, v]) => (
                <tr key={k}>
                  <td>{labels[k] || k}</td>
                  <td>{ranges.includes(k) ? rangeLabel(metadata[k]) : show(metadata[k])}</td>
                  <td>{ranges.includes(k) ? (v ? rangeLabel(v) : "未知") : show(v)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <button
            className="primary"
            disabled={busy || !actor.trim() || !reason.trim()}
            onClick={save}
          >
            {busy ? "正在保存…" : "确认并生成新版本"}
          </button>
        </div>
      )}
    </div>
  );
}
export default function DocumentPanel({
  id,
  onClose,
  onChanged,
}: {
  id: number;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [version, setVersion] = useState(""),
    [tab, setTab] = useState("metadata"),
    [tick, setTick] = useState(0),
    [offset, setOffset] = useState(0),
    [editing, setEditing] = useState(false),
    [actor, setActor] = useState(""),
    [reason, setReason] = useState(""),
    [operation, setOperation] = useState(""),
    [busy, setBusy] = useState(false);
  const meta = useLoad<Metadata>(
      `documents/${id}/metadata${version ? "?version=" + version : ""}`,
      tick,
    ),
    versions = useLoad<Version[]>(`documents/${id}/versions`, tick),
    works = useLoad<Work[]>(
      tab === "works" ? `documents/${id}/works` : null,
      tick,
    ),
    events = useLoad<Audit[]>(
      tab === "events" ? `events?doc_id=${id}` : null,
      tick,
    );
  const text = useLoad<{ text: string; next_offset: number | null }>(
    tab === "text" && meta.data
      ? `documents/${id}?version=${meta.data.version_no}&offset=${offset}`
      : null,
    tick,
  );
  const [highlight,setHighlight]=useState(false);
  const spans=useLoad<{items:{start:number;end:number}[];message:string|null}>(
    highlight&&tab==="text"&&meta.data?"documents/"+id+"/spans?version="+meta.data.version_no:null,tick);
  function refresh() {
    setEditing(false);
    setVersion("");
    setTick((t) => t + 1);
    setOffset(0);
    onChanged();
  }
  async function decide(work: Work, decision: string) {
    setBusy(true);
    setOperation("");
    try {
      await api(`documents/${id}/works/review`, undefined, {
        ...work,
        canonical_title: undefined,
        basis: undefined,
        status: undefined,
        decision,
        expected_status: work.status,
        actor,
        reason,
      });
      setOperation("关联审核已保存");
      setTick((t) => t + 1);
    } catch (e) {
      setOperation((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const m = meta.data;
  return (
    <section className="reader metadata-reader">
      <div className="panel-heading">
        <h2>文档档案</h2>
        <button className="subtle" onClick={onClose}>
          关闭
        </button>
      </div>
      <div className="version-bar">
        <label>
          版本
          <select
            aria-label="查看版本"
            value={version}
            onChange={(e) => {
              setVersion(e.target.value);
              setOffset(0);
              setEditing(false);
            }}
          >
            <option value="">当前主线版本</option>
            {versions.data?.map((v) => (
              <option key={v.version_id} value={v.version_no}>
                v{v.version_no}
                {v.is_current ? " · 当前" : ""}
                {v.variant ? " · 简体派生版" + (v.variant_stale ? "（已过期）" : "") : ""}
              </option>
            ))}
          </select>
        </label>
        <span>文档 #{id}</span>
      </div>
      {versions.error && <p className="error">{versions.error}</p>}
      <State loading={meta.loading} error={meta.error} />
      {m && (
        <>
          <div className="doc-heading">
            <span className={"badge " + m.quality_status}>
              {quality[m.quality_status]}
            </span>
            <h2>{m.title || "未题名"}</h2>
            <p>
              {m.source} · {count(m.char_count)} 字符 · v{m.version_no}
              {m.is_current ? " · 当前版" : " · 历史版"}
              {m.variant ? " · 简体派生版" + (m.variant_stale ? "（已过期）" : "") : ""}
            </p>
          </div>
          <div className="tabs" role="tablist">
            {[
              ["metadata", "元数据"],
              ["text", "正文"],
              ["works", "作品关联"],
              ["events", "操作记录"],
            ].map(([k, v]) => (
              <button
                key={k}
                role="tab"
                aria-selected={tab === k}
                className={tab === k ? "chosen" : ""}
                onClick={() => setTab(k)}
              >
                {v}
              </button>
            ))}
          </div>
          {tab === "metadata" &&
            (editing ? (
              <Editor
                key={m.version_id}
                metadata={m}
                onDone={refresh}
                onCancel={() => setEditing(false)}
              />
            ) : (
              <div className="metadata">
                <div className="section-title">
                  <h3>书目信息与质量</h3>
                  <button
                    disabled={!m.is_current || meta.loading || !!meta.error}
                    onClick={() => setEditing(true)}
                  >
                    编辑元数据
                  </button>
                </div>
                {!m.is_current && (
                  <p>历史版本保留原貌，请切换到当前版进行审核。</p>
                )}
                <dl className="fields">
                  {Object.entries(labels)
                    .filter(([k]) => k !== "extra")
                    .map(([k, label]) => (
                      <div key={k}>
                        <dt>{label}</dt>
                        <dd>
                          {k === "quality_status"
                            ? quality[String(m[k])]
                            : ["composed_years", "edition_years"].includes(k)
                              ? rangeLabel(m[k])
                              : show(m[k])}
                          <small>
                            {basis[String(m.field_basis[k])] ||
                              (m.field_basis[k]
                                ? show(m.field_basis[k])
                                : "依据未记录")}
                          </small>
                        </dd>
                      </div>
                    ))}
                </dl>
                <h3>处理步骤</h3><dl className="fields">{Object.entries((m.processed_steps || {}) as Record<string,boolean>).map(([step,done])=><div key={step}><dt>{stepLabels[step] || step}</dt><dd>{done?'已完成':'未完成'}</dd></div>)}</dl>
                <CurationHistory key={m.doc_id} docId={m.doc_id}/>
                <h3>来源与版本</h3>
                <dl className="fields">
                  {Object.entries({
                    来源: m.source,
                    来源内标识: m.document.source_key,
                    文档粒度: m.document.unit_type,
                    父文档: m.document.parent_doc_id,
                    版本ID: m.version_id,
                    父版本: m.parent_version_id,
                    创建时间: date(m.created_at),
                    处理类型: m.processing.kind,
                    处理规则: m.processing.rule_version,
                    发布批次: m.processing.batch_id,
                  }).map(([k, v]) => (
                    <div key={k}>
                      <dt>{k}</dt>
                      <dd>{show(v)}</dd>
                    </div>
                  ))}
                </dl>
                <details>
                  <summary>字段依据与补充信息</summary>
                  <pre>
                    {JSON.stringify(
                      {
                        field_basis: m.field_basis,
                        extra: m.extra,
                        text_hash: m.text_hash,
                      },
                      null,
                      2,
                    )}
                  </pre>
                </details>
                <details>
                  <summary>
                    关联来源记录 · 最近 {m.source_records.length} 条
                  </summary>
                  <p>
                    这些记录属于文档各次入库，所选版本对应的发布批次见上方。
                  </p>
                  {m.source_records.map((r, i) => (
                    <article key={i}>
                      <h4>
                        批次 #{String(r.batch_id)} · 记录 #{String(r.record_id)}
                      </h4>
                      <pre>{JSON.stringify(r, null, 2)}</pre>
                    </article>
                  ))}
                </details>
              </div>
            ))}
          {tab === "text" && (
            <>
              <label className="check"><input type="checkbox" checked={highlight} onChange={e=>setHighlight(e.target.checked)}/>重复高亮（最多 200 条，不修改正文）</label>
              {highlight&&spans.data?.message&&<p className="notice">{spans.data.message}</p>}
              {highlight&&spans.error&&<p className="error">{spans.error}</p>}
              <State loading={text.loading} error={text.error} />
              {text.data && (
                <>
                  <pre className="text">{highlight?<RepeatHighlight text={text.data.text} offset={offset} spans={spans.data?.items||[]}/>:text.data.text}</pre>
                  <div className="pagination">
                    <span>字符 {count(offset)} 起</span>
                    <div>
                      <button
                        disabled={!offset}
                        onClick={() => setOffset(Math.max(0, offset - 10000))}
                      >
                        前段
                      </button>
                      <button
                        disabled={text.data.next_offset === null}
                        onClick={() => setOffset(text.data!.next_offset!)}
                      >
                        后段
                      </button>
                    </div>
                  </div>
                </>
              )}
            </>
          )}
          {tab === "events" && (
            <>
              <State loading={events.loading} error={events.error} />
              {events.data && <Events events={events.data} />}
            </>
          )}
          {tab === "works" && (
            <div className="metadata">
              <State
                loading={works.loading}
                error={works.error}
                empty={works.data?.length === 0}
              />
              <div className="edit-grid">
                <label>
                  操作人
                  <input
                    value={actor}
                    onChange={(e) => setActor(e.target.value)}
                  />
                </label>
                <label>
                  审核依据
                  <input
                    value={reason}
                    onChange={(e) => setReason(e.target.value)}
                  />
                </label>
              </div>
              {operation && <p role="status">{operation}</p>}
              {works.data?.map((w) => (
                <article
                  className="work-card"
                  key={w.work_id + ":" + w.relation}
                >
                  <h3>{w.canonical_title}</h3>
                  <p>
                    {w.relation} · 依据 {basis[w.basis] || w.basis} · {w.status}
                  </p>
                  <div className="actions">
                    <button
                      disabled={
                        busy ||
                        w.status === "rejected" ||
                        !actor.trim() ||
                        !reason.trim()
                      }
                      onClick={() => decide(w, "rejected")}
                    >
                      拒绝关联
                    </button>
                    <button
                      disabled={
                        busy ||
                        w.status === "confirmed" ||
                        !actor.trim() ||
                        !reason.trim()
                      }
                      onClick={() => decide(w, "confirmed")}
                    >
                      确认关联
                    </button>
                  </div>
                </article>
              ))}
            </div>
          )}
        </>
      )}
    </section>
  );
}

function CurationHistory({docId}:{docId:number}) {
  const [open,setOpen]=useState(false),[id,setId]=useState<number|null>(null);
  const data=useLoad<{items:{item_id:number;status:string}[]}>(open?`curation/items?doc_id=${docId}`:null);
  return <div><button onClick={()=>setOpen(!open)}>整理证据与人工审核记录</button>{open && <>
    <State loading={data.loading} error={data.error}/>{data.data?.items.map(i=><button key={i.item_id} onClick={()=>setId(i.item_id)}>工作项 #{i.item_id} · {i.status}</button>)}
    {id && <CurationItem key={id} id={id}/>}</>}</div>;
}
