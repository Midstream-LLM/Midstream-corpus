import { useState } from "react";
import { api, useLoad, State } from "./shared";
type Model = {
  id: string;
  name: string;
  context_window?: number;
  max_output_tokens?: number;
};
export type QwenSelection = {
  region: string;
  base_url: string;
  model: string;
  max_tokens: number;
  credential: string;
  api_key: string;
};
export default function QwenQuick({
  credentials,
  pendingKeys,
  onApply,
}: {
  credentials: Record<string, string>;
  pendingKeys: Record<string, string>;
  onApply: (selection: QwenSelection) => void;
}) {
  const preset = useLoad<{
    regions: Record<string, { name: string; code: string }>;
    models: Model[];
    checked_on: string;
  }>("llm/qwen");
  const [region, setRegion] = useState("beijing"),
    [workspace, setWorkspace] = useState(""),
    [model, setModel] = useState("qwen3.8-max"),
    [credential, setCredential] = useState(""),
    [key, setKey] = useState("");
  const [found, setFound] = useState<Model[] | null>(null),
    [busy, setBusy] = useState(false),
    [error, setError] = useState(""),
    [notice, setNotice] = useState("");
  const options = found ?? preset.data?.models ?? [];
  const validWorkspace = /^[A-Za-z0-9][A-Za-z0-9-]{0,62}$/.test(workspace);
  const base_url =
    preset.data && validWorkspace
      ? `https://${workspace}.${preset.data.regions[region].code}.maas.aliyuncs.com/compatible-mode/v1`
      : "";
  function reset() {
    setFound(null);
    setNotice("");
    setError("");
  }
  async function refresh() {
    setBusy(true);
    setError("");
    setNotice("");
    try {
      const ref = credential || `qwen_${region}`;
      const result = await api<{
        models: Model[];
        total: number;
        truncated: boolean;
      }>("llm/qwen/models", undefined, {
        region,
        workspace,
        credential: ref,
        api_key: key || pendingKeys[ref] || undefined,
      });
      setFound(result.models);
      if (!result.models.some((m) => m.id === model))
        setModel(result.models[0]?.id || "");
      setNotice(
        `已获取 ${result.models.length} 个 Qwen 文本模型${result.truncated ? "（结果已截断，可手工填写未列出的模型）" : ""}；可用性仍以实际调用权限为准。`,
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <section className="catalog">
      <div className="panel-heading">
        <h2>Qwen 快速配置 · 阿里云百炼</h2>
      </div>
      <fieldset
        className="metadata"
        disabled={busy}
        style={{ border: 0, margin: 0, minWidth: 0 }}
      >
        <p>
          使用 OpenAI 兼容 Chat Completions。选择 key
          所在区域及业务空间，自动填写地址和模型；北京、新加坡、弗吉尼亚的 key
          不能混用。
        </p>
        <State loading={preset.loading} error={preset.error || error} />
        <div className="edit-grid">
          <label>
            Qwen 服务区域
            <select
              value={region}
              onChange={(e) => {
                setRegion(e.target.value);
                setCredential("");
                setKey("");
                reset();
              }}
            >
              {Object.entries(preset.data?.regions || {}).map(([id, r]) => (
                <option key={id} value={id}>
                  {r.name}
                </option>
              ))}
            </select>
          </label>
          <label>
            Qwen 业务空间 ID
            <input
              value={workspace}
              onChange={(e) => {
                setWorkspace(e.target.value.trim());
                reset();
              }}
              placeholder="百炼控制台中的 Workspace ID"
            />
          </label>
          <label>
            Qwen 模型
            <input
              list="qwen-model-options"
              value={model}
              onChange={(e) => setModel(e.target.value)}
              placeholder="可选择候选或手工输入模型 ID"
            />
            <datalist id="qwen-model-options">
              {options.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.name}
                </option>
              ))}
            </datalist>
          </label>
          <label>
            Qwen 凭据
            <select
              value={credential}
              onChange={(e) => setCredential(e.target.value)}
            >
              <option value="">
                使用 / 新建此区域的凭据引用
              </option>
              {Object.keys(credentials).map((k) => (
                <option key={k}>{k}</option>
              ))}
            </select>
          </label>
          <label>
            Qwen API key（可选）
            <input
              type="password"
              autoComplete="new-password"
              value={key}
              onChange={(e) => setKey(e.target.value)}
              placeholder="可复用已保存的同区域凭据"
            />
          </label>
        </div>
        <p>
          地址：{base_url || "填写业务空间 ID 后自动生成"}。默认输出预算
          4096，附加参数留空，避免给不同模型套用不兼容的思考参数。
        </p>
        {options
          .filter((m) => m.id === model)
          .map((m) => (
            <p key={m.id}>
              {m.context_window
                ? `上下文 ${m.context_window.toLocaleString()} tokens。`
                : ""}
              {m.max_output_tokens
                ? `服务端输出上限 ${m.max_output_tokens.toLocaleString()} tokens。`
                : ""}
            </p>
          ))}
        <p>
          {found
            ? "模型信息来自本次百炼模型目录响应。"
            : `离线候选核对日期 ${preset.data?.checked_on || "…"}，实际可用模型及权限按区域和账户而异。`}
        </p>
        {notice && <p role="status">{notice}</p>}
        <div className="actions">
          <button disabled={!validWorkspace || !preset.data} onClick={refresh}>
            {busy ? "获取中…" : "从百炼刷新模型"}
          </button>
          <button
            disabled={!base_url || !model.trim()}
            onClick={() => {
              onApply({
                region,
                base_url,
                model: model.trim(),
                max_tokens: Math.min(
                  4096,
                  options.find((m) => m.id === model)?.max_output_tokens ||
                    4096,
                ),
                credential,
                api_key: key,
              });
              setKey("");
            }}
          >
            填入 Qwen 配置
          </button>
        </div>
      </fieldset>
    </section>
  );
}
