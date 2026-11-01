import { useEffect, useState } from "react";
import { api, useLoad, State, date } from "./shared";

type Job = {
  job_id: number;
  kind: string;
  status: string;
  params: Record<string, unknown>;
  progress: { current: number; total: number; message: string };
  result: unknown;
  error: string | null;
  actor: string;
  reason: string;
  cancel_requested: boolean;
  created_at: string;
  started_at: string | null;
  finished_at: string | null;
  heartbeat_at: string | null;
  events: { event_id: number; created_at: string; level: string; message: string }[];
};
const labels: Record<string, string> = {
  queued: "排队中", running: "运行中", succeeded: "已完成", failed: "失败", cancelled: "已取消",
};
const kinds: Record<string, string> = {
  batch_resume: "批次处理 / 发布", dedup: "文档查重", dedup_spans: "段落查重",
};
const active = (job: Job) => ["queued", "running"].includes(job.status);

function Progress({ job }: { job: Job }) {
  return <>
    <progress aria-label="任务进度" style={{ width: "100%" }}
      max={job.progress.total || 1} value={job.progress.total ? job.progress.current : undefined} />
    <small>{job.progress.message}{job.progress.total > 0 && ` · ${job.progress.current}/${job.progress.total}`}</small>
  </>;
}

export default function Jobs({ id, onSelect }: { id: number | null; onSelect: (id: number) => void }) {
  const [tick, setTick] = useState(0),
    [before, setBefore] = useState<number | null>(null),
    [history, setHistory] = useState<(number | null)[]>([]),
    [actor, setActor] = useState(""), [reason, setReason] = useState(""),
    [busy, setBusy] = useState(false), [message, setMessage] = useState("");
  const list = useLoad<{ items: Job[]; next_cursor: number | null }>(`jobs${before ? `?before=${before}` : ""}`, tick),
    detail = useLoad<Job>(id === null ? null : `jobs/${id}`, tick);
  const polling = !!list.data?.items.some(active) || !!(detail.data && active(detail.data));
  useEffect(() => {
    if (!polling) return;
    const timer = setInterval(() => setTick(n => n + 1), 2000);
    return () => clearInterval(timer);
  }, [polling]);
  useEffect(() => setMessage(""), [id]);
  async function act(action: "cancel" | "retry") {
    setBusy(true);
    setMessage("");
    try {
      const result = await api<Job>(`jobs/${id}/${action}`, undefined, { actor, reason });
      if (action === "retry") {
        setBefore(null);
        setHistory([]);
        onSelect(result.job_id);
      } else {
        setMessage(result.status === "cancelled" ? "任务已取消。" : "已请求取消，将在下一阶段前停止。");
      }
      setTick(n => n + 1);
    } catch (e) {
      setMessage((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  const d = detail.data, allowed = !busy && !!actor.trim() && !!reason.trim();
  return <>
    <section className="heading">
      <div><div className="eyebrow">排队 · 执行 · 追踪</div><h1>任务中心</h1>
        <p>长操作在后台串行执行。需要手动启动 worker；关闭页面后任务仍会继续。</p></div>
      <button onClick={() => setTick(n => n + 1)}>刷新</button>
    </section>
    <div className="workspace">
      <section className="catalog">
        <div className="panel-heading"><h2>任务列表</h2></div>
        <State loading={list.loading && !list.data} error={list.error} empty={list.data?.items.length === 0} />
        {list.data && <>
          <div className="table-scroll"><table>
            <thead><tr><th>任务</th><th>状态 / 进度</th></tr></thead>
            <tbody>{list.data.items.map(job => <tr key={job.job_id} className={id === job.job_id ? "selected" : ""}>
              <td><button className="title" onClick={() => onSelect(job.job_id)}>#{job.job_id} · {kinds[job.kind]}</button>
                <small>{date(job.created_at)}</small></td>
              <td><span className="badge">{labels[job.status]}</span><Progress job={job} /></td>
            </tr>)}</tbody>
          </table></div>
          <div className="pagination"><span>按任务编号倒序</span><div>
            <button disabled={!history.length} onClick={() => {
              setBefore(history[history.length - 1]); setHistory(history.slice(0, -1));
            }}>上一页</button>
            <button disabled={list.data.next_cursor === null} onClick={() => {
              setHistory([...history, before]); setBefore(list.data!.next_cursor);
            }}>下一页</button>
          </div></div>
        </>}
      </section>
      <section className="reader">
        <div className="panel-heading"><h2>{id === null ? "任务详情" : `任务 #${id}`}</h2></div>
        {id === null ? <div className="empty">选择一个任务查看详情和日志</div> : <>
          <State loading={detail.loading && !detail.data} error={detail.error} />
          {message && <p role="status" className="notice">{message}</p>}
          {d && <div className="metadata">
            <dl className="fields">{Object.entries({
              类型: kinds[d.kind], 状态: labels[d.status], 操作人: d.actor, 操作依据: d.reason,
              提交时间: date(d.created_at), 开始时间: date(d.started_at),
              完成时间: date(d.finished_at), 最近心跳: date(d.heartbeat_at),
            }).map(([k, v]) => <div key={k}><dt>{k}</dt><dd>{v}</dd></div>)}</dl>
            <Progress job={d} />
            {d.cancel_requested && <p className="notice">已请求取消；已完成的阶段保留。</p>}
            {d.error && <p role="alert" className="error">{d.error}</p>}
            <h3>参数</h3><pre>{JSON.stringify(d.params, null, 2)}</pre>
            {(active(d) || ["failed", "cancelled"].includes(d.status)) && <>
              <div className="edit-grid">
                <label>操作人<input value={actor} onChange={e => setActor(e.target.value)} /></label>
                <label>操作依据<input value={reason} onChange={e => setReason(e.target.value)} /></label>
              </div>
              <div className="actions">{active(d)
                ? <button disabled={!allowed || d.cancel_requested} onClick={() => act("cancel")}>取消任务</button>
                : <button className="primary" disabled={!allowed} onClick={() => act("retry")}>按原参数新建重试任务</button>}
              </div>
            </>}
            {d.result !== null && <><h3>结果</h3><pre>{JSON.stringify(d.result, null, 2)}</pre></>}
            <h3>最近日志（最多 100 条）</h3>
            <div className="events">{d.events.map(event => <article key={event.event_id}>
              <div><b>{event.level}</b><span>{date(event.created_at)}</span></div><p>{event.message}</p>
            </article>)}</div>
          </div>}
        </>}
      </section>
    </div>
  </>;
}
