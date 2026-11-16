import { useState } from "react";
import { api, show } from "./shared";
import { statusLabels, type Group, type Plan } from "./importTypes";
const targets = [["text_field", "正文"], ["id_field", "原文 ID"], ["title", "标题"], ["authors", "作者"], ["composed_label", "年代"], ["category", "体裁 / 分类"]];
export default function ImportGroup({group: g, groups, adapters, actor, disabled, onChange, onMove}: {
  group: Group; groups: Group[]; adapters: {name: string; description: string}[]; actor: string; disabled: boolean; onChange: (g: Group) => void; onMove: (file: string, target: string) => void;
}) {
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [filePage, setFilePage] = useState(0);
  const file = g.files[0], fields = file.fields || {}, plain = ["txt", "lines"].includes(g.plan.format);
  const blocked = (status: string) => ["unsupported", "error", "pending"].includes(status);
  function planChange(plan: Plan) {onChange({...g, plan, json: undefined, approval: {...g.approval, method: g.approval.method === "llm" ? "llm" : "manual", edited: true, requested_by: g.approval.requested_by || actor}});}
  async function suggest() {
    setBusy(true); setError("");
    try {
      const result = await api<{items: {plan: Plan | null; note: string; model: string}[]}>("import/detect", undefined, {paths: [file.path], use_llm: true});
      onChange({...g, suggestion: {...result.items[0], requested_by: actor}});
    } catch(e) {setError((e as Error).message);} finally {setBusy(false);}
  }
  return <article className="import-group">
    <div className="section-title"><div><h3>{g.source || "未命名来源"}</h3><p>{file.format.toUpperCase()} · {g.files.length} 个文件 · 已选 {g.selected.length} 个</p></div>
    <span className={"badge " + (file.known ? "" : "flagged")}>{g.adapter ? "内置解析器" : g.approval.method === "llm" ? "已采用模型建议" : file.known ? "已识别来源" : (plain || g.plan.text_field) ? "映射已配置" : "待确认映射"}</span></div>
    <details><summary>查看文件与入库状态</summary><div className="table-scroll"><table><thead><tr><th>导入</th><th>文件</th><th>状态 / 历史批次</th><th>分组</th></tr></thead><tbody>
      {g.files.slice(filePage * 50, (filePage + 1) * 50).map(f => <tr key={f.path}><td><input type="checkbox" aria-label={"导入 " + f.path} disabled={disabled || blocked(f.import_status)} checked={g.selected.includes(f.path)} onChange={e => onChange({...g, selected: e.target.checked ? [...g.selected, f.path] : g.selected.filter(p => p !== f.path)})}/></td>
      <td title={f.path}>{f.path.split("/").pop()}<small>{f.path}</small></td><td>{statusLabels[f.import_status]}{f.previous_batches?.map(b => <small key={b.batch_id}>#{b.batch_id} · {b.source}</small>)}{f.note && <small>{f.note}</small>}{f.duplicate_of && <small>与 {f.duplicate_of} 内容相同</small>}</td>
      <td><select aria-label={"调整分组 " + f.path} disabled={disabled} value={g.id} onChange={e => onMove(f.path, e.target.value)}><option value={g.id}>当前分组</option><option value="new">拆为新分组</option>{groups.filter(other => other.id !== g.id && other.files[0].signature === f.signature && other.adapter === g.adapter).map(other => <option key={other.id} value={other.id}>{other.source}</option>)}</select></td></tr>)}
    </tbody></table></div><div className="pagination"><button disabled={filePage === 0} onClick={() => setFilePage(filePage - 1)}>上一页</button><span>{filePage + 1} / {Math.max(1, Math.ceil(g.files.length / 50))}</span><button disabled={(filePage + 1) * 50 >= g.files.length} onClick={() => setFilePage(filePage + 1)}>下一页</button></div></details>
    <div className="heading-actions"><button disabled={disabled} onClick={() => onChange({...g, selected: g.files.filter(f => ["new", "changed"].includes(f.import_status)).map(f => f.path)})}>选择新文件和变化文件</button><button disabled={disabled} onClick={() => onChange({...g, selected: []})}>跳过此组</button></div>
    {g.selected.length > 0 && <>
      <label className="import-label">解析方案<select disabled={disabled} value={g.adapter} onChange={e => onChange({...g, adapter: e.target.value,
        source: e.target.value || (g.adapter ? g.files[0].path.split("/").slice(-2, -1)[0] || "custom" : g.source),
        approval: {method: "manual", requested_by: actor}})}><option value="">自定义字段映射</option>{adapters.map(a => <option key={a.name} value={a.name}>{a.name} · {a.description}</option>)}</select></label>
      <label className="import-label">来源名称<input disabled={disabled || !!g.adapter} value={g.source} maxLength={200} onChange={e => onChange({...g, source: e.target.value})}/></label>
      {g.adapter ? <p>使用 {g.adapter} 解析器。预览中可以检查标题、作者、年代和正文。</p> : <>
        <div className="edit-grid"><label>读取方式<select disabled={disabled} value={g.plan.format} onChange={e => planChange({...g.plan, format: e.target.value})}>{["txt", "lines", "json", "jsonl", "parquet"].map(f => <option key={f} value={f}>{({txt: "TXT · 每个文件一篇", lines: "逐行文本 · 每行一篇", json: "JSON", jsonl: "JSONL", parquet: "Parquet"} as Record<string,string>)[f]}</option>)}</select></label><label>文本编码<select disabled={disabled} value={g.plan.encoding} onChange={e => planChange({...g.plan, encoding: e.target.value})}>{["utf-8-sig", "utf-16", "gb18030", "big5hkscs"].map(e => <option key={e} value={e}>{e === "big5hkscs" ? "big5（含香港增补字符）" : e}</option>)}</select></label></div>
        {plain ? <p>直接读取文本正文；TXT 使用文件名作为标题。</p> : <div className="table-scroll"><table className="mapping-table"><thead><tr><th>目标字段</th><th>原文件字段</th><th>样例</th></tr></thead><tbody>{targets.map(([key, label]) => {
          const value = key === "text_field" || key === "id_field" ? g.plan[key] : g.plan.fields[key] || "";
          return <tr key={key}><td>{label}{key === "text_field" && " *"}</td><td><select aria-label={label + "字段"} disabled={disabled} value={value} onChange={e => {
            if (key === "text_field" || key === "id_field") planChange({...g.plan, [key]: e.target.value});
            else {const next = {...g.plan.fields}; if(e.target.value) next[key] = e.target.value; else delete next[key]; planChange({...g.plan, fields: next});}
          }}><option value="">{key === "text_field" ? "请选择正文字段" : "保留未知"}</option>{value && !fields[value] && <option value={value}>{value}</option>}{Object.keys(fields).map(f => <option key={f}>{f}</option>)}</select></td><td>{fields[value]?.example || "—"}</td></tr>;
        })}</tbody></table></div>}
        <details><summary>查看原始样本</summary><pre>{file.sample || "未读取到可展示的样本"}</pre></details>
        <details className="advanced-options"><summary>高级设置 · 映射 JSON</summary><textarea aria-label="映射 JSON" disabled={disabled} rows={8} value={g.json ?? JSON.stringify(g.plan, null, 2)} onChange={e => onChange({...g, json: e.target.value})}/><button disabled={disabled} onClick={() => {
          try {const p = JSON.parse(g.json || JSON.stringify(g.plan)); if(!p || !p.fields || typeof p.format !== "string" || typeof p.text_field !== "string") throw Error("请填写完整映射方案"); planChange(p); setError("");} catch(e) {setError((e as Error).message);}
        }}>应用 JSON</button></details>
        {!file.known && <details className="advanced-options"><summary>使用 LLM 辅助映射</summary><p>发送本组首个文件的有限内容样本至已配置模型。建议确认后应用于本组。</p>
        {!actor.trim() && <p>请先在页面下方填写操作人。</p>}<button disabled={disabled || busy || !actor.trim()} onClick={suggest}>{busy ? "正在分析…" : "分析样本"}</button>
        {g.suggestion && <div className="change-preview"><p>{g.suggestion.model} · {g.suggestion.note}</p><pre>{show(g.suggestion.plan)}</pre><button disabled={disabled || !g.suggestion.plan} onClick={() => onChange({...g, plan: g.suggestion!.plan!, json: undefined, suggestion: undefined,
          approval: {method: "llm", model: g.suggestion!.model, requested_by: g.suggestion!.requested_by}})}>确认采用此映射</button></div>}</details>}
      </>}
    </>}
    {error && <p className="error" role="alert">{error}</p>}
  </article>;
}
