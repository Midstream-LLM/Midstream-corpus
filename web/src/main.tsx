import React, { useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import Corpus from "./Corpus";
import Stats from "./Stats";
import Batches from "./Batches";
import Jobs from "./Jobs";
import Datasets from "./Datasets";
import Duplicates from "./Duplicates";
import BackupSettings from "./BackupSettings";
import LLMSettings from "./LLMSettings";
import "./style.css";
function App() {
  const [route, setRoute] = useState(location.hash.slice(1) || "corpus");
  const page = route.split("/")[0];
  const jobId = route.startsWith("jobs/") ? Number(route.split("/")[1]) : null;
  const navigate = (next: string) => { location.hash = next; };
  useEffect(() => {
    const changed = () => setRoute(location.hash.slice(1) || "corpus");
    window.addEventListener("hashchange", changed);
    return () => window.removeEventListener("hashchange", changed);
  }, []);
  const names: Record<string, string> = {
    stats: "统计概览",
    corpus: "语料浏览",
    review: "人工审核",
    batches: "批次管理",
    jobs: "任务中心",
    datasets: "数据集",
    duplicates: "重复",
    llm: "设置",
  };
  return (
    <div className="app">
      <aside className="sidebar">
        <div className="brand">
          <span className="seal">中</span>
          <div>
            中流语料库<small>MIDSTREAM CORPUS</small>
          </div>
        </div>
        <div className="nav-title">工作空间</div>
        <nav aria-label="主要导航">
          {Object.entries(names).map(([k, v]) => (
            <button
              className={"nav " + (page === k ? "active" : "")}
              key={k}
              onClick={() => navigate(k)}
              aria-current={page === k ? "page" : undefined}
            >
              <span>{k === "stats" ? "◫" : k === "batches" ? "▦" : "▤"}</span>
              <span>{v}</span>
              {page === k && <span className="dot" />}
            </button>
          ))}
        </nav>
        <div className="side-note">
          文本有出处
          <br />
          版本可追溯
        </div>
        <footer>
          中流 · 击水
          <br />
          <span>语料管理与存储</span>
        </footer>
      </aside>
      <main>
        <header>
          <div className="breadcrumb">
            工作空间 <span>/</span> {names[page]}
          </div>
          <span className="label">MIDSTREAM CORPUS</span>
        </header>
        <div className="mobile-nav">
          {Object.entries(names).map(([k, v]) => (
            <button
              key={k}
              onClick={() => navigate(k)}
              className={page === k ? "chosen" : ""}
            >
              {v}
            </button>
          ))}
        </div>
        {page === "llm" ? (
          <><LLMSettings /><BackupSettings onJob={id=>navigate("jobs/"+id)}/></>
        ) : page === "stats" ? (
          <Stats />
        ) : page === "duplicates" ? (
          <Duplicates onJob={id => navigate("jobs/" + id)} />
        ) : page === "datasets" ? (
          <Datasets onJob={id => navigate("jobs/" + id)} />
        ) : page === "jobs" ? (
          <Jobs id={jobId} onSelect={id => navigate(`jobs/${id}`)} />
        ) : page === "batches" ? (
          <Batches onJob={id => navigate(`jobs/${id}`)} />
        ) : (
          <Corpus key={page} review={page === "review"} />
        )}
        <div className="bottom-note">
          保留原文形态 · 元数据修改生成新版本并留下依据
        </div>
      </main>
    </div>
  );
}
createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
