import Import from "./Import";
import { useState, useEffect } from "react";
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
type Batch = {
  batch_id: number;
  source: string;
  status: string;
  received_at: string;
  published_at: string | null;
  raw_objects: number;
};
type Detail = {
  batch_id: number;
  source: string;
  status: string;
  parser_version: string;
  records_ok: number;
  records_failed: number;
  staged: { decoded: number; cleaned: number };
  has_partition: boolean;
  failure_samples: {
    source_key: string;
    parse_error: string;
    locator: unknown;
  }[];
  raw_objects: { path: string; size_bytes: number; sha256: string }[];
};
export default function Batches() {
  const [all, setAll] = useState(false),
    [before, setBefore] = useState<number | null>(null),
    [history, setHistory] = useState<(number | null)[]>([]),
    [id, setId] = useState<number | null>(null),
    [tick, setTick] = useState(0),
    [actor, setActor] = useState(""),
    [reason, setReason] = useState(""),
    [busy, setBusy] = useState(false),
    [message, setMessage] = useState(""),
    [confirm, setConfirm] = useState(false);
  const list = useLoad<{ items: Batch[]; next_cursor: number | null }>(
      `batches?include_published=${all}${before ? "&before=" + before : ""}`,
      tick,
    ),
    detail = useLoad<Detail>(id !== null ? `batches/${id}` : null, tick),
    events = useLoad<Audit[]>(
      id !== null ? `events?batch_id=${id}` : null,
      tick,
    );
  useEffect(() => {
    if (!busy) return;
    const t = setInterval(() => setTick((n) => n + 1), 3000);
    return () => clearInterval(t);
  }, [busy]);
  async function act(action: string) {
    if (id === null) return;
    setBusy(true);
    setMessage("");
    setConfirm(false);
    try {
      const result = await api<{ status: string; next_action?: string }>(
        `batches/${id}/${action}`,
        undefined,
        { actor, reason },
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
      !["published", "abandoned"].includes(d.status) &&
      actor.trim() &&
      reason.trim();
  return (
    <>
      <section className="heading">
        <div>
          <div className="eyebrow">接收 · 验证 · 发布</div>
          <h1>批次管理</h1>
          <p>检查每一次入库的来源与状态，继续处理或放弃未发布批次。</p>
        </div>
        <button onClick={() => setTick((n) => n + 1)}>刷新</button>
      </section>
      <Import onRegistered={(batchId) => {setId(batchId); setBefore(null); setHistory([]); setTick(n => n + 1); setMessage("批次已登记，请填写操作依据并继续处理。");}} />
      <label className="check">
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
      <div className="workspace">
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
                              setConfirm(false);
                            }}
                          >
                            #{b.batch_id} · {b.source}
                          </button>
                          <small>{date(b.received_at)}</small>
                        </td>
                        <td>
                          <span className="badge">{batchLabels[b.status]}</span>
                        </td>
                        <td>{count(b.raw_objects)}</td>
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
            <h2>{id === null ? "批次详情" : `批次 #${id}`}</h2>
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
                  <h3>操作</h3>
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
                      操作依据
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
                    <button
                      className="primary"
                      disabled={busy || !allowed}
                      onClick={() => act("resume")}
                    >
                      继续处理 / 发布
                    </button>
                  </div>
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
    </>
  );
}
