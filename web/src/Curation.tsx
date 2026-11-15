import { useState } from 'react';
import { api, useLoad, State, show, date } from './shared';
import { savedActor } from './importDraft';

export type CurationConfig = {profile: string | null; search_profile: string | null; use_search: boolean;
  use_authorities: boolean; apply: boolean; overwrite: boolean; allow_text_edits: boolean; force: boolean;
  confidence_threshold: number; max_tool_calls: number; max_text_chars: number; context_chars: number; chunk_chars: number;
  steps: string[]};
export const defaultCuration = (): CurationConfig => ({profile:null, search_profile:null, use_search:true,
  use_authorities:true, apply:true, overwrite:false, allow_text_edits:false, force:false,
  confidence_threshold:0.9, max_tool_calls:6, max_text_chars:400000, context_chars:16000, chunk_chars:12000,
  steps:['metadata_enrichment','tagging','text_cleanup','segmentation']});
export const stepLabels: Record<string,string> = {basic_clean:'基础清洗', metadata_enrichment:'书目补全',
  tagging:'标签', text_cleanup:'文本质检与清洗', segmentation:'篇章识别', ai_curation:'完整 AI 整理',
  t2s:'繁转简', manual_edit:'人工修订', quality_check:'规则质检', collation_clean:'校勘清洗'};
const statusLabels: Record<string,string> = {pending:'等待执行', proposed:'候选待应用', applied:'已应用',
  rolled_back:'已撤销', reverted:'已撤销', failed:'执行失败', conflict:'冲突', withheld:'保留候选'};

export function CurationOptions({value,onChange,disabled=false,ingestion=false}: {
  value:CurationConfig; onChange:(value:CurationConfig)=>void; disabled?:boolean; ingestion?:boolean}) {
  const update = (key: keyof CurationConfig, next: unknown) => onChange({...value,[key]:next});
  return <div className="curation-options"><p>书目查询 → 网页考证 → 字段、标签与文本处理。人工审核可在处理后按需进行。</p>
    <div className="edit-grid">
      <label>整理模型 profile<input disabled={disabled} value={value.profile || ''} placeholder="使用设置中的语料整理服务" onChange={e=>update('profile',e.target.value || null)}/></label>
      <label>搜索 profile<input disabled={disabled} value={value.search_profile || ''} placeholder="使用设置中的网页搜索服务" onChange={e=>update('search_profile',e.target.value || null)}/></label>
      <label>自动应用置信度阈值<input disabled={disabled} type="number" min={0} max={1} step={0.01} value={value.confidence_threshold} onChange={e=>update('confidence_threshold',Number(e.target.value))}/></label>
      <label>每条记录检索上限<input disabled={disabled} type="number" min={0} max={20} value={value.max_tool_calls} onChange={e=>update('max_tool_calls',Number(e.target.value))}/></label>
      <label>逐段修复和切分的字数上限<input disabled={disabled} type="number" min={1000} max={50000000} value={value.max_text_chars} onChange={e=>update('max_text_chars',Number(e.target.value))}/></label>
    </div>
    <div className="curation-checks">{([
      ['use_authorities','结构化书目查询'],['use_search','网页搜索与阅读'],
      ['overwrite','允许校正已有字段'],['allow_text_edits','自动应用有证据的原文修复'],
      ...(!ingestion ? [['apply','自动应用结果'],['force','重新处理已完成的记录']] : [])
    ] as [keyof CurationConfig,string][]).map(([key,label])=><label key={key}><input type="checkbox" disabled={disabled}
      checked={Boolean(value[key])} onChange={e=>update(key,e.target.checked)}/>{label}</label>)}</div>
    <p className="muted">置信度为模型自评；系统另行校验证据、原文位置和字段冲突。预算耗尽保留未完成状态，未知字段保留为空。</p>
  </div>;
}

type Claim = {claim_id:number; kind:string; field:string; value:unknown; confidence:number; confidence_type:string;
  rationale:string; source_kind:string; application_status:string; human_reviewed:boolean; review_outcome:string|null; reviewed_by:string|null};
type Item = {item_id:number; run_id:number; doc_id:number|null; record_id:number|null; base_version_id:number|null;
  result_version_id:number|null; status:string; summary:string; error_code:string|null; completed_steps:Record<string,boolean>; claims:Claim[]};
type Run = {run_id:number;batch_id:number|null;started_at:string;counts:Record<string,number>|null};

