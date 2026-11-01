import { rangeLabel } from "./Years";
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
const common = [
  "title",
  "authors",
  "composed_label",
  "genre",
  "category",
  "topics",
  "license",
  "quality_status",
  "quality_flags",
];
function initial(m: Metadata) {
  return Object.fromEntries(
    common.map((k) => [
      k,
      Array.isArray(m[k]) ? (m[k] as string[]).join("\n") : String(m[k] ?? ""),
    ]),
  );
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
    [actor, setActor] = useState(""),
    [reason, setReason] = useState(""),
    [error, setError] = useState(""),
    [busy, setBusy] = useState(false),
    [preview, setPreview] = useState<Record<string, unknown> | null>(null);
  function changes() {
    const base = initial(metadata);
    const patch: Record<string, unknown> = {};
    for (const k of common) {
      if (form[k] !== base[k])
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
          {common.map((k) => (
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
            可补充成书/刊刻年份区间、历史分类、extra 等字段。例如成书区间用{" "}
            {`{"composed_years":"[618,908)"}`} 表示 618—907 年，null 表示未知。
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
                  <td>{show(metadata[k])}</td>
                  <td>{show(v)}</td>
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
              <State loading={text.loading} error={text.error} />
              {text.data && (
                <>
                  <pre className="text">{text.data.text}</pre>
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
