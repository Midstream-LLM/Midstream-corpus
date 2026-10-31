import { useState } from "react";
import { useLoad, State, count } from "./shared";
export function yearLabel(n: number) {
  return n < 0 ? `公元前${-n}年` : `${n}年`;
}
export function rangeLabel(value: unknown) {
  if (typeof value !== "string") return "未记录";
  const m = value.match(/^([[(])(-?\d*)?,(-?\d*)?([\])])$/);
  if (!m) return value;
  const lo = m[2] ? Number(m[2]) + (m[1] === "(" ? 1 : 0) : null;
  const hi = m[3] ? Number(m[3]) - (m[4] === ")" ? 1 : 0) : null;
  return lo === hi && lo !== null
    ? yearLabel(lo)
    : `${lo === null ? "起始未知" : yearLabel(lo)} — ${hi === null ? "结束未知" : yearLabel(hi)}`;
}
export function YearFilter({
  values,
  onChange,
}: {
  values: Record<string, string>;
  onChange: (v: Record<string, string>) => void;
}) {
  const [mode, setMode] = useState("any"),
    [period, setPeriod] = useState("");
  const options = useLoad<{ labels: string[] }>("years/options");
  function changeMode(next: string) {
    setMode(next);
    setPeriod("");
    const v = { ...values };
    for (const k of [
      "era",
      "year_from",
      "year_to",
      "include_unknown_years",
      "only_unknown_years",
    ])
      delete v[k];
    if (next === "unknown") v.only_unknown_years = "true";
    onChange(v);
  }
  return (
    <div className="year-filter">
      <label>
        成书年代粒度
        <select value={mode} onChange={(e) => changeMode(e.target.value)}>
          <option value="any">不限年代</option>
          <option value="era">朝代／时代标签</option>
          <option value="century">世纪</option>
          <option value="year">具体年份</option>
          <option value="range">自定义年份区间</option>
          <option value="unknown">仅看年份未知</option>
        </select>
      </label>
      {mode === "era" && (
        <label>
          朝代／时代（来源标签）
          <select
            required
            value={values.era || ""}
            onChange={(e) => onChange({ ...values, era: e.target.value })}
          >
            <option value="">选择标签</option>
            {options.data?.labels.map((s) => (
              <option key={s}>{s}</option>
            ))}
          </select>
          {options.error && <small className="error">{options.error}</small>}
        </label>
      )}
      {["century", "year"].includes(mode) && (
        <label>
          {mode === "century" ? "世纪序号" : "年份"}
          <input
            required
            type="number"
            step="1"
            value={period}
            placeholder={
              mode === "century" ? "如 7；公元前用负数" : "如 618；公元前用负数"
            }
            onChange={(e) => {
              const s = e.target.value,
                n = Number(s);
              setPeriod(s);
              e.target.setCustomValidity(
                !s || !Number.isInteger(n) || n === 0 ? "请输入非零整数" : "",
              );
              if (s && Number.isInteger(n) && n !== 0) {
                const lo =
                  mode === "year" ? n : n > 0 ? (n - 1) * 100 + 1 : n * 100;
                const hi =
                  mode === "year" ? n : n > 0 ? n * 100 : (n + 1) * 100 - 1;
                onChange({
                  ...values,
                  year_from: String(lo),
                  year_to: String(hi),
                });
              }
            }}
          />
        </label>
      )}
      {mode === "range" && (
        <>
          <label>
            起始年份
            <input
              type="number"
              step="1"
              placeholder="不限"
              value={values.year_from || ""}
              onChange={(e) => {
                e.target.setCustomValidity(
                  e.target.value === "0" ? "没有公元0年" : "",
                );
                onChange({ ...values, year_from: e.target.value });
              }}
            />
          </label>
          <label>
            结束年份
            <input
              type="number"
              step="1"
              placeholder="不限"
              value={values.year_to || ""}
              onChange={(e) => {
                e.target.setCustomValidity(
                  e.target.value === "0" ? "没有公元0年" : "",
                );
                onChange({ ...values, year_to: e.target.value });
              }}
            />
          </label>
        </>
      )}
      {["century", "year", "range"].includes(mode) && (
        <>
          <label className="check">
            <input
              type="checkbox"
              checked={values.include_unknown_years === "true"}
              onChange={(e) =>
                onChange({
                  ...values,
                  include_unknown_years: String(e.target.checked),
                })
              }
            />
            同时包含年份未知
          </label>
          <p>
            {values.year_from && values.year_to
              ? `${yearLabel(Number(values.year_from))} — ${yearLabel(Number(values.year_to))}。`
              : ""}
            按成书区间相交匹配；负数表示公元前，没有公元0年。
          </p>
        </>
      )}
      {mode === "era" && <p>精确匹配已有时代标签，不替未知年份推定时间。</p>}
    </div>
  );
}
type Distribution = {
  granularity: string;
  total: number;
  known_years: number;
  unknown_years: number;
  open_ranges: number;
  year_from?: number | null;
  year_to?: number | null;
  windowed?: boolean;
  overlapping: boolean;
  items: {
    label: string;
    documents: number;
    characters: number;
    year_from?: number;
    year_to?: number;
  }[];
};
export function YearStats({ revision = 0 }: { revision?: number }) {
  const [mode, setMode] = useState("century"),
    [from, setFrom] = useState(""),
    [to, setTo] = useState(""),
    [window, setWindow] = useState<{ from: string; to: string }>({
      from: "",
      to: "",
    });
  const params = new URLSearchParams({ granularity: mode });
  if (mode !== "dynasty") {
    if (window.from) params.set("year_from", window.from);
    if (window.to) params.set("year_to", window.to);
  }
  const state = useLoad<Distribution>("stats/years?" + params, revision),
    d = state.data;
  return (
    <section className="catalog year-stats">
      <div className="panel-heading">
        <h2>成书年代分布</h2>
        <span>当前版本</span>
      </div>
      <form
        className="year-filter"
        onSubmit={(e) => {
          e.preventDefault();
          setWindow({ from, to });
        }}
      >
        <label>
          统计粒度
          <select value={mode} onChange={(e) => setMode(e.target.value)}>
            <option value="dynasty">朝代／时代标签</option>
            <option value="century">世纪</option>
            <option value="year">逐年</option>
          </select>
        </label>
        {mode !== "dynasty" && (
          <>
            <label>
              起始年份
              <input
                type="number"
                placeholder="自动"
                value={from}
                onChange={(e) => setFrom(e.target.value)}
              />
            </label>
            <label>
              结束年份
              <input
                type="number"
                placeholder="自动"
                value={to}
                onChange={(e) => setTo(e.target.value)}
              />
            </label>
            <button>应用区间</button>
          </>
        )}
      </form>
      <State loading={state.loading} error={state.error} />
      {d && (
        <div className="distribution">
          <p>
            有年份区间 {count(d.known_years)} 篇 · 年份未知{" "}
            {count(d.unknown_years)} 篇
            {d.open_ranges > 0 ? ` · 开放边界 ${count(d.open_ranges)} 篇` : ""}
          </p>
          {d.year_from != null && d.year_to != null && (
            <p>
              展示 {yearLabel(d.year_from)} — {yearLabel(d.year_to)}
              {d.windowed ? "（自动显示最近100栏，可自定区间）" : ""}
            </p>
          )}
          <p>
            {d.overlapping
              ? "跨世纪或跨年的文献会计入多个相交栏位，栏位合计不代表独立文档总量。"
              : "按来源原有时代标签分组；标签未知与年份未知是两种情况。"}
          </p>
          <div className="year-bins">
            {d.items.map((b, i) => (
              <div key={i}>
                <div>
                  <b>{b.label}</b>
                  <span>
                    {count(b.documents)} 篇 · {count(b.characters)} 字符
                  </span>
                </div>
                <div className="bar">
                  <i
                    style={{
                      width:
                        (b.documents /
                          Math.max(1, ...d.items.map((x) => x.documents))) *
                          100 +
                        "%",
                    }}
                  />
                </div>
              </div>
            ))}
          </div>
          {!d.items.length && <p>暂无可展示的年代记录</p>}
        </div>
      )}
    </section>
  );
}
