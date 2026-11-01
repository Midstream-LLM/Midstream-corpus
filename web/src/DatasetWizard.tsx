import { useEffect, useMemo, useState } from "react";
import { api, useLoad, State, count, quality } from "./shared";
import { YearFilter } from "./Years";
import { splitNames, type Filter, type Recipe, type Facets, type Plan, type Reservation } from "./datasetTypes";

function Ids({ label, values, onChange }: { label: string; values: number[]; onChange: (ids: number[]) => void }) {
  const [text, setText] = useState(values.join(", "));
  return <label>{label}<input value={text} placeholder="文档 ID，用逗号或空格分隔" onChange={e => {
    setText(e.target.value);
    onChange(e.target.value.split(/[\s,，]+/).filter(Boolean).map(Number));
  }} /></label>;
}
function FilterEditor({ filter, onChange, base }: { filter: Filter; onChange: (f: Filter) => void; base: Facets }) {
  const [settled, setSettled] = useState(filter);
  useEffect(() => { const timer = setTimeout(() => setSettled(filter), 450); return () => clearTimeout(timer); }, [filter]);
  const facets = useLoad<Facets>("datasets/facets?filters=" + encodeURIComponent(JSON.stringify(settled)));
  const years: Record<string, string> = {};
  for (const key of ["year_from", "year_to", "include_unknown_years", "only_unknown_years"]) {
    if (filter[key] !== undefined) years[key] = String(filter[key]);
  }
  if (filter.composed_label) years.era = filter.composed_label;
  return <>
    <div className="facet-grid">{Object.entries({ sources: "来源", categories: "类别", genres: "体裁", quality: "质量" }).map(([field, label]) => {
      const choices = base[field] || [], selected = (filter[field] as string[] | undefined) || (field === "quality" ? ["ok"] : []);
      return <fieldset key={field}><legend>{label}（多选）</legend>{choices.map(item => {
        const current = facets.data?.[field]?.find(x => x.value === item.value);
        return <label className="check" key={item.value ?? "unknown"}><input type="checkbox"
          disabled={item.value === null} checked={item.value !== null && selected.includes(item.value)}
          onChange={e => onChange({ ...filter, [field]: e.target.checked ? [...selected, item.value!] : selected.filter(x => x !== item.value) })} />
          {field === "quality" ? quality[item.value!] : item.value ?? "未记录"}
          <small>{count(current?.documents)} 篇 · {count(current?.chars)} 字</small></label>;
      })}</fieldset>;
    })}</div>
    <State loading={false} error={facets.error} />
    <YearFilter values={years} onChange={values => {
      const next = { ...filter };
      for (const key of ["year_from", "year_to", "composed_label", "include_unknown_years", "only_unknown_years"]) delete next[key];
      for (const key of ["year_from", "year_to"]) if (values[key]) next[key] = Number(values[key]);
      for (const key of ["include_unknown_years", "only_unknown_years"]) if (values[key]) next[key] = values[key] === "true";
      if (values.era) next.composed_label = values.era;
      onChange(next);
    }} />
  </>;
}
export function PlanView({ plan }: { plan: Plan }) {
  return <>
    {plan.warnings.map((warning, i) => <p key={i} className="notice warning" role="alert">{warning}</p>)}
    <div className="metrics">{Object.entries({ 候选篇数: plan.candidates, 舍弃篇数: plan.dropped_documents,
      舍弃字数: plan.dropped_chars, 保留篇数: plan.documents, 保留字数: plan.chars }).map(([k, v]) => <article key={k}><span>{k}</span><strong>{count(v)}</strong></article>)}</div>
    <table><thead><tr><th>划分</th><th>篇数</th><th>字数</th><th>实际比例</th></tr></thead>
      <tbody>{Object.entries(plan.splits).map(([name, value]) => <tr key={name}><td>{splitNames[name]}</td><td>{count(value.documents)}</td>
        <td>{count(value.chars)}</td><td>{(value.ratio * 100).toFixed(2)}%</td></tr>)}</tbody></table>
    {plan.token_stats && <section className="settings-card"><h3>Token：{plan.token_stats.status === "exact" ? "精确" : plan.token_stats.status === "estimated" ? "估算" : "未知"}</h3>
      <p>{plan.token_stats.total === null ? "没有命中缓存，token 数未知。" : count(plan.token_stats.total)}
        {" · 覆盖率 " + ((plan.token_stats.coverage || 0) * 100).toFixed(1) + "%"}</p>
      <p>已缓存：{plan.token_stats.exact === null ? "未知" : count(plan.token_stats.exact)}；未覆盖部分估算：
        {plan.token_stats.estimated === null ? "未知" : count(plan.token_stats.estimated)}</p></section>}
    {Object.entries(plan.distributions).map(([dimension, rows]) => <details key={dimension}>
      <summary>{({ source: "来源", category: "类别", century: "世纪" } as Record<string, string>)[dimension]}分布</summary>
      {dimension === "century" && <p>跨世纪文档可能出现在多个栏位。</p>}
      <table><thead><tr><th>项目</th><th>划分</th><th>篇数</th><th>字数</th></tr></thead><tbody>{rows.map((row, i) => <tr key={i}>
        <td>{row.value === null ? "未记录" : dimension === "century" ? Number(row.value) < 0 ? "公元前" + -Number(row.value) + "世纪" : Number(row.value) + 1 + "世纪" : row.value}</td>
        <td>{splitNames[row.split]}</td><td>{count(row.documents)}</td><td>{count(row.chars)}</td></tr>)}</tbody></table>
    </details>)}
  </>;
}
export default function DatasetWizard({ initial, onJob, onClose }: { initial?: Recipe; onJob: (id: number) => void; onClose: () => void }) {
  const [recipe, setRecipe] = useState<Recipe>(initial || { name: "", seed: "midstream", filters: { quality: ["ok"] }, splits: { train: .9, val: .05, test: .05 } });
  const [step, setStep] = useState(0), [revision, setRevision] = useState(0), [loadedName, setLoadedName] = useState<string | null>(null);
  const [actor, setActor] = useState(""), [reason, setReason] = useState(""), [tokenizer, setTokenizer] = useState("");
  const [plan, setPlan] = useState<{ key: string; value: Plan } | null>(null), [error, setError] = useState(""), [message, setMessage] = useState(""), [busy, setBusy] = useState(false);
  const base = useLoad<Facets>("datasets/facets?filters=" + encodeURIComponent(JSON.stringify({ quality: ["ok", "flagged", "quarantined"] })));
  const saved = useLoad<{ name: string; body: Recipe }[]>("dataset-recipes", revision);
  const body = useMemo(() => ({ recipe: { ...recipe, name: recipe.name || "preview" }, ...(tokenizer ? { tokenizer } : {}) }), [recipe, tokenizer]);
  const key = JSON.stringify(body);
  useEffect(() => {
    setPlan(null); setError("");
    const controller = new AbortController();
    const timer = setTimeout(() => api<Plan>("datasets/preview", controller.signal, body)
      .then(value => { if (!controller.signal.aborted) setPlan({ key, value }); })
      .catch(e => { if (!controller.signal.aborted) setError(e.message); }), 500);
    return () => { clearTimeout(timer); controller.abort(); };
  }, [key]);
  const validIdentity = !!actor.trim() && !!reason.trim();
  const groups = recipe.include || [recipe.filters || {}], excludes = recipe.exclude || [];
  function load(next: Recipe, name: string | null) { setRecipe(next); setLoadedName(name); setRevision(n => n + 1); setStep(0); }
  async function save(remove = false) {
    setBusy(true); setMessage("");
    try {
      if (remove && loadedName) {
        await api("dataset-recipes/" + encodeURIComponent(loadedName), undefined, { actor, reason }, "DELETE");
        setLoadedName(null); setMessage("配方草稿已删除，已发布数据集不受影响。");
      } else {
        await api("dataset-recipes" + (loadedName ? "/" + encodeURIComponent(loadedName) : ""), undefined,
          { recipe, actor, reason }, loadedName ? "PUT" : "POST");
        setLoadedName(recipe.name); setMessage("配方已保存。");
      }
      setRevision(n => n + 1);
    } catch (e) { setMessage((e as Error).message); } finally { setBusy(false); }
  }
  async function publish() {
    setBusy(true);
    try { const job = await api<{ job_id: number }>("datasets", undefined, { recipe, actor, reason }); onJob(job.job_id); }
    catch (e) { setMessage((e as Error).message); } finally { setBusy(false); }
  }
  function reserved(split: string, next: Reservation) { setRecipe({ ...recipe, reserved: { ...recipe.reserved, [split]: next } }); }
  const priority = recipe.selection?.source_priority || [];
  function move(from: number, to: number) { const items = [...priority]; items.splice(to, 0, items.splice(from, 1)[0]); setRecipe({ ...recipe, selection: { ...recipe.selection, source_priority: items } }); }
  return <section className="dataset-wizard">
    <div className="panel-heading"><h2>创建数据集</h2><button onClick={onClose}>关闭向导</button></div>
    <label>载入已保存配方<select value={loadedName || ""} onChange={e => {
      const item = saved.data?.find(x => x.name === e.target.value); if (item) load(item.body, item.name);
    }}><option value="">选择配方</option>{saved.data?.map(x => <option key={x.name}>{x.name}</option>)}</select></label>
    <nav className="actions" aria-label="创建步骤">{["1 选语料", "2 去重和划分", "3 试算", "4 确认"].map((label, index) =>
      <button key={label} className={step === index ? "primary" : ""} onClick={() => setStep(index)}>{label}</button>)}</nav>
    <State loading={!base.data && base.loading} error={base.error || saved.error} />
    {step === 0 && base.data && <div key={revision}>
      <label>正文版本<select value={recipe.variant || ""} onChange={e => {
        const next = { ...recipe }; if (e.target.value) next.variant = "simplified"; else delete next.variant; setRecipe(next);
      }}><option value="">当前主线</option><option value="simplified">简体派生版（缺失或过期时报错）</option></select></label>
      {groups.map((filter, i) => <section className="settings-card" key={i}><h3>包含条件 {i + 1}（组间为或）</h3>
        <FilterEditor base={base.data!} filter={filter} onChange={next => setRecipe({ ...recipe, include: groups.map((x, j) => i === j ? next : x) })} />
        {groups.length > 1 && <button onClick={() => setRecipe({ ...recipe, include: groups.filter((_, j) => i !== j) })}>删除此组</button>}</section>)}
      <button onClick={() => setRecipe({ ...recipe, include: [...groups, { quality: ["ok"] }] })}>添加一组包含条件</button>
      {excludes.map((filter, i) => <section className="settings-card" key={i}><h3>排除条件 {i + 1}</h3>
        <FilterEditor base={base.data!} filter={filter} onChange={next => setRecipe({ ...recipe, exclude: excludes.map((x, j) => i === j ? next : x) })} />
        <button onClick={() => setRecipe({ ...recipe, exclude: excludes.filter((_, j) => i !== j) })}>删除排除条件</button></section>)}
      <button onClick={() => setRecipe({ ...recipe, exclude: [...excludes, { quality: ["ok", "flagged", "quarantined"] }] })}>添加排除条件</button>
      <div className="edit-grid"><Ids label="手动加入文档" values={recipe.documents?.add || []} onChange={add => setRecipe({ ...recipe, documents: { ...recipe.documents, add } })} />
        <Ids label="手动移除文档" values={recipe.documents?.remove || []} onChange={remove => setRecipe({ ...recipe, documents: { ...recipe.documents, remove } })} /></div>
      <p>手动加入覆盖筛选和排除条件；手动移除最后生效。未记录字段保留未知。</p>
    </div>}
    {step === 1 && <>
      <label className="check"><input type="checkbox" checked={recipe.selection?.dedup === "document"} onChange={e =>
        setRecipe({ ...recipe, selection: { ...recipe.selection, dedup: e.target.checked ? "document" : "none" } })} />文档级去重</label>
      <h3>来源优先级（拖动或用按钮排序）</h3>
      <select aria-label="添加优先来源" value="" onChange={e => setRecipe({ ...recipe, selection: { ...recipe.selection, source_priority: [...priority, e.target.value] } })}>
        <option value="">添加来源</option>{base.data?.sources.filter(x => x.value && !priority.includes(x.value)).map(x => <option key={x.value!}>{x.value}</option>)}</select>
      <ol>{priority.map((source, i) => <li key={source} draggable onDragStart={e => e.dataTransfer.setData("text/plain", String(i))}
        onDragOver={e => e.preventDefault()} onDrop={e => { e.preventDefault(); move(Number(e.dataTransfer.getData("text/plain")), i); }}>
        {source} <button disabled={i === 0} onClick={() => move(i, i - 1)}>上移</button> <button disabled={i === priority.length - 1} onClick={() => move(i, i + 1)}>下移</button>
        <button onClick={() => setRecipe({ ...recipe, selection: { ...recipe.selection, source_priority: priority.filter((_, j) => j !== i) } })}>移除</button></li>)}</ol>
      <div className="edit-grid">{Object.entries(splitNames).map(([name, label]) => <label key={name}>{label}比例（%）<input type="number" min="0" max="100" step=".1"
        value={(recipe.splits[name] || 0) * 100} onChange={e => setRecipe({ ...recipe, splits: { ...recipe.splits, [name]: Number(e.target.value) / 100 } })} /></label>)}
        <label>划分依据<select value={recipe.split_by || "chars"} onChange={e => setRecipe({ ...recipe, split_by: e.target.value })}><option value="chars">字数</option><option value="documents">篇数</option></select></label>
        <label>随机种子<input value={recipe.seed} onChange={e => setRecipe({ ...recipe, seed: e.target.value })} /></label></div>
      <div className="actions">{Object.entries({ source: "按来源分层", category: "按类别分层" }).map(([field, label]) => <label className="check" key={field}>
        <input type="checkbox" checked={recipe.stratify?.includes(field) || false} onChange={e => setRecipe({ ...recipe, stratify: e.target.checked ? [...recipe.stratify || [], field] : recipe.stratify?.filter(x => x !== field) })} />{label}</label>)}</div>
      {["val", "test"].map(split => <section className="settings-card" key={split}><h3>{splitNames[split]}上限与保留集</h3>
        <div className="edit-grid">{Object.entries({ max_chars: "最多字数", max_documents: "最多篇数" }).map(([field, label]) => <label key={field}>{label}<input type="number" min="0" placeholder="不限"
          value={recipe.limits?.[split]?.[field as "max_chars"] ?? ""} onChange={e => {
            const limit = { ...recipe.limits?.[split] };
            if (e.target.value === "") delete limit[field as "max_chars"]; else limit[field as "max_chars"] = Number(e.target.value);
            setRecipe({ ...recipe, limits: { ...recipe.limits, [split]: limit } });
          }} /></label>)}
          <Ids label="保留文档 ID" values={recipe.reserved?.[split]?.doc_ids || []} onChange={doc_ids => reserved(split, { ...recipe.reserved?.[split], doc_ids })} /></div>
        {(recipe.reserved?.[split]?.releases || []).map((ref, i) => <div className="actions" key={i}>
          <label>已有发布 ID<input type="number" min="1" value={ref.release_id || ""} onChange={e => reserved(split, { ...recipe.reserved?.[split],
            releases: recipe.reserved![split].releases!.map((x, j) => j === i ? { ...x, release_id: Number(e.target.value) } : x) })} /></label>
          <label>源划分<select value={ref.split} onChange={e => reserved(split, { ...recipe.reserved?.[split], releases: recipe.reserved![split].releases!.map((x, j) => j === i ? { ...x, split: e.target.value } : x) })}>
            {Object.entries(splitNames).map(([key, label]) => <option value={key} key={key}>{label}</option>)}</select></label>
          <button onClick={() => reserved(split, { ...recipe.reserved?.[split], releases: recipe.reserved![split].releases!.filter((_, j) => j !== i) })}>移除保留引用</button></div>)}
        <button onClick={() => reserved(split, { ...recipe.reserved?.[split], releases: [...recipe.reserved?.[split]?.releases || [], { release_id: 0, split }] })}>从已有发布保留划分</button>
      </section>)}
    </>}
    {step === 2 && <>
      <label>分词器路径（可选）<input value={tokenizer} placeholder="后端机器上的 tokenizer.json 路径" onChange={e => setTokenizer(e.target.value)} /></label>
      <State loading={!plan && !error} error={error} />
      {plan?.key === key && <PlanView plan={plan.value} />}
    </>}
    {step === 3 && <>
      <div className="edit-grid"><label>数据集名称<input value={recipe.name} onChange={e => setRecipe({ ...recipe, name: e.target.value })} /></label>
        <label>操作人<input value={actor} onChange={e => setActor(e.target.value)} /></label><label>操作依据<input value={reason} onChange={e => setReason(e.target.value)} /></label></div>
      <State loading={!plan && !error} error={error} />
      {plan?.key === key && <PlanView plan={plan.value} />}
      <div className="actions"><button disabled={!validIdentity || !recipe.name || busy || !plan || plan.key !== key} onClick={() => save()}>保存配方</button>
        {loadedName && <button disabled={!validIdentity || busy} onClick={() => save(true)}>删除配方草稿</button>}
        <button className="primary" disabled={!validIdentity || !recipe.name || busy || !plan || plan.key !== key} onClick={publish}>确认创建数据集</button></div>
      <p>提交后由 worker 固定成员，跳转任务中心查看进度。</p>
    </>}
    {message && <p role="status" className="notice">{message}</p>}
    <div className="actions"><button disabled={step === 0} onClick={() => setStep(n => n - 1)}>上一步</button><button disabled={step === 3} onClick={() => setStep(n => n + 1)}>下一步</button></div>
  </section>;
}
