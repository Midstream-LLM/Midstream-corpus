import { useState } from "react";
import { api, useLoad, State, count, show } from "./shared";
type Preview = { source: string; parser_version: string; objects: {path: string; size_bytes: number; sha256: string}[];
  fill_rates: {field: string; filled: number; total: number}[];
  samples: {file: string; position: string; source_key: string; metadata: Record<string,unknown>; error: string | null; text_preview: string}[] };
export default function Import({onRegistered}: {onRegistered: (id: number) => void}) {
  const adapters = useLoad<{name: string; description: string; license_note: string}[]>("import/adapters");
  const [source, setSource] = useState(""), [paths, setPaths] = useState(""), [actor, setActor] = useState(""), [reason, setReason] = useState("");
  const [preview, setPreview] = useState<Preview | null>(null), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [planText, setPlanText] = useState(""), [detection, setDetection] = useState<{path: string; known: boolean; source: string | null; adapter: string | null; format: string; plan: unknown; note: string; method?: string; model?: string}[]>([]);
  function plan() { return planText.trim() ? JSON.parse(planText) : null; }
  async function detect(use_llm: boolean) {
    setBusy(true); setError(""); setPreview(null); setDetection([]);
    try {
      const result = await api<{items: typeof detection}>("import/detect", undefined, {paths: names, use_llm});
      setDetection(result.items);
    } catch(e) {setError((e as Error).message);}
    finally {setBusy(false);}
  }
  const names = paths.split("\n").map(p => p.trim()).filter(Boolean);
  async function submit(register: boolean) {
    setBusy(true); setError("");
    try {
      if (register && preview) {
        const result = await api<{batch_id: number}>("import/register", undefined, {source, plan: plan(), paths: names, objects: preview.objects, parser_version: preview.parser_version, actor, reason});
        setPreview(null); setPaths(""); onRegistered(result.batch_id);
      } else {
        setPreview(null);
        setPreview(await api<Preview>("import/preview", undefined, {source, plan: plan(), paths: names}));
      }
    } catch(e) {setError((e as Error).message + (register ? "；请刷新批次列表核实是否已登记，再重试。" : ""));}
    finally {setBusy(false);}
  }
  return <details className="metadata"><summary>新建入库批次</summary>
    <p>填写后端机器上的原文件绝对路径，每行一个（最多 20 个）。文件保持原位；登记后请保留原文件。先识别已有来源；未匹配的文件可使用 LLM 建议字段映射。</p>
    <State loading={adapters.loading} error={adapters.error || error}/>
    <div className="edit-grid"><label>来源格式<select disabled={busy} value={source} onChange={e => {setSource(e.target.value); setPlanText(""); setPreview(null);}}><option value="">请选择</option>{source && !["custom", ...(adapters.data?.map(a => a.name) || [])].includes(source) && <option value={source}>{source}</option>}<option value="custom">自定义来源（使用映射方案）</option>{adapters.data?.map(a => <option key={a.name} value={a.name}>{a.name} · {a.description}</option>)}</select></label>
    <label>文件路径<textarea disabled={busy} value={paths} rows={4} onChange={e => {setPaths(e.target.value); setDetection([]); setPlanText(""); setSource(""); setPreview(null);}} placeholder="/data/incoming/corpus.jsonl"/></label></div>
    <div className="actions"><button disabled={busy || !names.length || names.length > 20} onClick={() => detect(false)}>识别已有格式</button><button disabled={busy || !names.length || names.length > 20} onClick={() => detect(true)}>识别并用 LLM 分析未知格式</button></div>
    <p>LLM 识别会把未匹配文件的有限内容样本发送至已配置的模型接入点。二进制文件不会发送。</p>
    {detection.map((d,i) => <article key={i} className="change-preview"><b>{d.path}</b><p>{d.known ? "已知来源规则" : d.method === "llm" ? `LLM 建议 · ${d.model}` : "未匹配"} · {d.format} · {d.source || "请指定来源名称"}</p><p>{d.note}</p>{d.plan != null && <pre>{JSON.stringify(d.plan,null,2)}</pre>}<button disabled={busy || (!d.adapter && !d.plan)} onClick={() => {setSource(d.source || "custom"); setPlanText(d.plan ? JSON.stringify(d.plan,null,2) : ""); setPreview(null);}}>采用此方案并预览（应用于当前全部文件）</button></article>)}
    {(planText || (source && !adapters.data?.some(a => a.name === source))) && <div className="edit-grid"><label>来源名称<input disabled={busy} value={source} onChange={e => {setSource(e.target.value); setPreview(null);}}/></label><label>导入映射方案（JSON）<textarea disabled={busy} rows={8} value={planText} onChange={e => {setPlanText(e.target.value); setPreview(null);}}/></label><p>同一批次使用同一来源和方案。混合格式请分批导入；字段映射不会猜测作者、年份或许可证。</p></div>}
    {adapters.data?.filter(a => a.name === source).map(a => <p key={a.name}>来源许可证说明：{a.license_note || "未知"}</p>)}
    <button disabled={busy || !source || !names.length || names.length > 20} onClick={() => submit(false)}>{busy ? "处理中…" : "检查文件并预览"}</button>
    {preview && <div className="change-preview"><h3>登记确认</h3><p>解析器：{preview.parser_version}。按首、中、尾窗口抽样，Parquet 按行组抽样；正文最多 500 字。这是抽样填充率，不是全量校验结果。</p>
      {preview.objects.map(o => <details key={o.path}><summary>{o.path} · {count(o.size_bytes)} 字节</summary><pre>SHA-256: {o.sha256}</pre></details>)}
      <table><thead><tr><th>映射字段</th><th>已填样本 / 总样本</th><th>填充率</th></tr></thead><tbody>{preview.fill_rates.map(rate => <tr key={rate.field}><td>{rate.field}</td><td>{rate.filled} / {rate.total}</td><td>{rate.total ? (100*rate.filled/rate.total).toFixed(1) : 0}%</td></tr>)}</tbody></table>
      <div className="table-scroll"><table><thead><tr><th>文件 / 位置 / 来源键</th><th>映射后的元数据</th><th>正文样本 / 问题</th></tr></thead><tbody>{preview.samples.map((s,i) => <tr key={i}>
        <td>{s.file.split("/").pop()}<small>{s.position} · {s.source_key || "来源键待暂存确定"}</small></td>
        <td><dl className="fields">{Object.entries(s.metadata).filter(([key]) => key !== "field_basis" && key !== "extra").map(([key,value]) => <div key={key}><dt>{key}</dt><dd>{show(value)}</dd></div>)}</dl></td>
        <td><p className="text-preview">{s.text_preview}</p>{s.error && <p className="error">{s.error}</p>}</td></tr>)}</tbody></table></div>
      {!preview.samples.length && <p>没有解析出样本，请检查文件与来源格式。</p>}
      <div className="edit-grid"><label>操作人<input disabled={busy} value={actor} onChange={e => setActor(e.target.value)}/></label><label>入库依据<input disabled={busy} value={reason} onChange={e => setReason(e.target.value)}/></label></div>
      <p>确认后仅登记批次。随后先暂存验证，查看第二层预览，再确认发布。</p>
      <button className="primary" disabled={busy || !actor.trim() || !reason.trim() || !preview.samples.length} onClick={() => submit(true)}>确认登记批次</button>
    </div>}
  </details>;
}
