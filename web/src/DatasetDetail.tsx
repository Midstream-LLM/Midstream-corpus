import { useEffect, useState } from "react";
import { api, useLoad, State, count, date } from "./shared";
import { splitNames, type Recipe, type Release, type TokenStats } from "./datasetTypes";
type Export = { export_id: number; status: string; format: string; manifest_path: string | null; error: string | null;
  files: { path: string; row_count: number; size_bytes: number; sha256: string }[] };
const allFields = ["ordinal", "version_id", "doc_id", "split", "title", "text", "authors", "era", "category", "genre", "source"];
export default function DatasetDetail({ id, onCopy, onJob }: { id: number; onCopy: (recipe: Recipe) => void; onJob: (id: number) => void }) {
  const [tick, setTick] = useState(0), [actor, setActor] = useState(""), [reason, setReason] = useState(""),
    [tokenizer, setTokenizer] = useState(""), [format, setFormat] = useState("parquet"), [unit, setUnit] = useState("document"),
    [shard, setShard] = useState(256), [maxChars, setMaxChars] = useState(8192), [overlap, setOverlap] = useState(0),
    [fields, setFields] = useState(allFields.slice(0, 6)), [zst, setZst] = useState(false), [hash, setHash] = useState(false),
    [run, setRun] = useState(""), [busy, setBusy] = useState(false), [message, setMessage] = useState("");
  const detail = useLoad<Release>("datasets/" + id, tick), stats = useLoad<TokenStats[]>("datasets/" + id + "/token-stats", tick),
    exports = useLoad<Export[]>("datasets/" + id + "/exports", tick),
    training = useLoad<{ run_name: string; used_at: string; note: string }[]>("datasets/" + id + "/training", tick);
  const running = exports.data?.some(item => item.status === "writing");
  useEffect(() => { if (!running) return; const t = setInterval(() => setTick(n => n + 1), 2000); return () => clearInterval(t); }, [running]);
  async function act(action: string, extra: object = {}) {
    setBusy(true); setMessage("");
    try {
      const result = await api<{ job_id?: number }>("datasets/" + id + "/" + action, undefined, { actor, reason, ...extra });
      if (result.job_id) onJob(result.job_id); else { setTick(n => n + 1); setMessage("训练使用已登记。"); }
    } catch (e) { setMessage((e as Error).message); } finally { setBusy(false); }
  }
  const enabled = !!actor.trim() && !!reason.trim() && !busy;
  return <section className="reader dataset-detail">
    <div className="panel-heading"><h2>数据集 #{id}</h2><button onClick={() => setTick(n => n + 1)}>刷新详情</button></div>
    <State loading={detail.loading && !detail.data} error={detail.error} />
    {detail.data && <div className="metadata">
      <h2>{detail.data.name}</h2><p>{date(detail.data.created_at)}</p>
      <div className="metrics"><article><span>总字数</span><strong>{count(detail.data.char_count)}</strong></article>
        <article><span>成员数</span><strong>{count(Object.values(detail.data.splits).reduce((sum, v) => sum + v.documents, 0))}</strong></article></div>
      <table><thead><tr><th>划分</th><th>篇数</th><th>字数</th></tr></thead><tbody>{Object.entries(splitNames).map(([split, label]) =>
        <tr key={split}><td>{label}</td><td>{count(detail.data!.splits[split]?.documents)}</td><td>{count(detail.data!.splits[split]?.chars)}</td></tr>)}</tbody></table>
      <details><summary>固定配方（只读）</summary><pre>{JSON.stringify(detail.data.recipe, null, 2)}</pre><p>内容摘要：{detail.data.content_digest}</p></details>
      <button onClick={() => onCopy({ ...detail.data!.recipe, name: detail.data!.name + "-copy" })}>复制配方创建新数据集</button>
      <h3>操作依据</h3><div className="edit-grid"><label>操作人<input value={actor} onChange={e => setActor(e.target.value)} /></label>
        <label>操作依据<input value={reason} onChange={e => setReason(e.target.value)} /></label></div>
      {message && <p role="status" className="notice">{message}</p>}
      <h3>Token 统计</h3><State loading={stats.loading && !stats.data} error={stats.error} />
      {!stats.data?.length && <p>未统计</p>}
      {stats.data?.map(item => <article key={item.tokenizer_fingerprint}><b>{item.tokenizer_name} · {count(item.total ?? 0)} tokens（精确）</b>
        <small>{item.tokenizer_fingerprint}</small><p>{Object.entries(item.by_split || {}).map(([split, n]) => splitNames[split] + " " + count(n)).join(" · ")}</p>
        <p>{Object.entries(item.percentiles || {}).map(([name, n]) => name + " " + count(n)).join(" · ")}</p></article>)}
      <label>分词器路径<input value={tokenizer} placeholder="后端机器上的 tokenizer.json 路径" onChange={e => setTokenizer(e.target.value)} /></label>
      <button disabled={!enabled || !tokenizer.trim()} onClick={() => act("token-stats", { tokenizer })}>发起 Token 统计</button>
      <h3>校验发布与文件</h3><label className="check"><input type="checkbox" checked={hash} onChange={e => setHash(e.target.checked)} />重新读取文件核对 SHA-256</label>
      <button disabled={!enabled} onClick={() => act("verify", { hash_files: hash })}>提交校验任务</button>
      <h3>导出</h3><div className="edit-grid"><label>导出格式<select value={format} onChange={e => { setFormat(e.target.value); setZst(false); }}>
        <option value="parquet">Parquet</option><option value="jsonl">JSONL</option><option value="pkl">PKL（仅加载可信文件）</option></select></label>
        <label>记录单位<select value={unit} onChange={e => setUnit(e.target.value)}><option value="document">完整文档</option><option value="sample">切分样本</option></select></label>
        <label>分片预算（MiB）<input type="number" min=".001" step="any" value={shard} onChange={e => setShard(Number(e.target.value))} /></label>
        {unit === "sample" && <><label>每条最多字符<input type="number" min="1" value={maxChars} onChange={e => setMaxChars(Number(e.target.value))} /></label>
          <label>重叠字符<input type="number" min="0" value={overlap} onChange={e => setOverlap(Number(e.target.value))} /></label></>}</div>
      {format === "jsonl" && <label className="check"><input type="checkbox" checked={zst} onChange={e => setZst(e.target.checked)} />Zstandard 压缩</label>}
      <fieldset><legend>导出字段</legend><div className="actions">{allFields.map(field => <label className="check" key={field}><input type="checkbox" checked={fields.includes(field)}
        onChange={e => setFields(e.target.checked ? [...fields, field] : fields.filter(x => x !== field))} />{field}</label>)}</div></fieldset>
      <button disabled={!enabled || !fields.length || shard <= 0 || (unit === "sample" && (maxChars <= overlap || overlap < 0))}
        onClick={() => act("exports", { options: { format, fields, unit, shard_mb: shard, max_chars: maxChars, overlap_chars: overlap, compression: zst ? "zst" : null } })}>提交导出任务</button>
      <h3>导出历史与下载</h3><State loading={exports.loading && !exports.data} error={exports.error} empty={exports.data?.length === 0} />
      {exports.data?.map(item => <details key={item.export_id}><summary>#{item.export_id} · {item.format} · {{ completed: "已完成", failed: "失败", writing: "写出中" }[item.status as "completed"]}</summary>
        {item.error && <p className="error">{item.error}</p>}
        {item.manifest_path && <><p>manifest：{item.manifest_path}</p><p>把 USER@HOST 替换为数据服务器登录信息：</p>
          <pre>{"rsync -av --protect-args " + "'" + ("USER@HOST:" + item.manifest_path.slice(0, item.manifest_path.lastIndexOf("/") + 1)).replace(/'/g, "'\\''") + "' ./downloads/"}</pre></>}
        <table><thead><tr><th>文件</th><th>行数</th><th>字节数</th></tr></thead><tbody>{item.files.map(file => <tr key={file.path}>
          <td className="file-path">{file.path}<small>SHA-256: {file.sha256}</small></td><td>{count(file.row_count)}</td><td>{count(file.size_bytes)}</td></tr>)}</tbody></table></details>)}
      <h3>登记训练使用</h3><label>训练运行名称<input value={run} onChange={e => setRun(e.target.value)} /></label>
      <p>登记后，这个发布的训练划分会参与后续验证/测试集的泄漏检查。</p>
      <button disabled={!enabled || !run.trim()} onClick={() => act("training", { run_name: run })}>登记训练</button>
      <State loading={training.loading && !training.data} error={training.error} />
      {training.data?.map(item => <p key={item.run_name}>{item.run_name} · {date(item.used_at)}</p>)}
    </div>}
  </section>;
}
