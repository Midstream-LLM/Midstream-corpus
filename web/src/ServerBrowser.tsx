import { useEffect, useState } from "react";
import { api, State, count } from "./shared";
type Listing = {path: string; parent: string; total: number; next_offset: number | null;
  items: {name: string; path: string; directory: boolean; size_bytes: number}[]};
const favorites = (): string[] => {try {return JSON.parse(localStorage.getItem("midstream.folders") || "[]");} catch {return [];}};
export default function ServerBrowser({path, selected, onPath, onSelect, disabled}: {
  path: string; selected: string[]; onPath: (path: string) => void; onSelect: (paths: string[]) => void; disabled: boolean;
}) {
  const [listing, setListing] = useState<Listing | null>(null), [address, setAddress] = useState(path);
  const [offset, setOffset] = useState(0), [busy, setBusy] = useState(false), [error, setError] = useState("");
  const [bookmarks, setBookmarks] = useState(favorites);
  useEffect(() => {
    const controller = new AbortController();
    setBusy(true); setError("");
    api<Listing>("import/browse", controller.signal, {path: path || null, offset})
      .then(value => {setListing(value); setAddress(value.path);})
      .catch(e => {if (!controller.signal.aborted) setError(e.message);})
      .finally(() => {if (!controller.signal.aborted) setBusy(false);});
    return () => controller.abort();
  }, [path, offset]);
  function navigate(next: string) {setOffset(0); onPath(next);}
  function toggle(name: string) {onSelect(selected.includes(name) ? selected.filter(p => p !== name) : [...selected, name]);}
  function bookmark() {
    if (!listing) return;
    const next = bookmarks.includes(listing.path) ? bookmarks.filter(p => p !== listing.path) : [...bookmarks, listing.path];
    localStorage.setItem("midstream.folders", JSON.stringify(next)); setBookmarks(next);
  }
  return <div className="server-browser">
    <div className="folder-bookmarks"><span>常用目录</span>{bookmarks.map(p => <button key={p} title={p} disabled={disabled} onClick={() => navigate(p)}>{p.split("/").pop() || "/"}</button>)}{!bookmarks.length && <p>打开常用文件夹后，点击“收藏目录”。</p>}</div>
    <form className="folder-toolbar" onSubmit={e => {e.preventDefault(); navigate(address);}}>
      <button type="button" disabled={disabled || busy || !listing || listing.path === listing.parent} onClick={() => navigate(listing!.parent)}>↑ 上级</button>
      <input aria-label="服务器目录" value={address} placeholder="服务器目录" disabled={disabled} onChange={e => setAddress(e.target.value)}/>
      <button disabled={disabled || !address.trim()}>前往</button><button type="button" disabled={!listing || disabled} onClick={bookmark}>{listing && bookmarks.includes(listing.path) ? "取消收藏" : "收藏目录"}</button>
    </form>
    <State loading={busy} error={error}/>
    {!busy && !error && listing && <>
      <div className="folder-location"><span title={listing.path}>{listing.path}</span><button disabled={disabled || (selected.length >= 200 && !selected.includes(listing.path))} onClick={() => toggle(listing.path)}>{selected.includes(listing.path) ? "取消选择此文件夹" : "选择整个文件夹"}</button></div>
      <div className="folder-entries"><table><thead><tr><th>选择</th><th>名称</th><th>大小</th></tr></thead><tbody>{listing.items.map(item => <tr key={item.path}>
        <td><input type="checkbox" aria-label={"选择 " + item.name} disabled={disabled || (selected.length >= 200 && !selected.includes(item.path))} checked={selected.includes(item.path)} onChange={() => toggle(item.path)}/></td>
        <td>{item.directory ? <button className="title" disabled={disabled} onClick={() => navigate(item.path)}>▸ {item.name}/</button> : <span>{item.name}</span>}</td>
        <td>{item.directory ? "文件夹" : count(item.size_bytes) + " B"}</td></tr>)}</tbody></table>
        {!listing.items.length && <div className="empty">此文件夹为空</div>}
      </div>
      <div className="pagination"><span>共 {count(listing.total)} 项</span><div><button disabled={disabled || offset === 0} onClick={() => setOffset(Math.max(0, offset - 200))}>上一页</button><button disabled={disabled || listing.next_offset === null} onClick={() => setOffset(listing.next_offset!)}>下一页</button></div></div>
    </>}
    {selected.length > 0 && <details className="selected-paths"><summary>已选择 {selected.length} 个文件或文件夹</summary>{selected.map(p => <div key={p}><span>{p}</span><button disabled={disabled} onClick={() => toggle(p)}>移除</button></div>)}</details>}
  </div>;
}
