import { useState } from "react";
import { useLoad, State, count, show } from "./shared";
type Summary = { counts: Record<string,{documents:number; chars:number|null}>; exact_match_documents:number;
  quality:{value:string|null; documents:number; chars:number}[]; lengths:{value:string; documents:number}[]; eras:{value:string|null; documents:number}[] };
type StagedRecord = { record_id:number;source_key:string;status:string;metadata:{[key:string]:unknown}|null;
  char_count:number|null;parse_error:string|null;text:string|null;truncated:boolean };
const names: {[key:string]:string} = {new:"新增",updated:"更新",unchanged:"不变",failed:"失败"};
export default function BatchPreview({id,tick}:{id:number;tick:number}) {
  const [status,setStatus]=useState(""),[after,setAfter]=useState(0),[history,setHistory]=useState<number[]>([]),[record,setRecord]=useState<number|null>(null);
  const summary=useLoad<Summary>("batches/"+id+"/preview",tick);
  const records=useLoad<{items:StagedRecord[];next_cursor:number|null}>("batches/"+id+"/records?after="+after+(status?"&status="+status:""),tick);
  const diff=useLoad<{before:string|null;after:string|null;diff:string;truncated:boolean}>(record===null?null:"batches/"+id+"/records/"+record+"/diff",tick);
  return <section className="settings-card"><h3>发布前预览</h3>
    <p className="notice">此处只读暂存区；近重复和段落重复在发布后计算。</p>
    <State loading={summary.loading&&!summary.data} error={summary.error}/>
    {summary.data&&<>
      <table><thead><tr><th>变更</th><th>篇数</th><th>字数</th></tr></thead><tbody>{Object.entries(summary.data.counts).map(([name,value])=>
        <tr key={name}><td>{names[name]}</td><td>{count(value.documents)}</td><td>{value.chars===null?"未知":count(value.chars)}</td></tr>)}</tbody></table>
      <p>与库内 text_hash 完全相同：{count(summary.data.exact_match_documents)} 篇</p>
      <details><summary>质量、字数与年代分布</summary>{[["quality","质量"],["lengths","字数区间"],["eras","年代标签"]].map(([key,label])=>
        <table key={key}><caption>{label}</caption><thead><tr><th>项目</th><th>篇数</th></tr></thead><tbody>
          {summary.data![key as "quality"].map((item,i)=><tr key={i}><td>{item.value??"未记录"}</td><td>{count(item.documents)}</td></tr>)}</tbody></table>)}</details>
    </>}
    <label>记录状态<select value={status} onChange={e=>{setStatus(e.target.value);setAfter(0);setHistory([]);setRecord(null);}}>
      <option value="">全部</option>{Object.entries(names).map(([key,label])=><option key={key} value={key}>{label}</option>)}</select></label>
    <State loading={records.loading&&!records.data} error={records.error} empty={records.data?.items.length===0}/>
    {records.data&&<>
      <div className="table-scroll"><table><thead><tr><th>来源键 / 状态</th><th>元数据</th><th>清洗后正文</th></tr></thead><tbody>{records.data.items.map(item=>
        <tr key={item.record_id}><td>{item.source_key}<small>{names[item.status]} · {item.char_count===null?"未知":count(item.char_count)} 字</small>
          <button onClick={()=>setRecord(item.record_id)}>清洗前后差异</button></td>
          <td><dl className="fields">{Object.entries(item.metadata||{}).filter(([key])=>!["field_basis","extra"].includes(key)).map(([key,value])=>
            <div key={key}><dt>{key}</dt><dd>{show(value)}</dd></div>)}</dl></td>
          <td><p className="text-preview">{item.text}</p>{item.truncated&&<small>仅展示前 2000 字</small>}{item.parse_error&&<p className="error">{item.parse_error}</p>}</td></tr>)}</tbody></table></div>
      <div className="actions"><button disabled={!history.length} onClick={()=>{setAfter(history[history.length-1]);setHistory(history.slice(0,-1));}}>上一页</button>
        <button disabled={records.data.next_cursor===null} onClick={()=>{setHistory([...history,after]);setAfter(records.data!.next_cursor!);}}>下一页</button></div>
    </>}
    <State loading={diff.loading&&!diff.data} error={diff.error}/>
    {diff.data&&<details open><summary>记录 #{record} 清洗差异</summary>{diff.data.truncated&&<p>对比仅限前 10000 字。</p>}
      <div className="diff"><pre>{diff.data.before}</pre><pre>{diff.data.after}</pre></div><pre>{diff.data.diff||"预览范围内无差异"}</pre></details>}
  </section>;
}
