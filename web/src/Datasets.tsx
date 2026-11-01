import { useState } from "react";
import { useLoad, State, count, date } from "./shared";
import DatasetWizard from "./DatasetWizard";
import DatasetDetail from "./DatasetDetail";
import { type Recipe, type Release } from "./datasetTypes";
export default function Datasets({ onJob }: { onJob: (id: number) => void }) {
  const [id, setId] = useState<number | null>(null), [wizard, setWizard] = useState(false),
    [initial, setInitial] = useState<Recipe | undefined>(), [revision, setRevision] = useState(0),
    [before, setBefore] = useState<number | null>(null), [history, setHistory] = useState<(number | null)[]>([]);
  const list = useLoad<{ items: Release[]; next_cursor: number | null }>("datasets" + (before ? "?before=" + before : ""), revision);
  return <>
    <section className="heading"><div><div className="eyebrow">选材 · 试算 · 固定发布</div><h1>数据集</h1><p>按作品和重复组划分，保留可复现成员，统计和导出独立执行。</p></div>
      <div className="actions"><button onClick={() => setRevision(n => n + 1)}>刷新</button><button className="primary" onClick={() => { setInitial(undefined); setWizard(true); }}>创建数据集</button></div></section>
    {wizard ? <DatasetWizard key={JSON.stringify(initial)} initial={initial} onJob={onJob} onClose={() => setWizard(false)} /> :
      <div className="workspace"><section className="catalog"><div className="panel-heading"><h2>固定发布</h2></div>
        <State loading={list.loading && !list.data} error={list.error} empty={list.data?.items.length === 0} />
        {list.data && <><div className="table-scroll"><table><thead><tr><th>名称 / 创建时间</th><th>字数 / Token</th><th>划分字数</th></tr></thead>
          <tbody>{list.data.items.map(item => <tr key={item.release_id} className={id === item.release_id ? "selected" : ""}><td>
            <button className="title" onClick={() => setId(item.release_id)}>{item.name}</button><small>{date(item.created_at)}</small></td>
            <td>{count(item.char_count)} 字{item.token_stats.map(stats => <small key={stats.tokenizer_fingerprint}>{stats.tokenizer_name}：{count(stats.total ?? 0)} tokens</small>)}</td>
            <td><small>训练 {count(item.train_char_count)}</small><small>验证 {count(item.val_char_count)}</small><small>测试 {count(item.test_char_count)}</small></td></tr>)}</tbody></table></div>
          <div className="pagination"><span>按发布编号倒序</span><div><button disabled={!history.length} onClick={() => { setBefore(history[history.length - 1]); setHistory(history.slice(0, -1)); }}>上一页</button>
            <button disabled={list.data.next_cursor === null} onClick={() => { setHistory([...history, before]); setBefore(list.data!.next_cursor); }}>下一页</button></div></div></>}
      </section>{id === null ? <section className="reader"><div className="empty">选择一个发布查看配方、统计和导出</div></section> :
        <DatasetDetail key={id} id={id} onJob={onJob} onCopy={recipe => { setInitial(recipe); setWizard(true); }} />}</div>}
  </>;
}
