import { useEffect,useState } from "react";
import { api,useLoad,State,count,date } from "./shared";
type Cluster={cluster_id:number;size:number;chars:number;kind:string;sources:string[]};
type Member={doc_id:number;version_id:number;title:string|null;source:string;char_count:number};
export default function Duplicates({onJob}:{onJob:(id:number)=>void}) {
  const [kind,setKind]=useState(""),[source,setSource]=useState(""),[minimum,setMinimum]=useState(2),[maximum,setMaximum]=useState(""),
    [before,setBefore]=useState<number|null>(null),[history,setHistory]=useState<(number|null)[]>([]),[id,setId]=useState<number|null>(null),
    [after,setAfter]=useState(0),[memberHistory,setMemberHistory]=useState<number[]>([]),[left,setLeft]=useState(""),[right,setRight]=useState(""),
    [pair,setPair]=useState(""),[actor,setActor]=useState(""),[reason,setReason]=useState(""),[busy,setBusy]=useState(false),[message,setMessage]=useState("");
  const sources=useLoad<{name:string}[]>("sources");
  const params=new URLSearchParams({min_size:String(minimum),...(kind?{kind}:{}),...(source?{source}:{}),...(maximum?{max_size:maximum}:{}),...(before?{before:String(before)}:{})});
  const list=useLoad<{items:Cluster[];next_cursor:number|null}>("duplicates/clusters?"+params);
  const detail=useLoad<{members:Member[];next_cursor:number|null}>(id===null?null:"duplicates/clusters/"+id+"?after="+after);
  const compare=useLoad<{left:string;right:string;diff:string;truncated:boolean}>(pair?"duplicates/compare?"+pair:null);
  const overrides=useLoad<{override_id:number;left_doc_id:number;right_doc_id:number;actor:string;reason:string;created_at:string}[]>("duplicates/overrides");
  useEffect(()=>{if(detail.data?.members.length&&!left&&!right){setLeft(String(detail.data.members[0].doc_id));setRight(String(detail.data.members[1]?.doc_id||""));}},[detail.data]);
  const allowed=!!actor.trim()&&!!reason.trim()&&!busy;
  async function run(kind:string,deny?:object) {
    setBusy(true);setMessage("");
    try {
      if(deny) await api("duplicates/overrides",undefined,{...deny,actor,reason});
      const job=await api<{job_id:number}>("jobs",undefined,{kind,params:{},actor,reason});
      onJob(job.job_id);
    } catch(e){setMessage((e as Error).message);}finally{setBusy(false);}
  }
  function reset(){setBefore(null);setHistory([]);}
  return <>
    <section className="heading"><div><div className="eyebrow">证明 · 比较 · 人工决议</div><h1>重复浏览</h1><p>人工否决按文档对保留。重算按正向证明合并，并阻止被否决两端经第三篇重新连接。</p></div></section>
    <div className="edit-grid"><label>操作人<input value={actor} onChange={e=>setActor(e.target.value)}/></label><label>操作依据<input value={reason} onChange={e=>setReason(e.target.value)}/></label></div>
    <div className="actions"><button disabled={!allowed} onClick={()=>run("dedup")}>文档查重</button><button disabled={!allowed} onClick={()=>run("dedup_spans")}>段落查重</button></div>
    {message&&<p role="alert" className="notice">{message}</p>}
    <div className="edit-grid"><label>类型<select value={kind} onChange={e=>{setKind(e.target.value);reset();}}><option value="">全部</option><option value="exact">完全重复</option><option value="near">近重复</option></select></label>
      <label>来源<select value={source} onChange={e=>{setSource(e.target.value);reset();}}><option value="">全部</option>{sources.data?.map(s=><option key={s.name}>{s.name}</option>)}</select></label>
      <label>最少成员<input type="number" min="2" value={minimum} onChange={e=>{setMinimum(Number(e.target.value));reset();}}/></label>
      <label>最多成员<input type="number" min="2" value={maximum} placeholder="不限" onChange={e=>{setMaximum(e.target.value);reset();}}/></label></div>
    <div className="workspace"><section className="catalog"><div className="panel-heading"><h2>文档重复簇</h2></div>
      <State loading={list.loading&&!list.data} error={list.error} empty={list.data?.items.length===0}/>
      <table><thead><tr><th>簇</th><th>成员 / 字数</th><th>来源</th></tr></thead><tbody>{list.data?.items.map(item=><tr key={item.cluster_id}>
        <td><button className="title" onClick={()=>{setId(item.cluster_id);setAfter(0);setMemberHistory([]);setLeft("");setRight("");setPair("");}}>#{item.cluster_id} · {item.kind==="exact"?"完全重复":"近重复"}</button></td>
        <td>{count(item.size)} 篇<small>{count(item.chars)} 字</small></td><td>{item.sources.join("、")}</td></tr>)}</tbody></table>
      <div className="actions"><button disabled={!history.length} onClick={()=>{setBefore(history[history.length-1]);setHistory(history.slice(0,-1));}}>上一页</button>
        <button disabled={!list.data?.next_cursor} onClick={()=>{setHistory([...history,before]);setBefore(list.data!.next_cursor);}}>下一页</button></div>
    </section><section className="reader"><div className="panel-heading"><h2>{id===null?"选择重复簇":"重复簇 #"+id}</h2></div>
      <State loading={detail.loading&&!detail.data} error={detail.error}/>
      {detail.data&&<div className="metadata"><table><thead><tr><th>文档 / 来源</th><th>字数</th><th>比较 / 否决</th></tr></thead><tbody>{detail.data.members.map(item=><tr key={item.doc_id}>
        <td>#{item.doc_id} · {item.title||"未题名"}<small>{item.source} · v{item.version_id}</small></td><td>{count(item.char_count)}</td><td>
          <button onClick={()=>setLeft(String(item.doc_id))}>选左侧</button><button onClick={()=>setRight(String(item.doc_id))}>选右侧</button>
          <button disabled={!allowed} onClick={()=>run("dedup",{left_doc_id:item.doc_id,cluster_id:id})}>否决此成员与全簇重复并重算</button></td></tr>)}</tbody></table>
        <div className="actions"><button disabled={!memberHistory.length} onClick={()=>{setAfter(memberHistory[memberHistory.length-1]);setMemberHistory(memberHistory.slice(0,-1));}}>上一批成员</button>
          <button disabled={!detail.data.next_cursor} onClick={()=>{setMemberHistory([...memberHistory,after]);setAfter(detail.data!.next_cursor!);}}>下一批成员</button></div>
        <div className="edit-grid"><label>左侧文档 ID<input type="number" min="1" value={left} onChange={e=>setLeft(e.target.value)}/></label>
          <label>右侧文档 ID<input type="number" min="1" value={right} onChange={e=>setRight(e.target.value)}/></label></div>
        <div className="actions"><button disabled={!left||!right} onClick={()=>setPair(new URLSearchParams({left,right,length:"10000"}).toString())}>比较两篇</button>
          <button disabled={!allowed||!left||!right||left===right} onClick={()=>run("dedup",{left_doc_id:Number(left),right_doc_id:Number(right)})}>否决此文档对并重算</button></div>
        <State loading={compare.loading&&!compare.data} error={compare.error}/>{compare.data&&<>
          {compare.data.truncated&&<p>仅比较各侧前 10000 字。</p>}<div className="diff"><pre>{compare.data.left}</pre><pre>{compare.data.right}</pre></div><pre>{compare.data.diff||"预览范围内相同"}</pre></>}
      </div>}
    </section></div>
    <details><summary>最近人工否决（最多 100 条）</summary>{overrides.data?.map(item=><p key={item.override_id}>#{item.left_doc_id} ↔ #{item.right_doc_id} · {item.actor} · {item.reason} · {date(item.created_at)}</p>)}</details>
  </>;
}
