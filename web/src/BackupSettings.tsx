import { useEffect,useState } from "react";
import { api,useLoad,State,date,count } from "./shared";
type Config={endpoint:string;bucket:string;prefix:string;schedule:string;keep:number;include_config:boolean};
type Settings={config:Config;revision:string;credentials_status:Record<string,{configured:boolean;source:string;local_saved:boolean}>};
type Backup={backup_id:number;status:string;object_key:string;database_name:string;byte_count:number|null;created_at:string;verified_at:string|null;error:string|null};
const labels:Record<string,string>={access_key_id:"AccessKey ID",access_key_secret:"AccessKey Secret",security_token:"STS Token（可选）"};
export default function BackupSettings({onJob}:{onJob:(id:number)=>void}) {
  const [tick,setTick]=useState(0),[config,setConfig]=useState<Config|null>(null),[revision,setRevision]=useState(""),
    [keys,setKeys]=useState<Record<string,string>>({}),[clear,setClear]=useState<string[]>([]),[actor,setActor]=useState(""),
    [reason,setReason]=useState(""),[busy,setBusy]=useState(false),[message,setMessage]=useState("");
  const loaded=useLoad<Settings>("backup/settings",tick),history=useLoad<Backup[]>("backups",tick);
  useEffect(()=>{if(loaded.data){setConfig(loaded.data.config);setRevision(loaded.data.revision);setKeys({});setClear([]);}},[loaded.data]);
  async function save(){
    setBusy(true);setMessage("");
    try{await api("backup/settings",undefined,{config,keys:Object.fromEntries(Object.entries(keys).filter(([,v])=>v)),clear_keys:clear,revision});setTick(n=>n+1);setMessage("备份设置已保存。");}
    catch(e){setMessage((e as Error).message);}finally{setBusy(false);}
  }
  async function act(path:string,test=false){
    setBusy(true);setMessage("");
    try{const result=await api<{job_id?:number}>(
      path,undefined,test?{}:{actor,reason});if(result.job_id)onJob(result.job_id);else setMessage("连接成功。");}
    catch(e){setMessage((e as Error).message);}finally{setBusy(false);}
  }
  const allowed=!!actor.trim()&&!!reason.trim()&&!busy;
  return <section className="metadata settings-card"><div className="panel-heading"><h2>备份设置 · 阿里云 OSS</h2><button disabled={busy} onClick={()=>setTick(n=>n+1)}>重新加载备份设置</button></div>
    <p>默认不自动备份。启用每日/每周后，需要 worker 常驻；若最近一个周期没有备份任务，首次检查会立即提交。</p>
    <State loading={loaded.loading&&!loaded.data} error={loaded.error}/>
    {config&&<>
      <div className="edit-grid">{Object.entries({endpoint:"OSS Endpoint",bucket:"Bucket",prefix:"对象前缀"}).map(([key,label])=>
        <label key={key}>{label}<input value={config[key as "endpoint"]} onChange={e=>setConfig({...config,[key]:e.target.value})}/></label>)}
        <label>自动备份<select value={config.schedule} onChange={e=>setConfig({...config,schedule:e.target.value})}><option value="none">不自动（默认）</option><option value="daily">每日</option><option value="weekly">每周</option></select></label>
        <label>保留份数<input type="number" min="1" value={config.keep} onChange={e=>setConfig({...config,keep:Number(e.target.value)})}/></label></div>
      <label className="check"><input type="checkbox" checked={config.include_config} onChange={e=>setConfig({...config,include_config:e.target.checked})}/>同时备份配置文件（可能包含本地保存的密钥）</label>
      <div className="edit-grid">{Object.entries(labels).map(([key,label])=><label key={key}>{label}
        <input type="password" autoComplete="new-password" value={keys[key]||""} placeholder="留空保留，保存后不回显" onChange={e=>setKeys({...keys,[key]:e.target.value})}/>
        <small>{loaded.data?.credentials_status[key]?.configured?"已配置":"未配置"} · {loaded.data?.credentials_status[key]?.source==="environment"?"环境变量优先":loaded.data?.credentials_status[key]?.source==="local"?"本地文件":"未设置"}</small>
        <span className="check"><input type="checkbox" checked={clear.includes(key)} onChange={e=>setClear(e.target.checked?[...clear,key]:clear.filter(x=>x!==key))}/>清除本地值</span></label>)}</div>
      <div className="actions"><button className="primary" disabled={busy} onClick={save}>保存备份设置</button><button disabled={busy||!loaded.data?.config.endpoint||!loaded.data?.config.bucket} onClick={()=>act("backup/test",true)}>测试已保存连接</button></div>
    </>}
    {message&&<p role="status" className="notice">{message}</p>}
    <h3>手动备份与历史</h3><div className="edit-grid"><label>操作人<input value={actor} onChange={e=>setActor(e.target.value)}/></label><label>操作依据<input value={reason} onChange={e=>setReason(e.target.value)}/></label></div>
    <button disabled={!allowed} onClick={()=>act("backups")}>提交备份任务</button>
    <State loading={history.loading&&!history.data} error={history.error} empty={history.data?.length===0}/>
    <div className="table-scroll"><table><thead><tr><th>备份 / 时间</th><th>状态 / 字节数</th><th>对象与校验</th></tr></thead><tbody>{history.data?.map(item=><tr key={item.backup_id}>
      <td>#{item.backup_id} · {item.database_name}<small>{date(item.created_at)}</small></td>
      <td>{({running:"备份中",completed:"已完成",failed:"失败",deleting:"清理中",deleted:"已清理"} as Record<string,string>)[item.status]}<small>{item.byte_count===null?"未知":count(item.byte_count)}</small>{item.error&&<p className="error">{item.error}</p>}</td>
      <td className="file-path">{item.object_key}<small>校验：{date(item.verified_at)}</small><button disabled={!allowed||item.status!=="completed"} onClick={()=>act("backups/"+item.backup_id+"/verify")}>校验备份目录</button></td></tr>)}</tbody></table></div>
  </section>;
}
