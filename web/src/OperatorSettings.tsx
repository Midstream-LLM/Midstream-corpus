import { useState } from "react";
import { savedActor } from "./importDraft";
export default function OperatorSettings() {
  const [actor, setActor] = useState(savedActor), [saved, setSaved] = useState(false);
  return <section className="operator-settings catalog metadata"><h2>操作人</h2><p>用于记录入库、审核和发布操作，保存在当前浏览器。</p>
    <div className="heading-actions"><label className="import-label">姓名或标识<input value={actor} maxLength={200} onChange={e => {setActor(e.target.value); setSaved(false);}}/></label>
    <button disabled={!actor.trim()} onClick={() => {localStorage.setItem("midstream.actor", actor.trim()); window.dispatchEvent(new Event("midstream-operator")); setSaved(true);}}>保存</button>{saved && <span role="status">已保存</span>}</div></section>;
}
