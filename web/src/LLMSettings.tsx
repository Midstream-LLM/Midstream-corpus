import { useEffect, useState } from "react";
import { api, useLoad, State } from "./shared";
import QwenQuick, { type QwenSelection } from "./QwenQuick";
type Endpoint = {
  protocol: string;
  base_url: string;
  credential: string;
  timeout: number;
  concurrency: number;
  max_retries: number;
  requests_per_second: number;
};
type Profile = {
  endpoint: string;
  model: string;
  max_tokens: number;
  parameters: Record<string, unknown>;
};
type Config = {
  credentials: Record<string, string>;
  endpoints: Record<string, Endpoint>;
  profiles: Record<string, Profile>;
  services: Record<string, string>;
};
type Settings = {
  config: Config;
  credentials_status: Record<
    string,
    { configured: boolean; source: string; local_saved: boolean }
  >;
  revision: string;
  configured: boolean;
  protocols: string[];
};
const protocolNames: Record<string, string> = {
  chat_completions: "Chat Completions",
  responses: "Responses",
  anthropic_messages: "Anthropic Messages",
  fim_completions: "Beta FIM",
};
const serviceNames: Record<string, string> = {
  generation: "通用生成",
  quality_check: "语料质检",
  import_detection: "入库格式识别",
};
type ModelInfo = {
  id: string;
  name: string;
  context_window?: number;
  max_output_tokens?: number;
  effort_levels?: string[];
};
type DeepSeekPreset = {
  base_urls: Record<string, string>;
  models: ModelInfo[];
  checked_on: string;
  source: string;
};
export default function LLMSettings() {
  const loaded = useLoad<Settings>("llm/settings");
  const deepseek = useLoad<DeepSeekPreset>("llm/deepseek");
  const [dsProtocol, setDsProtocol] = useState("chat_completions"),
    [dsModel, setDsModel] = useState("deepseek-flash"),
    [dsCredential, setDsCredential] = useState(""),
    [dsKey, setDsKey] = useState("");
  const [discovered, setDiscovered] = useState<ModelInfo[] | null>(null),
    [discoverBusy, setDiscoverBusy] = useState(false);
  const modelOptions = discovered ?? deepseek.data?.models ?? [];
  async function discoverModels() {
    setDiscoverBusy(true);
    setError("");
    try {
      const credential =
        dsCredential || (data?.config.credentials.deepseek ? "deepseek" : "");
      const response = await api<{ models: ModelInfo[] }>(
        "llm/deepseek/models",
        undefined,
        { credential, api_key: dsKey || keys[credential] || undefined },
      );
      setDiscovered(response.models);
      if (!response.models.some((m) => m.id === dsModel))
        setDsModel(
          dsProtocol === "fim_completions"
            ? response.models.some((m) => m.id === "deepseek-flash")
              ? "deepseek-flash"
              : ""
            : response.models[0]?.id || "",
        );
      setMessage(
        `已从 DeepSeek 获取 ${response.models.length} 个模型；未发送生成请求。`,
      );
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setDiscoverBusy(false);
    }
  }
  function addDeepSeek() {
    if (!data || !deepseek.data) return;
    const config = structuredClone(data.config);
    function free(base: string, section: Record<string, unknown>) {
      let name = base,
        n = 1;
      while (name in section) name = `${base}_${++n}`;
      return name;
    }
    let credential = dsCredential;
    if (!credential) {
      credential =
        config.credentials.deepseek === "DEEPSEEK_API_KEY"
          ? "deepseek"
          : free("deepseek", config.credentials);
      config.credentials[credential] = "DEEPSEEK_API_KEY";
    }
    const endpoint = free(`deepseek_${dsProtocol}`, config.endpoints);
    const name = free("deepseek", config.profiles);
    config.endpoints[endpoint] = {
      protocol: dsProtocol,
      base_url: deepseek.data.base_urls[dsProtocol],
      credential,
      timeout: 120,
      concurrency: 4,
      max_retries: 2,
      requests_per_second: 0,
    };
    const parameters =
      dsProtocol === "responses"
        ? { reasoning: { effort: "none" } }
        : dsProtocol === "fim_completions"
          ? {}
          : { thinking: { type: "disabled" } };
    const info = modelOptions.find((m) => m.id === dsModel);
    config.profiles[name] = {
      endpoint,
      model: dsModel,
      max_tokens: Math.min(4096, info?.max_output_tokens || 4096),
      parameters,
    };
    if (
      !Object.keys(config.services).length &&
      Object.keys(config.profiles).length === 1
    ) {
      for (const service of Object.keys(serviceNames))
        config.services[service] = name;
    }
    setData({ ...data, config });
    setParams((p) => ({ ...p, [name]: JSON.stringify(parameters, null, 2) }));
    if (dsKey) setKeys((p) => ({ ...p, [credential]: dsKey }));
    setDsKey("");
    setDirty(true);
    setMessage(`已填入 ${name}，检查后保存配置即可使用。`);
    setError("");
  }
  function addQwen(selection: QwenSelection) {
    if (!data) return;
    const config = structuredClone(data.config);
    function free(base: string, section: Record<string, unknown>) {
      let name = base,
        n = 1;
      while (name in section) name = `${base}_${++n}`;
      return name;
    }
    const env = `DASHSCOPE_API_KEY_${selection.region.toUpperCase()}`;
    let credential = selection.credential;
    if (!credential) {
      const base = `qwen_${selection.region}`;
      credential =
        config.credentials[base] === env
          ? base
          : free(base, config.credentials);
      config.credentials[credential] = env;
    }
    const endpoint = free(`qwen_${selection.region}`, config.endpoints),
      name = free("qwen", config.profiles);
    config.endpoints[endpoint] = {
      protocol: "chat_completions",
      base_url: selection.base_url,
      credential,
      timeout: 120,
      concurrency: 4,
      max_retries: 2,
      requests_per_second: 0,
    };
    config.profiles[name] = {
      endpoint,
      model: selection.model,
      max_tokens: selection.max_tokens,
      parameters: {},
    };
    if (
      !Object.keys(config.services).length &&
      Object.keys(config.profiles).length === 1
    )
      for (const service of Object.keys(serviceNames))
        config.services[service] = name;
    setData({ ...data, config });
    setParams((p) => ({ ...p, [name]: "{}" }));
    if (selection.api_key)
      setKeys((p) => ({ ...p, [credential]: selection.api_key }));
    setDirty(true);
    setError("");
    setMessage(`已填入 ${name}，检查区域与凭据后保存即可使用。`);
  }
  const [data, setData] = useState<Settings | null>(null),
    [keys, setKeys] = useState<Record<string, string>>({}),
    [clearKeys, setClearKeys] = useState<string[]>([]),
    [params, setParams] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState(false),
    [message, setMessage] = useState(""),
    [error, setError] = useState(""),
    [dirty, setDirty] = useState(false);
  function apply(next: Settings) {
    setData(next);
    setKeys({});
    setClearKeys([]);
    setParams(
      Object.fromEntries(
        Object.entries(next.config.profiles).map(([k, v]) => [
          k,
          JSON.stringify(v.parameters, null, 2),
        ]),
      ),
    );
    setDirty(false);
  }
  useEffect(() => {
    if (loaded.data) apply(loaded.data);
  }, [loaded.data]);
  function edit(fn: (c: Config) => void) {
    if (!data) return;
    const config = structuredClone(data.config);
    fn(config);
    setData({ ...data, config });
    setDirty(true);
    setMessage("");
  }
  function add(section: "credentials" | "endpoints" | "profiles") {
    if (!data) return;
    let name =
      section === "profiles" && !data.config.profiles.default
        ? "default"
        : `${section.slice(0, -1)}_1`;
    let n = 1;
    while (name in data.config[section])
      name = `${section.slice(0, -1)}_${++n}`;
    edit((c) => {
      if (section === "credentials") c.credentials[name] = "MIDSTREAM_API_KEY";
      if (section === "endpoints")
        c.endpoints[name] = {
          protocol: "chat_completions",
          base_url: "https://api.deepseek.com",
          credential: Object.keys(c.credentials)[0] || "",
          timeout: 120,
          concurrency: 4,
          max_retries: 2,
          requests_per_second: 0,
        };
      if (section === "profiles")
        c.profiles[name] = {
          endpoint: Object.keys(c.endpoints)[0] || "",
          model: "",
          max_tokens: 4096,
          parameters: {},
        };
    });
    if (section === "profiles") setParams((p) => ({ ...p, [name]: "{}" }));
  }
  async function save() {
    if (!data) return;
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const config = structuredClone(data.config);
      for (const [name, profile] of Object.entries(config.profiles)) {
        profile.parameters = JSON.parse(params[name] || "{}");
        if (
          !profile.parameters ||
          Array.isArray(profile.parameters) ||
          typeof profile.parameters !== "object"
        )
          throw new Error("模型附加参数必须为 JSON 对象");
      }
      const result = await api<Settings>("llm/settings", undefined, {
        config,
        keys: Object.fromEntries(
          Object.entries(keys).filter(
            ([k, v]) => v.length > 0 && k in config.credentials,
          ),
        ),
        clear_keys: clearKeys.filter((k) => k in config.credentials),
        revision: data.revision,
      });
      apply(result);
      setMessage("配置已保存，后续 LLM 请求立即使用新配置。");
    } catch (e) {
      setError((e as Error).message);
      setKeys({});
    } finally {
      setBusy(false);
    }
  }
  const c = data?.config;
  return (
    <>
      <section className="heading">
        <div>
          <div className="eyebrow">接入点 · 模型 · 用途</div>
          <h1>LLM 配置</h1>
          <p>
            Web 与 CLI
            共用后端配置；按用途选择模型，统一管理协议、凭据和请求限制。
          </p>
        </div>
        <button
          disabled={busy}
          onClick={() => {
            if (
              !dirty ||
              window.confirm("重新加载会放弃未保存的配置，是否继续？")
            ) {
              loaded.refresh();
              setError("");
              setMessage("");
            }
          }}
        >
          重新加载
        </button>
      </section>
      <State loading={loaded.loading} error={loaded.error || error} />
      {message && (
        <p className="notice" role="status">
          {message}
        </p>
      )}
      {data && c && (
        <fieldset className="llm-settings" disabled={busy}>
          <datalist id="deepseek-model-options">
            {modelOptions.map((m) => (
              <option key={m.id} value={m.id}>
                {m.name}
              </option>
            ))}
          </datalist>
          <section className="catalog">
            <div className="panel-heading">
              <h2>DeepSeek 快速配置</h2>
            </div>
            <div className="metadata">
              <p>
                自动填写协议地址、凭据引用、模型和参数；添加为新配置，已有配置保留。
              </p>
              <State loading={deepseek.loading} error={deepseek.error} />
              <div className="edit-grid">
                <label>
                  DeepSeek 协议
                  <select
                    value={dsProtocol}
                    onChange={(e) => {
                      setDsProtocol(e.target.value);
                      if (e.target.value === "fim_completions")
                        setDsModel("deepseek-flash");
                    }}
                  >
                    {data.protocols.map((p) => (
                      <option key={p} value={p}>
                        {protocolNames[p]}
                      </option>
                    ))}
                  </select>
                </label>
                <label>
                  DeepSeek 模型
                  <select
                    value={dsModel}
                    onChange={(e) => setDsModel(e.target.value)}
                  >
                    {!modelOptions.some((m) => m.id === dsModel) && (
                      <option value={dsModel}>{dsModel}</option>
                    )}
                    {modelOptions
                      .filter(
                        (m) =>
                          dsProtocol !== "fim_completions" ||
                          m.id === "deepseek-flash",
                      )
                      .map((m) => (
                        <option key={m.id} value={m.id}>
                          {m.name} · {m.id}
                        </option>
                      ))}
                  </select>
                </label>
                <label>
                  DeepSeek 凭据
                  <select
                    value={dsCredential}
                    onChange={(e) => setDsCredential(e.target.value)}
                  >
                    <option value="">使用 / 新建 DEEPSEEK_API_KEY 引用</option>
                    {Object.keys(c.credentials).map((k) => (
                      <option key={k}>{k}</option>
                    ))}
                  </select>
                </label>
                <label>
                  DeepSeek API key（可选）
                  <input
                    type="password"
                    autoComplete="new-password"
                    value={dsKey}
                    onChange={(e) => setDsKey(e.target.value)}
                    placeholder="已有凭据可留空；输入后可刷新模型列表"
                  />
                </label>
              </div>
              <p>
                地址：{deepseek.data?.base_urls[dsProtocol]}
                。默认关闭思考，输出上限 4096 tokens；均可在下方修改。FIM 仅提供
                Flash 候选。
              </p>
              {modelOptions
                .filter((m) => m.id === dsModel)
                .map((m) => (
                  <p key={m.id}>
                    {m.context_window
                      ? `上下文容量 ${m.context_window.toLocaleString()} tokens。`
                      : ""}
                    {m.max_output_tokens
                      ? `服务端输出上限 ${m.max_output_tokens.toLocaleString()} tokens（FIM 另限 4096）。`
                      : ""}
                    {m.effort_levels?.length
                      ? `思考等级：${m.effort_levels.join(" / ")}。`
                      : ""}
                  </p>
                ))}
              <p>
                {discovered
                  ? "模型信息来自本次官方 /models 响应。"
                  : `离线候选核对于 ${deepseek.data?.checked_on || "…"}；账户可用模型以刷新结果为准。`}
              </p>
              <div className="actions">
                <button disabled={discoverBusy} onClick={discoverModels}>
                  {discoverBusy ? "获取中…" : "从 DeepSeek 刷新模型"}
                </button>
                <button
                  disabled={!deepseek.data || !dsModel}
                  onClick={addDeepSeek}
                >
                  填入 DeepSeek 配置
                </button>
              </div>
            </div>
          </section>
          <QwenQuick
            credentials={c.credentials}
            pendingKeys={keys}
            onApply={addQwen}
          />
          {!data.configured && (
            <p className="notice">
              尚未配置 LLM。先添加凭据、接入点和模型，再指定用途默认值。
            </p>
          )}
          <section className="catalog">
            <div className="panel-heading">
              <h2>凭据</h2>
              <button onClick={() => add("credentials")}>添加凭据</button>
            </div>
            <div className="metadata">
              <p>
                可引用后端环境变量，或输入 API key
                保存到后端本地配置文件（仅当前系统用户可读写）。环境变量优先；密钥保存后不回显。
              </p>
              {Object.entries(c.credentials).map(([name, env]) => (
                <article className="settings-card" key={name}>
                  <div className="panel-heading">
                    <h3>{name}</h3>
                    <button
                      onClick={() =>
                        edit((c) => {
                          delete c.credentials[name];
                        })
                      }
                    >
                      移除此凭据
                    </button>
                  </div>
                  <div className="edit-grid">
                    <label>
                      环境变量 · {name}
                      <input
                        value={env}
                        onChange={(e) =>
                          edit((c) => {
                            c.credentials[name] = e.target.value;
                          })
                        }
                      />
                    </label>
                    <label>
                      API key · {name}
                      <input
                        type="password"
                        autoComplete="new-password"
                        value={keys[name] || ""}
                        placeholder="留空保留已有密钥"
                        onChange={(e) => {
                          setKeys({ ...keys, [name]: e.target.value });
                          setDirty(true);
                          setMessage("");
                        }}
                      />
                    </label>
                  </div>
                  <p>
                    当前状态：
                    {data.credentials_status[name]?.configured
                      ? "已配置"
                      : "未配置"}{" "}
                    ·{" "}
                    {
                      (
                        {
                          environment: "使用环境变量",
                          local: "使用本地密钥",
                          missing: "尚无密钥",
                        } as Record<string, string>
                      )[data.credentials_status[name]?.source || "missing"]
                    }
                  </p>
                  {data.credentials_status[name]?.local_saved && (
                    <label className="check">
                      <input
                        type="checkbox"
                        checked={clearKeys.includes(name)}
                        onChange={(e) => {
                          setClearKeys(
                            e.target.checked
                              ? [...clearKeys, name]
                              : clearKeys.filter((k) => k !== name),
                          );
                          setDirty(true);
                        }}
                      />
                      保存时清除本地密钥
                    </label>
                  )}
                </article>
              ))}
            </div>
          </section>
          <section className="catalog">
            <div className="panel-heading">
              <h2>接入点</h2>
              <button onClick={() => add("endpoints")}>添加接入点</button>
            </div>
            <div className="metadata">
              {Object.entries(c.endpoints).map(([name, endpoint]) => (
                <article className="settings-card" key={name}>
                  <div className="panel-heading">
                    <h3>{name}</h3>
                    <button
                      onClick={() =>
                        edit((c) => {
                          delete c.endpoints[name];
                        })
                      }
                    >
                      移除接入点
                    </button>
                  </div>
                  <div className="edit-grid">
                    <label>
                      协议 · {name}
                      <select
                        value={endpoint.protocol}
                        onChange={(e) =>
                          edit((c) => {
                            c.endpoints[name].protocol = e.target.value;
                          })
                        }
                      >
                        {data.protocols.map((p) => (
                          <option key={p} value={p}>
                            {protocolNames[p] || p}
                          </option>
                        ))}
                      </select>
                    </label>
                    <label>
                      Base URL · {name}
                      <input
                        value={endpoint.base_url}
                        onChange={(e) =>
                          edit((c) => {
                            c.endpoints[name].base_url = e.target.value;
                          })
                        }
                      />
                    </label>
                    <label>
                      凭据引用 · {name}
                      <select
                        value={endpoint.credential}
                        onChange={(e) =>
                          edit((c) => {
                            c.endpoints[name].credential = e.target.value;
                          })
                        }
                      >
                        <option value="">请选择</option>
                        {Object.keys(c.credentials).map((k) => (
                          <option key={k}>{k}</option>
                        ))}
                      </select>
                    </label>
                    {(
                      [
                        ["timeout", "超时（秒）", 0.001],
                        ["concurrency", "并发请求数", 1],
                        ["max_retries", "最大重试次数", 0],
                        ["requests_per_second", "每秒请求数（0 不限）", 0],
                      ] as const
                    ).map(([k, label, min]) => (
                      <label key={k}>
                        {label} · {name}
                        <input
                          type="number"
                          min={min}
                          step={
                            k === "timeout" || k === "requests_per_second"
                              ? "any"
                              : 1
                          }
                          value={endpoint[k]}
                          onChange={(e) =>
                            edit((c) => {
                              c.endpoints[name][k] = Number(e.target.value);
                            })
                          }
                        />
                      </label>
                    ))}
                  </div>
                </article>
              ))}
            </div>
          </section>
          <section className="catalog">
            <div className="panel-heading">
              <h2>模型 Profiles</h2>
              <button onClick={() => add("profiles")}>添加模型</button>
            </div>
            <div className="metadata">
              {Object.entries(c.profiles).map(([name, profile]) => (
                <article className="settings-card" key={name}>
                  <div className="panel-heading">
                    <h3>{name}</h3>
                    <button
                      onClick={() =>
                        edit((c) => {
                          delete c.profiles[name];
                        })
                      }
                    >
                      移除模型
                    </button>
                  </div>
                  <div className="edit-grid">
                    <label>
                      接入点引用 · {name}
                      <select
                        value={profile.endpoint}
                        onChange={(e) =>
                          edit((c) => {
                            c.profiles[name].endpoint = e.target.value;
                          })
                        }
                      >
                        <option value="">请选择</option>
                        {Object.keys(c.endpoints).map((k) => (
                          <option key={k}>{k}</option>
                        ))}
                      </select>
                    </label>
                    <label>
                      模型名称 · {name}
                      <input
                        value={profile.model}
                        list="deepseek-model-options"
                        placeholder="填写服务商提供的模型 ID"
                        onChange={(e) =>
                          edit((c) => {
                            c.profiles[name].model = e.target.value;
                          })
                        }
                      />
                    </label>
                    <label>
                      最大输出 tokens · {name}
                      <input
                        type="number"
                        min="1"
                        step="1"
                        value={profile.max_tokens}
                        onChange={(e) =>
                          edit((c) => {
                            c.profiles[name].max_tokens = Number(
                              e.target.value,
                            );
                          })
                        }
                      />
                    </label>
                    <label>
                      附加参数 JSON · {name}
                      <textarea
                        rows={5}
                        value={params[name] || "{}"}
                        onChange={(e) => {
                          setParams({ ...params, [name]: e.target.value });
                          setDirty(true);
                          setMessage("");
                        }}
                      />
                    </label>
                  </div>
                </article>
              ))}
            </div>
          </section>
          <section className="catalog">
            <div className="panel-heading">
              <h2>用途默认值</h2>
            </div>
            <div className="metadata edit-grid">
              {Object.entries(serviceNames).map(([name, label]) => (
                <label key={name}>
                  {label}
                  <select
                    value={c.services[name] || ""}
                    onChange={(e) =>
                      edit((c) => {
                        if (e.target.value) c.services[name] = e.target.value;
                        else delete c.services[name];
                      })
                    }
                  >
                    <option value="">跟随 default profile</option>
                    {Object.keys(c.profiles).map((k) => (
                      <option key={k}>{k}</option>
                    ))}
                  </select>
                </label>
              ))}
            </div>
          </section>
          <div className="actions settings-save">
            <span>{dirty ? "有未保存的修改" : "配置已同步"}</span>
            <button
              className="primary"
              disabled={!dirty || busy}
              onClick={save}
            >
              {busy ? "保存中…" : "保存 LLM 配置"}
            </button>
          </div>
        </fieldset>
      )}
    </>
  );
}