function Evidence({claimId}:{claimId:number}) {
  const data=useLoad<{items:{evidence_id:number;provider:string;reference:string;content:{title:string;excerpt:string;data:unknown}}[]}>(`curation/claims/${claimId}/evidence`);
  return <><State loading={data.loading} error={data.error}/>{data.data?.items.map(e=><div className="change-preview" key={e.evidence_id}>
    <strong>{e.content.title || e.provider}</strong><p>{/^https?:\/\//.test(e.reference) ? <a href={e.reference} target="_blank" rel="noreferrer">{e.reference}</a> : e.reference}</p>
    <pre>{e.content.excerpt || show(e.content.data)}</pre></div>)}</>;
}

export function CurationItem({id,onChanged}:{id:number;onChanged?:()=>void}) {
  const data=useLoad<Item>(`curation/items/${id}`);
  const [open,setOpen]=useState<number|null>(null), [error,setError]=useState(''),[busy,setBusy]=useState(false);
  const [actor,setActor]=useState(savedActor),[reason,setReason]=useState('');
  async function review(claim:Claim,outcome:string) {
    setBusy(true);setError('');
    try {await api(`curation/claims/${claim.claim_id}/review`,undefined,{actor,reason:reason || '整理结果复核',outcome});data.refresh();onChanged?.();}
    catch(e){setError((e as Error).message);}finally{setBusy(false);}
  }
  async function apply() {
    setBusy(true);setError('');
    try {await api(`curation/items/${id}/apply`,undefined,{actor,reason:reason || '应用整理候选'});data.refresh();onChanged?.();}
    catch(e){setError((e as Error).message);}finally{setBusy(false);}
  }
  async function rollback() {
    setBusy(true);setError('');
    try {await api(`curation/items/${id}/rollback`,undefined,{actor,reason:reason || '撤销本次整理'});data.refresh();onChanged?.();}
    catch(e){setError((e as Error).message);}finally{setBusy(false);}
  }
  return <section className="panel"><State loading={data.loading} error={data.error}/>{data.data && <>
    <h3>工作项 #{id} · {statusLabels[data.data.status]}</h3><p>{data.data.summary}</p>
    {data.data.error_code && <p className="error">{data.data.error_code}</p>}
    <p>原版本 {data.data.base_version_id || '暂存'} → 结果版本 {data.data.result_version_id || '待发布/应用'}</p>
    <div className="edit-grid"><label>审核操作人（可选操作）<input value={actor} onChange={e=>setActor(e.target.value)}/></label>
      <label>备注<input value={reason} onChange={e=>setReason(e.target.value)}/></label></div>
    {data.data.status==='proposed' && <button disabled={busy || !actor.trim()} onClick={apply}>应用符合策略的候选</button>}
    {data.data.status==='applied' && data.data.result_version_id && <button disabled={busy || !actor.trim()} onClick={rollback}>撤销本次整理，生成恢复版本</button>}
    <p className="muted">审核记录与应用状态独立。撤销会恢复本次整理前的文本、元数据和处理标记；已有后续修订时，请在文档中修正具体字段。</p>
    {data.data.claims.map(c=><article className="change-preview" key={c.claim_id}>
      <strong>{c.field} · {statusLabels[c.application_status]} · {Math.round(c.confidence*100)}% 模型自评</strong>
      <pre>{show(c.value)}</pre><p>{c.rationale}</p><p>{c.human_reviewed ? `${c.review_outcome==='confirmed'?'已确认':'已否决'} · ${c.reviewed_by}` : '未经人工审核'}</p>
      <div className="actions"><button onClick={()=>setOpen(open===c.claim_id?null:c.claim_id)}>查看证据</button>
        <button disabled={busy || !actor.trim()} onClick={()=>review(c,'confirmed')}>确认结论</button>
        <button disabled={busy || !actor.trim()} onClick={()=>review(c,'rejected')}>否决结论</button></div>
      {open===c.claim_id && <Evidence claimId={c.claim_id}/>}</article>)}
  </>}{error && <p role="alert" className="error">{error}</p>}</section>;
}

export default function Curation({onJob}:{onJob:(id:number)=>void}) {
  const [config,setConfig]=useState(defaultCuration),[actor,setActor]=useState(savedActor),[source,setSource]=useState('');
  const [limit,setLimit]=useState(100),[batch,setBatch]=useState(''),[ids,setIds]=useState('');
  const [error,setError]=useState(''),[busy,setBusy]=useState(false),[run,setRun]=useState<number|null>(null),[item,setItem]=useState<number|null>(null);
  const [runBefore,setRunBefore]=useState<number|null>(null),[after,setAfter]=useState(0);
  const runs=useLoad<{items:Run[];next_cursor:number|null}>(`curation/runs${runBefore?'?before='+runBefore:''}`);
  const items=useLoad<{items:Item[];next_cursor:number|null}>(run?`curation/items?run_id=${run}&after=${after}`:null);
  async function start(resume?:Run) {
    setBusy(true);setError('');
    try {
      const params=resume ? {resume_run_id:resume.run_id,batch_id:resume.batch_id} : {
        config,limit,batch_id:batch?Number(batch):null,filters:source?{sources:source.split(',').map(s=>s.trim()).filter(Boolean)}:{},
        doc_ids:ids?ids.split(/[,\s]+/).filter(Boolean).map(Number):null};
      const job=await api<{job_id:number}>('curation/runs',undefined,{actor,reason:resume?'恢复整理运行':'自动清洗与标注',params});
      onJob(job.job_id);
    }catch(e){setError((e as Error).message);}finally{setBusy(false);}
  }
  return <div className="page"><div className="page-heading"><h1>数据整理</h1><p>默认处理尚未完成 AI 整理的记录。原文、旧版本、字段证据和审核记录均可追溯。</p></div>
    <section className="panel"><h2>新建整理任务</h2><div className="edit-grid">
      <label>来源（逗号分隔，留空为全部）<input value={source} disabled={!!batch} onChange={e=>setSource(e.target.value)}/></label>
      <label>文档 ID（选填）<input value={ids} disabled={!!batch} onChange={e=>setIds(e.target.value)}/></label>
      <label>最多处理文档数<input type="number" min={1} max={1000} value={limit} disabled={!!batch} onChange={e=>setLimit(Number(e.target.value))}/></label>
      <label>暂存批次 ID（选填，处理整个批次）<input type="number" min={1} value={batch} onChange={e=>{setBatch(e.target.value);setSource('');setIds('');}}/></label>
      <label>操作人<input value={actor} onChange={e=>setActor(e.target.value)}/></label></div>
      <CurationOptions value={config} onChange={setConfig} disabled={busy}/>
      <button className="primary" disabled={busy || !actor.trim()} onClick={()=>start()}>开始清洗与标注</button>
      {error && <p className="error" role="alert">{error}</p>}</section>
    <section className="panel"><h2>运行记录 <button onClick={runs.refresh}>刷新</button></h2><State loading={runs.loading} error={runs.error}/>
      <table><thead><tr><th>运行</th><th>时间</th><th>结果</th><th>操作</th></tr></thead><tbody>{runs.data?.items.map(r=><tr key={r.run_id}>
        <td>#{r.run_id}{r.batch_id ? ` · 批次 ${r.batch_id}`:''}</td><td>{date(r.started_at)}</td><td>{Object.entries(r.counts || {}).map(([k,n])=>`${statusLabels[k]} ${n}`).join(' / ') || '无待处理记录'}</td>
        <td><button onClick={()=>{setRun(r.run_id);setAfter(0);setItem(null);}}>查看</button> <button disabled={busy || !actor.trim()} onClick={()=>start(r)}>恢复未完成项</button></td></tr>)}</tbody></table>
      {runs.data?.next_cursor && <button onClick={()=>setRunBefore(runs.data!.next_cursor)}>更早运行</button>}
      {runBefore && <button onClick={()=>setRunBefore(null)}>最新运行</button>}</section>
    {run && <section className="panel"><h2>运行 #{run} <button onClick={items.refresh}>刷新</button></h2><State loading={items.loading} error={items.error}/>
      {items.data?.items.map(i=><p key={i.item_id}><button onClick={()=>setItem(i.item_id)}>工作项 {i.item_id} · {i.doc_id?`文档 ${i.doc_id}`:`暂存 ${i.record_id}`} · {statusLabels[i.status]}</button> {i.error_code}</p>)}
      {items.data?.next_cursor && <button onClick={()=>setAfter(items.data!.next_cursor!)}>下一页</button>}{after>0 && <button onClick={()=>setAfter(0)}>第一页</button>}</section>}
    {item && <CurationItem key={item} id={item} onChanged={()=>{items.refresh();runs.refresh();}}/>}
  </div>;
}
