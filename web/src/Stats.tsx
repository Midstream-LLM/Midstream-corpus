import { useState } from "react";
import { YearStats } from "./Years";
import { useLoad, State, count, date, batchLabels } from "./shared";
type Stats = {
  current: { documents: number; characters: number; quarantined: number };
  sources: {
    source: string;
    documents: number;
    ok: number;
    flagged: number;
    with_years: number;
    versions: number;
  }[];
  categories: { name: string | null; documents: number; characters: number }[];
  plain_bytes: number;
  compressed_bytes: number;
  database_bytes: number;
  batch_statuses: Record<string, number>;
  releases: {
    release_id: number;
    name: string;
    char_count: number;
    train: number;
    val: number;
    test: number;
    created_at: string;
  }[];
};
const bytes = (n: number) => (n / 1024 / 1024).toFixed(1) + " MiB";
export default function Stats() {
  const [yearRevision, setYearRevision] = useState(0);
  const state = useLoad<Stats>("stats"),
    s = state.data;
  return (
    <>
      <section className="heading">
        <div>
          <div className="eyebrow">规模 · 质量 · 发布</div>
          <h1>统计概览</h1>
          <p>当前版本的语料规模与质量，以及已固定的数据集发布。</p>
        </div>
        <button onClick={() => { state.refresh(); setYearRevision(n => n + 1); }}>刷新统计</button>
      </section>
      <State loading={state.loading} error={state.error} />
      {s && (
        <>
          <div className="metrics">
            {[
              ["当前文档", s.current.documents],
              ["当前版本字符", s.current.characters],
              ["待复核文档", s.sources.reduce((n, v) => n + v.flagged, 0)],
              ["已隔离文档", s.current.quarantined],
            ].map(([k, v]) => (
              <article key={String(k)}>
                <span>{k}</span>
                <strong>{count(Number(v))}</strong>
              </article>
            ))}
          </div>
          <section className="catalog">
            <div className="panel-heading">
              <h2>按来源查看</h2>
              <span>包含全部质量状态</span>
            </div>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>来源</th>
                    <th>文档</th>
                    <th>正常</th>
                    <th>待复核</th>
                    <th>有年代信息</th>
                    <th>历史版本总数</th>
                  </tr>
                </thead>
                <tbody>
                  {s.sources.map((v) => (
                    <tr key={v.source}>
                      <td>{v.source}</td>
                      {[
                        v.documents,
                        v.ok,
                        v.flagged,
                        v.with_years,
                        v.versions,
                      ].map((n, i) => (
                        <td key={i}>{count(n)}</td>
                      ))}
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {!s.sources.length && <div className="empty">尚未导入语料</div>}
          </section>
          <YearStats revision={yearRevision} />
          <div className="stats-grid">
            <section className="catalog">
              <div className="panel-heading">
                <h2>部类分布</h2>
                <span>按当前文档数</span>
              </div>
              <div className="distribution">
                {s.categories.map((c, i) => (
                  <div key={i}>
                    <div>
                      <b>{c.name || "未分类"}</b>
                      <span>
                        {count(c.documents)} 篇 · {count(c.characters)} 字符
                      </span>
                    </div>
                    <div className="bar">
                      <i
                        style={{
                          width:
                            Math.max(
                              1,
                              (c.documents / Math.max(1, s.current.documents)) *
                                100,
                            ) + "%",
                        }}
                      />
                    </div>
                  </div>
                ))}
              </div>
            </section>
            <section className="catalog">
              <div className="panel-heading">
                <h2>存储与批次</h2>
              </div>
              <div className="metadata">
                <dl className="fields">
                  {Object.entries({
                    "正文原始字节（去重存储）": bytes(s.plain_bytes),
                    压缩正文: bytes(s.compressed_bytes),
                    数据库总大小: bytes(s.database_bytes),
                  }).map(([k, v]) => (
                    <div key={k}>
                      <dt>{k}</dt>
                      <dd>{v}</dd>
                    </div>
                  ))}
                </dl>
                <div className="status-counts">
                  {Object.entries(s.batch_statuses).map(([k, v]) => (
                    <span key={k}>
                      {batchLabels[k] || k} <b>{count(v)}</b>
                    </span>
                  ))}
                </div>
              </div>
            </section>
          </div>
          <section className="catalog">
            <div className="panel-heading">
              <h2>数据集发布</h2>
              <span>最近 50 次 · 字符数为发布快照</span>
            </div>
            <div className="table-scroll">
              <table>
                <thead>
                  <tr>
                    <th>数据集</th>
                    <th>总字符</th>
                    <th>训练</th>
                    <th>验证</th>
                    <th>测试</th>
                    <th>创建时间</th>
                  </tr>
                </thead>
                <tbody>
                  {s.releases.map((r) => (
                    <tr key={r.release_id}>
                      <td>{r.name}</td>
                      {[r.char_count, r.train, r.val, r.test].map((n, i) => (
                        <td key={i}>{count(n)}</td>
                      ))}
                      <td>{date(r.created_at)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {!s.releases.length && <div className="empty">暂无数据集发布</div>}
          </section>
        </>
      )}
    </>
  );
}
