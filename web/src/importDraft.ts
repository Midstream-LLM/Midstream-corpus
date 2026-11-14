import { useEffect, useState } from "react";
import { emptyDraft, type Draft } from "./importTypes";

function database(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open("midstream-import", 1);
    request.onupgradeneeded = () => request.result.createObjectStore("drafts");
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error);
  });
}
export function useImportDraft() {
  const [draft, setDraft] = useState<Draft>(emptyDraft), [ready, setReady] = useState(false), [storageError, setStorageError] = useState("");
  useEffect(() => {
    let active = true;
    database().then(db => new Promise<Draft | undefined>((resolve, reject) => {
      const request = db.transaction("drafts").objectStore("drafts").get("current");
      request.onsuccess = () => resolve(request.result);
      request.onerror = () => reject(request.error);
      request.transaction!.oncomplete = () => db.close();
    })).then(saved => {if (active && saved) setDraft(saved);})
      .catch(() => {if (active) setStorageError("浏览器无法恢复草稿，请保持此页面打开。");})
      .finally(() => {if (active) setReady(true);});
    return () => {active = false;};
  }, []);
  useEffect(() => {
    if (!ready) return;
    // Coalesce progress updates; IndexedDB retains scans and previews across page navigation.
    const timer = setTimeout(() => {
      database().then(db => {
        const tx = db.transaction("drafts", "readwrite");
        tx.objectStore("drafts").put(draft, "current");
        tx.oncomplete = () => db.close();
        tx.onerror = () => {db.close(); setStorageError("草稿保存失败，请保持此页面打开。");};
      }).catch(() => setStorageError("草稿保存失败，请保持此页面打开。"));
    }, 100);
    return () => clearTimeout(timer);
  }, [draft, ready]);
  return {draft, setDraft, ready, storageError};
}
export function savedActor() {return localStorage.getItem("midstream.actor") || "";}
