import { useEffect, useRef, useState } from "react";
import { api, State, count, show, useLoad } from "./shared";
import ServerBrowser from "./ServerBrowser";
import { CurationOptions, defaultCuration } from "./Curation";
import ImportGroup from "./ImportGroup";
import { useImportDraft, savedActor } from "./importDraft";
import { emptyDraft, groupFiles, type Scan, type Group, type Prepared, type Preview } from "./importTypes";
const labels: Record<string, string> = {title: "标题", authors: "作者", composed_label: "年代", category: "体裁", language: "语言", license: "许可证"};
export default function Import({onRegistered, active}: {onRegistered: (id: number) => void; active: boolean}) {
  const adapters = useLoad<{name: string; description: string}[]>(active ? "import/adapters" : null);
  const {draft: d, setDraft, ready, storageError} = useImportDraft();
  const [busy, setBusy] = useState(false), [error, setError] = useState(""), [progress, setProgress] = useState("");
  const [actor, setActor] = useState(savedActor), [remember, setRemember] = useState(true);
  const [scanInfo, setScanInfo] = useState<{current: string; errors: string[]}>({current: "", errors: []});
  const [groupPage, setGroupPage] = useState(0), [previewPage, setPreviewPage] = useState(0);
  const [resetConfirm, setResetConfirm] = useState(false);
  const stopPreview = useRef(false);
  useEffect(() => {
    const update = () => setActor(savedActor());
    window.addEventListener("midstream-operator", update);
    return () => window.removeEventListener("midstream-operator", update);
  }, []);
  const activeScan = d.scanStatus === "running";
  useEffect(() => {
    if (!ready || !d.scanId || !activeScan) return;
    let active = true, timer: ReturnType<typeof setTimeout>;
    const controller = new AbortController();
    async function poll() {
      try {
        const scan = await api<Scan>(`import/scans/${d.scanId}?offset=${d.scanOffset}`, controller.signal);
        if (!active) return;
        setScanInfo({current: scan.current, errors: scan.errors});
        const drained = scan.next_offset >= scan.found && scan.status !== "running";
        setDraft(prev => {
          const files = [...prev.files, ...scan.items];
          return {...prev, files, scanOffset: scan.next_offset, scanStatus: drained ? scan.status : "running", scanErrors: scan.errors,
            step: drained && files.length ? 2 : prev.step, groups: drained ? groupFiles(files) : prev.groups};
        });
        if (!drained && scan.next_offset === d.scanOffset) timer = setTimeout(poll, 800);
      } catch(e) {
        if (!active) return;
        setError((e as Error).message);
        setDraft(prev => ({...prev, scanStatus: "failed"}));
      }
    }
    poll();
    return () => {active = false; controller.abort(); clearTimeout(timer);};
  }, [ready, d.scanId, d.scanOffset, activeScan]);
  async function scan() {
    setBusy(true); setError(""); setGroupPage(0); setScanInfo({current: "", errors: []});
    try {
      const result = await api<Scan>("import/scans", undefined, {paths: d.selected, recursive: d.recursive});
      setDraft(prev => ({...prev, files: [], groups: [], prepared: [], scanId: result.scan_id, scanOffset: 0, scanStatus: "running", scanErrors: []}));
    } catch(e) {setError((e as Error).message);} finally {setBusy(false);}
  }
  function changeGroup(group: Group) {
    setDraft(prev => ({...prev, prepared: [], groups: prev.groups.map(g => g.id === group.id ? group : g)}));
  }
  function move(path: string, target: string) {
    setDraft(prev => {
      const origin = prev.groups.find(g => g.files.some(f => f.path === path))!;
      const file = origin.files.find(f => f.path === path)!;
      const selected = origin.selected.includes(path);
      let groups = prev.groups.map(g => g.id === origin.id ? {...g, files: g.files.filter(f => f.path !== path), selected: g.selected.filter(p => p !== path)} :
        g.id === target ? {...g, files: [...g.files, file], selected: selected ? [...g.selected, path] : g.selected} : g).filter(g => g.files.length);
      if(target === "new") groups = [...groups, {...origin, id: crypto.randomUUID(), source: origin.adapter ? origin.source : origin.source + "（分组）", files: [file], selected: selected ? [path] : []}];
      return {...prev, groups, prepared: []};
    });
  }
  const selectedGroups = d.groups.filter(g => g.selected.length);
  const invalid = selectedGroups.some(g => !g.source.trim() || g.json !== undefined || (!g.adapter && !["txt", "lines"].includes(g.plan.format) && !g.plan.text_field));
  async function preview() {
    setPreviewPage(0); stopPreview.current = false; setBusy(true); setError("");
    const prepared: Prepared[] = [];
    try {
      const total = selectedGroups.reduce((n, g) => n + Math.ceil(g.selected.length / 2000), 0);
      for (const group of selectedGroups) {
        for(let offset = 0; offset < group.selected.length; offset += 2000) {
          if(stopPreview.current) {setProgress("已停止预览"); return;}
          const paths = group.selected.slice(offset, offset + 2000);
          setProgress(`正在预览 ${prepared.length + 1} / ${total} 批 · ${group.source}`);
          const plan = group.adapter ? null : group.plan;
          const result = await api<Preview>("import/preview", undefined, {source: group.source, paths, plan});
          prepared.push({curation: d.curation || null, groupId: group.id, source: group.source, paths, plan, preview: result,
            approval: {...group.approval, requested_by: group.approval.requested_by || actor}, actor: actor.trim(),
            reason: `目录导入；来源：${group.source}；文件数：${paths.length}。${d.note.trim()}`, token: crypto.randomUUID()});
        }
      }
      if(!stopPreview.current) setDraft(prev => ({...prev, prepared, step: 3}));
    } catch(e) {setError((e as Error).message);} finally {setBusy(false); setProgress("");}
  }
  async function register() {
    setBusy(true); setError("");
    if (remember) localStorage.setItem("midstream.actor", actor.trim());
    try {
      for (const entry of d.prepared) {
        if(entry.result) continue;
        setProgress("正在登记并排队验证：" + entry.source);
        const result = await api<{batch_id: number; job_id: number}>("import/start", undefined, {
          request_token: entry.token, source: entry.source, plan: entry.plan, paths: entry.paths,
          objects: entry.preview.objects, parser_version: entry.preview.parser_version,
          actor: entry.actor, reason: entry.reason, curation: entry.curation || null,
          approval: {...entry.approval, requested_by: entry.approval.requested_by || entry.actor},
        });
        setDraft(prev => ({...prev, prepared: prev.prepared.map(p => p.token === entry.token ? {...p, result} : p)}));
      }
    } catch(e) {setError((e as Error).message + "；已完成的批次保留，重试会继续剩余部分。");}
    finally {setBusy(false); setProgress("");}
  }
  const completed = d.prepared.filter(p => p.result).length;
  const allDone = d.prepared.length > 0 && completed === d.prepared.length;
  const locked = busy || activeScan;
  const hasStarted = completed > 0;
  if(!ready) return <State loading error=""/>;
  return <section className="import-wizard" aria-label="新建入库批次">
    <ol className="import-steps" aria-label="入库步骤">{["选择文件夹", "检查识别结果", "预览并导入"].map((label, i) =>
      <li key={label} className={d.step === i + 1 ? "current" : d.step > i + 1 ? "complete" : ""} aria-current={d.step === i + 1 ? "step" : undefined}><span>{i + 1}</span>{label}</li>)}</ol>
    {(error || storageError) && <p className="notice error" role="alert">{error || storageError}</p>}
    {d.scanErrors?.length > 0 && <div className="notice warning">{d.scanErrors.map((e, i) => <p key={i}>{e}</p>)}</div>}
    {d.step === 1 && <div className="import-stage">
      <h2>选择服务器上的语料文件夹</h2><p>可以选择整个文件夹，也可以勾选其中的文件。扫描后自动识别来源与格式。</p>
      {active && <ServerBrowser path={d.directory} selected={d.selected} disabled={locked} onPath={directory => setDraft(prev => ({...prev, directory}))} onSelect={selected => setDraft(prev => ({...prev, selected}))}/>}
      <label className="check"><input type="checkbox" disabled={locked} checked={d.recursive} onChange={e => setDraft(prev => ({...prev, recursive: e.target.checked}))}/>包含子文件夹</label>
      {activeScan && <div className="scan-progress" role="status"><progress/><strong>已检查 {count(d.scanOffset)} 个文件</strong><p>{scanInfo.current || "正在扫描…"}</p><button onClick={async () => {try {await api(`import/scans/${d.scanId}/cancel`, undefined, {});} catch(e) {setError((e as Error).message);}}}>取消扫描</button></div>}
      {!activeScan && d.scanStatus === "completed" && !d.files.length && <p>所选范围内没有找到文件，请选择其他文件夹。</p>}
      <div className="import-footer"><span>已选 {d.selected.length} 项</span><button className="primary" disabled={locked || !d.selected.length} onClick={scan}>{busy ? "正在启动…" : "扫描并识别 →"}</button></div>
    </div>}
    {d.step === 2 && <div className="import-stage">
      <h2>确认要导入的来源</h2><p>扫描了 {d.files.length} 个文件，分为 {d.groups.length} 组。已选 {selectedGroups.reduce((n, g) => n + g.selected.length, 0)} 个文件。</p>
      {d.scanStatus === "cancelled" && <p className="notice warning">扫描已取消。下方保留取消前已检查的文件；可返回重新扫描。</p>}
      {d.scanStatus === "failed" && <p className="notice warning">扫描未完成，请返回重新扫描。</p>}
      {d.groups.slice(groupPage * 10, (groupPage + 1) * 10).map(group => <ImportGroup key={group.id} group={group} groups={d.groups} adapters={adapters.data || []} actor={actor} disabled={locked} onChange={changeGroup} onMove={move}/>)}
      {d.groups.length > 10 && <div className="pagination"><button disabled={locked || !groupPage} onClick={() => setGroupPage(groupPage - 1)}>上一页来源</button><span>第 {groupPage + 1} / {Math.ceil(d.groups.length / 10)} 页</span><button disabled={locked || (groupPage + 1) * 10 >= d.groups.length} onClick={() => setGroupPage(groupPage + 1)}>下一页来源</button></div>}
      <section className="panel"><label><input type="checkbox" disabled={locked} checked={!!d.curation}
        onChange={e=>setDraft(prev=>({...prev,curation:e.target.checked?defaultCuration():null}))}/>入库时完成 AI 清洗与标注</label>
        {d.curation && <CurationOptions value={d.curation} disabled={locked} ingestion onChange={curation=>setDraft(prev=>({...prev,curation}))}/>}
      </section>
      <div className="edit-grid"><label>操作人<input value={actor} disabled={locked} maxLength={200} onChange={e => setActor(e.target.value)} placeholder="用于记录导入和映射确认"/></label><label>补充备注（选填）<input value={d.note} disabled={locked} maxLength={2000} onChange={e => setDraft(prev => ({...prev, note: e.target.value}))}/></label></div>
      <label className="check"><input type="checkbox" checked={remember} onChange={e => setRemember(e.target.checked)}/>在此浏览器记住操作人</label>
      {invalid && <p className="error">请补全来源名称与正文字段；编辑过的 JSON 需先点击“应用 JSON”。</p>}
      {progress && <p role="status">{progress}</p>}
      <div className="import-footer"><button disabled={locked} onClick={() => setDraft(prev => ({...prev, step: 1}))}>← 调整文件范围</button>{busy && <button onClick={() => {stopPreview.current = true;}}>停止后续预览</button>}<button className="primary" disabled={locked || invalid || !selectedGroups.length || !actor.trim()} onClick={preview}>预览导入结果 →</button></div>
    </div>}
    {d.step === 3 && <div className="import-stage">
      <h2>{allDone ? "已创建批次，正在排队验证" : "检查元数据，确认导入"}</h2><p>共 {d.prepared.length} 个批次。每批最多 2000 个文件，大组自动拆分。下方展示抽样结果，暂存后进行全量验证。</p>
      {d.prepared.slice(previewPage * 5, (previewPage + 1) * 5).map((entry, index) => <details key={entry.token} open={index === 0} className="preview-batch"><summary>{entry.source} · {entry.paths.length} 个文件{entry.result ? ` · 批次 #${entry.result.batch_id} 已登记` : ""}</summary>
        {entry.curation && <p>已启用入库清洗：暂存阶段完成书目、标签、文本质检和篇章识别，再发布。</p>}
        {entry.result && <p>验证任务 #{entry.result.job_id} 已提交。<button onClick={() => onRegistered(entry.result!.batch_id)}>查看批次</button></p>}
        <div className="table-scroll"><table><thead><tr><th>字段</th><th>样本填充率</th></tr></thead><tbody>{entry.preview.fill_rates.map(r => <tr key={r.field}><td>{labels[r.field] || r.field}</td><td>{r.filled} / {r.total} · {r.total ? (100*r.filled/r.total).toFixed(0) : 0}%</td></tr>)}</tbody></table></div>
        <div className="preview-samples">{entry.preview.samples.map((sample, i) => <article key={i}><h3>{show(sample.metadata.title)}</h3><p>{sample.file.split("/").pop()} · {sample.position}</p>
          <dl className="fields">{Object.entries(sample.metadata).filter(([key]) => key !== "field_basis" && key !== "extra").map(([key, value]) => <div key={key}><dt>{labels[key] || key}</dt><dd>{show(value)}</dd></div>)}</dl>
          <details><summary>正文节选</summary><p className="text-preview">{sample.text_preview}</p></details>{sample.error && <p className="error">{sample.error}</p>}</article>)}</div>
        {!entry.preview.samples.length && <p className="error">没有解析出样本，请返回调整映射。</p>}
      </details>)}
      {progress && <p role="status">{progress}</p>}
      {d.prepared.length > 5 && <div className="pagination"><button disabled={!previewPage} onClick={() => setPreviewPage(previewPage - 1)}>上一页预览</button><span>第 {previewPage + 1} / {Math.ceil(d.prepared.length / 5)} 页</span><button disabled={(previewPage + 1) * 5 >= d.prepared.length} onClick={() => setPreviewPage(previewPage + 1)}>下一页预览</button></div>}
      {!allDone && <p>确认后自动登记并暂存验证，完成后在批次详情检查并发布。</p>}
      <div className="import-footer">
        {!hasStarted && <button disabled={busy} onClick={() => setDraft(prev => ({...prev, step: 2, prepared: []}))}>← 调整映射</button>}
        {allDone ? <><button onClick={() => {setDraft(emptyDraft()); setResetConfirm(false);}}>继续导入</button><button className="primary" onClick={() => onRegistered(d.prepared[0].result!.batch_id)}>查看批次进度</button></> :
          <button className="primary" disabled={busy || d.prepared.some(p => !p.preview.samples.some(s => !s.error))} onClick={register}>{busy ? "正在登记…" : hasStarted ? "继续剩余批次" : "确认导入并验证"}</button>}
      </div>
    </div>}
    {!locked && !allDone && <div className="draft-footer"><span>草稿保存在当前浏览器，可返回列表或切换页面。</span><button className="subtle" onClick={() => setResetConfirm(true)}>清空草稿</button>{resetConfirm && <span>清空当前选择和扫描结果？<button onClick={() => {setDraft(emptyDraft()); setResetConfirm(false); setError("");}}>确认清空</button><button onClick={() => setResetConfirm(false)}>取消</button></span>}</div>}
  </section>;
}
