import Import from "./Import";
import BatchPreview from "./BatchPreview";
import { savedActor } from "./importDraft";
import { useEffect, useState } from "react";
import {
  api,
  useLoad,
  State,
  count,
  date,
  batchLabels,
  Events,
  type Audit,
} from "./shared";
type ActiveJob = {job_id: number; status: string; kind: string};
type Batch = {
  active_job?: ActiveJob | null;
  batch_id: number;
  source: string;
  status: string;
  received_at: string;
  published_at: string | null;
  raw_objects: number;
};
type Detail = {
  active_job?: ActiveJob | null;
  batch_id: number;
  source: string;
  status: string;
  parser_version: string;
  records_ok: number;
  records_failed: number;
  staged: { decoded: number; cleaned: number };
  has_partition: boolean;
  uncurated?: number | null;
  failure_samples: {
    source_key: string;
    parse_error: string;
    locator: unknown;
  }[];
  raw_objects: { path: string; size_bytes: number; sha256: string }[];
};
export default function Batches({ onJob, active = true }: { onJob: (id: number) => void; active?: boolean }) {
  const [creating, setCreating] = useState(false);
  const [all, setAll] = useState(false),
    [before, setBefore] = useState<number | null>(null),
    [history, setHistory] = useState<(number | null)[]>([]),
    [id, setId] = useState<number | null>(null),
    [tick, setTick] = useState(0),
    [actor, setActor] = useState(savedActor),
    [reason, setReason] = useState(""),
    [busy, setBusy] = useState(false),
    [allowUncurated, setAllowUncurated] = useState(false),
    [message, setMessage] = useState(""),
    [confirm, setConfirm] = useState(false);
  const list = useLoad<{ items: Batch[]; next_cursor: number | null }>(
      active ? `batches?include_published=${all}${before ? "&before=" + before : ""}` : null,
      tick,
    ),
    detail = useLoad<Detail>(active && id !== null ? `batches/${id}` : null, tick),
    events = useLoad<Audit[]>(
      active && id !== null ? `events?batch_id=${id}` : null,
      tick,
    );
  useEffect(() => {
    if(!active) return;
    const timer = setInterval(() => {if(!document.hidden) setTick(n => n + 1);}, 5000);
    const updateActor = () => setActor(savedActor());
    window.addEventListener("midstream-operator", updateActor);
    return () => {clearInterval(timer); window.removeEventListener("midstream-operator", updateActor);};
  }, [active]);
  async function act(action: string) {
    if (id === null) return;
    setBusy(true);
    setMessage("");
    setConfirm(false);
    try {
      if (action === "resume" || action === "publish") {
        const job = await api<{ job_id: number }>("jobs", undefined, {
          kind: action === "publish" ? "batch_publish" : "batch_stage", params: action === "publish" && allowUncurated ? { batch_id: id, allow_uncurated: true } : { batch_id: id }, actor, reason: reason.trim() || (action === "publish" ? "人工确认发布批次" : "继续暂存并验证批次"),
        });
        onJob(job.job_id);
        return;
      }
      const result = await api<{ status: string; next_action?: string }>(
        `batches/${id}/${action}`,
        undefined,
        { actor, reason: reason.trim() || "人工放弃批次" },
      );
      setMessage(
        result.status === "already_published"
          ? "此批次已经发布，无需再次发布；查重可单独执行。"
          : `操作完成：${batchLabels[result.status] || result.status}`,
      );
    } catch (e) {
      setMessage((e as Error).message + "；请刷新查看批次的实际状态。");
    } finally {
      setBusy(false);
      setTick((n) => n + 1);
    }
  }
  const d = detail.data,
    allowed =
      !!d &&
      !["published", "abandoned"].includes(d.status) && !d.active_job &&
      actor.trim();
  return (
    <>
      <section className="heading">
        <div>
          <div className="eyebrow">接收 · 验证 · 发布</div>
          <h1>{creating ? "新建入库批次" : "批次管理"}</h1>
          <p>{creating ? "选择文件、确认格式，预览后登记入库。" : "查看入库进度，继续处理待办批次。"}</p>
        </div>
        <div className="heading-actions"><button onClick={() => setCreating(!creating)}>{creating ? "返回批次列表" : "新建入库批次"}</button>{!creating && <button className="subtle" onClick={() => setTick((n) => n + 1)}>刷新</button>}</div>
      </section>
      <div hidden={!creating}><Import active={creating && active} onRegistered={(batchId) => {setCreating(false); setId(batchId); setBefore(null); setHistory([]); setTick(n => n + 1); setActor(savedActor()); setMessage("已提交验证任务，可在任务中心查看进度。");}} /></div>
      {!creating && <>
      <label className="check" hidden={id !== null}>
        <input
          type="checkbox"
          checked={all}
          onChange={(e) => {
            setAll(e.target.checked);
            setBefore(null);
            setHistory([]);
          }}
        />
        包含已发布批次
      </label>
      <div className={"batch-workspace " + (id === null ? "show-list" : "show-detail")}>
        <section className="catalog">
          <div className="panel-heading">
            <h2>入库批次</h2>
          </div>
          <State
            loading={list.loading && !list.data}
            error={list.error}
            empty={list.data?.items.length === 0}
          />
          {list.data && (
            <>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>批次 / 来源</th>
                      <th>状态</th>
                      <th>文件</th>
                      <th>下一步</th>
                    </tr>
                  </thead>
                  <tbody>
                    {list.data.items.map((b) => (
                      <tr
                        key={b.batch_id}
                        className={id === b.batch_id ? "selected" : ""}
                      >
                        <td>
                          <button
                            className="title"
                            disabled={busy}
                            onClick={() => {
                              setId(b.batch_id);
                              setMessage("");
                              setConfirm(false); setActor(savedActor());
                            }}
                          >
                            #{b.batch_id} · {b.source}
                          </button>
                          <small>{date(b.received_at)}</small>
                        </td>
                        <td>
                          <span className="badge">{b.active_job ? b.active_job.status === "queued" ? "排队中" : "处理中" : batchLabels[b.status]}</span>
                        </td>
                        <td>{count(b.raw_objects)}</td><td><button disabled={busy} onClick={() => {if(b.active_job) onJob(b.active_job.job_id); else {setId(b.batch_id); setMessage(""); setConfirm(false);}}}>{b.active_job ? "查看任务进度" : b.status === "validated" ? "检查并发布" : ["published", "abandoned"].includes(b.status) ? "查看详情" : "查看进度 / 处理"}</button></td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="pagination">
                <span>按批次编号倒序</span>
                <div>
                  <button
                    disabled={!history.length || busy}
                    onClick={() => {
                      setBefore(history[history.length - 1]);
                      setHistory(history.slice(0, -1));
                    }}
                  >
                    上一页
                  </button>
                  <button
                    disabled={list.data.next_cursor === null || busy}
                    onClick={() => {
                      setHistory([...history, before]);
                      setBefore(list.data!.next_cursor);
                    }}
                  >
                    下一页
                  </button>
                </div>
              </div>
            </>
          )}
        </section>
        <section className="reader">
          <div className="panel-heading">
            <h2>{id === null ? "批次详情" : `批次 #${id}`}</h2><button disabled={busy} onClick={() => setId(null)}>← 返回批次列表</button>
          </div>
          {id === null ? (
            <div className="empty">选择一个批次查看详情</div>
          ) : (
            <>
              <State loading={detail.loading && !detail.data} error={detail.error} />
              {busy && (
                <div role="status" className="notice">
                  正在处理，批次状态会自动刷新…
                </div>
              )}
              {message && (
                <p role="status" className="notice">
                  {message}
                </p>
              )}
              {d && (
                <div className="metadata">
                  <dl className="fields">
                    {Object.entries({
                      来源: d.source,
                      状态: batchLabels[d.status],
                      解析器: d.parser_version,
                      解析成功: count(d.records_ok),
                      解析失败: count(d.records_failed),
                      暂存原始版: count(d.staged.decoded),
                      暂存清洗版: count(d.staged.cleaned),
                      暂存分区: d.has_partition ? "存在" : "无",
                    }).map(([k, v]) => (
                      <div key={k}>
                        <dt>{k}</dt>
                        <dd>{v}</dd>
                      </div>
                    ))}
                  </dl>
                  {d.active_job && <div className="notice"><p>{d.active_job.status === "queued" ? "任务排队中" : "正在处理"} · 页面每 5 秒更新</p><button onClick={() => onJob(d.active_job!.job_id)}>查看任务 #{d.active_job.job_id}</button></div>}
                  {!d.active_job && !["published", "abandoned"].includes(d.status) && <section className="batch-next">
                  <h3>{d.status === "validated" ? "下一步：检查预览并发布" : "下一步：暂存并验证"}</h3>
                  <p>{d.status === "validated" ? "查看下方全量预览，确认后发布到语料库。" : "系统将解析文件并校验元数据。已有任务时可在任务中心查看进度。"}</p>
                  <div className="edit-grid">
                    <label>
                      操作人
                      <input
                        disabled={busy}
                        value={actor}
                        onChange={(e) => setActor(e.target.value)}
                      />
                    </label>
                    <label>
                      补充备注（选填）
                      <input
                        disabled={busy}
                        value={reason}
                        onChange={(e) => setReason(e.target.value)}
                      />
                    </label>
                  </div>
                  <div className="actions">
                    <button
                      disabled={busy || !allowed}
                      onClick={() => setConfirm(true)}
                    >
                      放弃批次
                    </button>
                    {d.status !== "validated" && <button
                      className="primary"
                      disabled={busy || !allowed}
                      onClick={() => act("resume")}
                    >
                      暂存并验证
                    </button>}
                    {d.status === "validated" && <button className="primary" disabled={busy || !allowed || (!!d.uncurated && !allowUncurated)} onClick={() => act("publish")}>确认发布</button>}
                  </div>
                  {d.status === "validated" && !!d.uncurated && <label className="check-row"><input type="checkbox" disabled={busy} checked={allowUncurated} onChange={e => setAllowUncurated(e.target.checked)}/>
                    还有 {d.uncurated} 条记录未完成入库清洗。仍然发布，这些记录留到“数据整理”里补清洗（记入审计）</label>}
                  {confirm && (
                    <div className="change-preview">
                      <p>
                        放弃后将清理此批次的暂存正文，保留登记与操作记录。此批次不能再恢复。
                      </p>
                      <div className="actions">
                        <button onClick={() => setConfirm(false)}>取消</button>
                        <button
                          className="danger"
                          disabled={!allowed || busy}
                          onClick={() => act("abandon")}
                        >
                          确认放弃此批次
                        </button>
                      </div>
                    </div>
                  )}
                  </section>}
                  {d.has_partition && !["published","abandoned"].includes(d.status) && <BatchPreview key={d.batch_id} id={d.batch_id} tick={tick}/>}
                  <h3>原文件</h3>
                  {d.raw_objects.map((o) => (
                    <details key={o.path}>
                      <summary>
                        {o.path.split("/").pop()} · {count(o.size_bytes)} 字节
                      </summary>
                      <pre>
                        {o.path}\nSHA-256: {o.sha256}
                      </pre>
                    </details>
                  ))}
                  <h3>失败样本</h3>
                  {!d.failure_samples.length ? (
                    <p>暂无失败样本</p>
                  ) : (
                    d.failure_samples.map((f, i) => (
                      <article key={i}>
                        <b>{f.source_key}</b>
                        <p>{f.parse_error}</p>
                        <pre>{JSON.stringify(f.locator, null, 2)}</pre>
                      </article>
                    ))
                  )}
                </div>
              )}
              <div className="panel-heading">
                <h2>批次操作记录</h2>
              </div>
              <State loading={events.loading && !events.data} error={events.error} />
              {events.data && <Events events={events.data} />}
            </>
          )}
        </section>
      </div>
      </>}
    </>
  );
}
