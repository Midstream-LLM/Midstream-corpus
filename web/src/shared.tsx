import { useEffect, useRef, useState } from "react";
export const quality: Record<string, string> = {
  ok: "正常",
  flagged: "待复核",
  quarantined: "已隔离",
};
export const batchLabels: Record<string, string> = {
  received: "已登记",
  staged: "已暂存",
  validated: "已验证",
  published: "已发布",
  failed: "失败",
  needs_review: "待检查",
  abandoned: "已放弃",
};
export const count = (n: number = 0) => Number(n).toLocaleString("zh-CN");
export const show = (v: unknown): string =>
  v === null || v === undefined || v === ""
    ? "未记录"
    : Array.isArray(v)
      ? v.join("、") || "未记录"
      : typeof v === "object"
        ? JSON.stringify(v, null, 2)
        : String(v);
export const date = (s: string | null) =>
  s ? new Date(s).toLocaleString() : "未记录";
export async function api<T>(
  path: string,
  signal?: AbortSignal,
  body?: unknown,
  method = "POST",
): Promise<T> {
  const response = await fetch("/api/v1/" + path, {
    signal,
    ...(body !== undefined
      ? {
          method,
          headers: {
            "Content-Type": "application/json",
            "X-Midstream-Action": "1",
          },
          body: JSON.stringify(body),
        }
      : {}),
  });
  if (!response.ok) {
    const e = await response.json().catch(() => ({}));
    throw new Error(
      typeof e.detail === "string" ? e.detail : `请求失败 (${response.status})`,
    );
  }
  return response.json();
}
export function useLoad<T>(path: string | null, revision = 0) {
  const previousPath = useRef(path);
  const [data, setData] = useState<T | null>(null),
    [error, setError] = useState(""),
    [loading, setLoading] = useState(false),
    [tick, setTick] = useState(0);
  useEffect(() => {
    if (!path) {
      setData(null);
      previousPath.current = path;
      setLoading(false);
      setError("");
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError("");
    if (previousPath.current !== path) setData(null);
    previousPath.current = path;
    api<T>(path, controller.signal)
      .then(setData)
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [path, revision, tick]);
  return { data, error, loading, refresh: () => setTick((n) => n + 1) };
}
export function State({
  loading,
  error,
  empty = false,
}: {
  loading: boolean;
  error: string;
  empty?: boolean;
}) {
  return loading ? (
    <div className="empty" role="status">
      正在读取…
    </div>
  ) : error ? (
    <div className="empty error" role="alert">
      {error}
    </div>
  ) : empty ? (
    <div className="empty">暂无记录</div>
  ) : null;
}
export type Audit = {
  event_id: number;
  action: string;
  actor: string;
  reason: string;
  created_at: string;
  before_state: unknown;
  after_state: unknown;
};
export function Events({ events }: { events: Audit[] }) {
  return (
    <div className="events">
      {events.length === 0 ? (
        <p>暂无人工操作记录</p>
      ) : (
        events.map((e) => (
          <article key={e.event_id}>
            <div>
              <b>{e.actor}</b>
              <span>{date(e.created_at)}</span>
            </div>
            <p>{e.reason}</p>
            <details>
              <summary>查看变更 · {e.action}</summary>
              <div className="diff">
                <pre>{JSON.stringify(e.before_state, null, 2)}</pre>
                <pre>{JSON.stringify(e.after_state, null, 2)}</pre>
              </div>
            </details>
          </article>
        ))
      )}
    </div>
  );
}
