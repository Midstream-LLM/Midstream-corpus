import { useState } from "react";
import { api, count, useLoad } from "./shared";
type Engine = {id: string; label: string; available: boolean; note: string};
type Status = {engine: string; pages: number; cached: number; remaining: number; files: {path: string; pages: number; cached: number}[]};
// 扫描件分组：选引擎、查看已识别页数、提交 OCR 任务。入库只读 OCR 缓存，不会触发识别。
export default function OcrPanel({paths, engine, actor, disabled, onEngine}: {
  paths: string[]; engine: string; actor: string; disabled: boolean; onEngine: (id: string) => void;
}) {
  const engines = useLoad<Engine[]>("ocr/engines");
  const [status, setStatus] = useState<Status | null>(null), [limit, setLimit] = useState(200),
    [busy, setBusy] = useState(false), [message, setMessage] = useState("");
  const current = engines.data?.find(e => e.id === engine);
  async function check() {
    setBusy(true); setMessage("");
    try {setStatus(await api<Status>("ocr/status", undefined, {paths, engine}));}
    catch(e) {setMessage((e as Error).message);} finally {setBusy(false);}
  }
  async function submit() {
    setBusy(true); setMessage("");
    try {
      const job = await api<{job_id: number}>("jobs", undefined, {kind: "ocr", params: {paths, engine, max_pages: limit || null},
        actor, reason: `扫描件 OCR（${current?.label || engine}，最多 ${limit} 页）`});
      setMessage(`已提交任务 #${job.job_id}，完成后点“查看 OCR 进度”刷新，全部页识别完再登记入库。`);
    } catch(e) {setMessage((e as Error).message);} finally {setBusy(false);}
  }
  return <div className="change-preview">
    <div className="edit-grid">
      <label>OCR 引擎<select disabled={disabled} value={engine} onChange={e => {onEngine(e.target.value); setStatus(null);}}>
        {(engines.data || [{id: engine, label: engine, available: true, note: ""}]).map(e => <option key={e.id} value={e.id} disabled={!e.available}>{e.label}{e.available ? "" : "（未配置）"}</option>)}
      </select></label>
      <label>本次最多识别页数<input type="number" min={1} disabled={disabled} value={limit} onChange={e => setLimit(Number(e.target.value))}/></label>
    </div>
    {current?.note && <p>{current.note}</p>}
    {status && <p>已识别 {count(status.cached)} / {count(status.pages)} 页，还剩 {count(status.remaining)} 页。{status.remaining > 0 && "未识别完的文件登记后会验证失败。"}</p>}
    <div className="actions">
      <button disabled={disabled || busy} onClick={check}>查看 OCR 进度</button>
      <button disabled={disabled || busy || !actor.trim() || !current?.available || (status !== null && status.remaining === 0)} onClick={submit}>提交 OCR 任务</button>
    </div>
    {!actor.trim() && <p>请先在页面下方填写操作人。</p>}
    {message && <p role="status">{message}</p>}
  </div>;
}
