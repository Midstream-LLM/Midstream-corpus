import { YearFilter } from "./Years";
import { useState } from "react";
import DocumentPanel from "./DocumentPanel";
import { useLoad, State, quality, count } from "./shared";
type Row = {
  doc_id: number;
  title: string | null;
  authors: string[] | null;
  source: string;
  composed_label: string | null;
  category: string | null;
  char_count: number;
  quality_status: string;
  version_no: number;
};
export default function Corpus({ review = false }: { review?: boolean }) {
  const defaults: Record<string, string> = review ? { quality: "flagged" } : {};
  const [filters, setFilters] = useState<Record<string, string>>(defaults),
    [applied, setApplied] = useState<Record<string, string>>(defaults),
    [cursor, setCursor] = useState(0),
    [history, setHistory] = useState<number[]>([]),
    [selected, setSelected] = useState<number | null>(null),
    [revision, setRevision] = useState(0);
  const sources = useLoad<{ name: string }[]>("sources"),
    list = useLoad<{ items: Row[]; next_cursor: number | null }>(
      "documents?" + new URLSearchParams({ ...applied, after: String(cursor) }),
      revision,
    );
  const filter = (k: string, v: string) =>
    setFilters((f) => ({ ...f, [k]: v }));
  return (
    <>
      <section className="heading">
        <div>
          <div className="eyebrow">文献 · 版本 · 来源</div>
          <h1>{review ? "人工审核" : "浏览语料"}</h1>
          <p>
            {review
              ? "从待复核文档开始，核对字段依据、质量与作品关联。"
              : "先了解文献的身份、来源和处理历史，再按需阅读正文。"}
          </p>
        </div>
        <button onClick={() => setRevision((n) => n + 1)}>↻ 刷新</button>
      </section>
      <form
        className="filters"
        onSubmit={(e) => {
          e.preventDefault();
          setCursor(0);
          setHistory([]);
          setApplied(
            Object.fromEntries(
              Object.entries(filters).filter(([, v]) => v !== ""),
            ),
          );
        }}
      >
        <div className="filter-top">
          <label className="search">
            <span>⌕</span>
            <input
              aria-label="标题前缀"
              placeholder="按标题前缀查找文献…"
              value={filters.title || ""}
              onChange={(e) => filter("title", e.target.value)}
            />
          </label>
          <button className="primary">查询语料</button>
        </div>
        <div className="filter-grid">
          <label>
            来源
            <select
              value={filters.source || ""}
              onChange={(e) => filter("source", e.target.value)}
            >
              <option value="">全部来源</option>
              {sources.data?.map((s) => (
                <option key={s.name}>{s.name}</option>
              ))}
            </select>
          </label>
          <label>
            作者
            <input
              placeholder="完整作者名"
              value={filters.author || ""}
              onChange={(e) => filter("author", e.target.value)}
            />
          </label>
          <label>
            部类
            <input
              placeholder="如：史"
              value={filters.category || ""}
              onChange={(e) => filter("category", e.target.value)}
            />
          </label>
          <label>
            质量
            <select
              value={filters.quality || ""}
              onChange={(e) => filter("quality", e.target.value)}
            >
              <option value="">全部状态</option>
              {Object.entries(quality).map(([k, v]) => (
                <option key={k} value={k}>
                  {v}
                </option>
              ))}
            </select>
          </label>
        </div>
        <YearFilter values={filters} onChange={setFilters} />
      </form>
      <div className="workspace">
        <section className="catalog">
          <div className="panel-heading">
            <h2>文献目录</h2>
            <span>
              {list.loading
                ? "查询中…"
                : `本页 ${list.data?.items.length || 0} 篇`}
            </span>
          </div>
          <State
            loading={list.loading}
            error={list.error}
            empty={list.data?.items.length === 0}
          />
          {list.data && (
            <>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr>
                      <th>篇名 / 作者</th>
                      <th>来源 / 部类</th>
                      <th>字符数</th>
                      <th>状态</th>
                    </tr>
                  </thead>
                  <tbody>
                    {list.data.items.map((r) => (
                      <tr
                        key={r.doc_id}
                        className={selected === r.doc_id ? "selected" : ""}
                      >
                        <td>
                          <button
                            className="title"
                            onClick={() => setSelected(r.doc_id)}
                          >
                            {r.title || "未题名"}
                          </button>
                          <small>
                            {r.authors?.join("、") || "作者未知"} ·{" "}
                            {r.composed_label || "年代未知"}
                          </small>
                        </td>
                        <td>
                          {r.source}
                          <small>
                            {r.category || "未分类"} · v{r.version_no}
                          </small>
                        </td>
                        <td>{count(r.char_count)}</td>
                        <td>
                          <span className={"badge " + r.quality_status}>
                            {quality[r.quality_status]}
                          </span>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="pagination">
                <span>按文档编号排序</span>
                <div>
                  <button
                    disabled={!history.length || list.loading}
                    onClick={() => {
                      setCursor(history[history.length - 1]);
                      setHistory(history.slice(0, -1));
                    }}
                  >
                    上一页
                  </button>
                  <button
                    disabled={list.data.next_cursor === null || list.loading}
                    onClick={() => {
                      setHistory([...history, cursor]);
                      setCursor(list.data!.next_cursor!);
                    }}
                  >
                    下一页 →
                  </button>
                </div>
              </div>
            </>
          )}
        </section>
        {selected === null ? (
          <section className="reader">
            <div className="panel-heading">
              <h2>文档档案</h2>
            </div>
            <div className="reader-empty">
              <div className="book">冊</div>
              <h3>选择一篇文献</h3>
              <p>
                先查看完整元数据与来源依据
                <br />
                也可切换正文、关联及操作记录。
              </p>
            </div>
          </section>
        ) : (
          <DocumentPanel
            key={selected}
            id={selected}
            onClose={() => setSelected(null)}
            onChanged={() => setRevision((n) => n + 1)}
          />
        )}
      </div>
    </>
  );
}
