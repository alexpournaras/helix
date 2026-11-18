import { useCallback, useEffect, useLayoutEffect, useRef, useState } from "react";
import { Boxes, ExternalLink, RefreshCw, Send, Square } from "lucide-react";
import { api } from "../api.js";
import { Card, Empty, OfflineNotice, PageIntro } from "../components/ui.jsx";

const LOCAL_URL = "http://127.0.0.1:11434";
const CLOUD_URL = "https://ollama.com";

const KEEP_ALIVE = [
  ["0", "Unload right after each reply"],
  ["5m", "5 minutes (Ollama default)"],
  ["30m", "30 minutes"],
  ["1h", "1 hour"],
  ["-1", "Keep loaded until Ollama stops"],
];

const formatBytes = (n) => {
  if (n == null) return "–";
  const gb = n / 1024 ** 3;
  return gb >= 1 ? `${gb.toFixed(1)} GB` : `${Math.round(n / 1024 ** 2)} MB`;
};

const formatDate = (iso) => {
  if (!iso) return "–";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "–" : d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
};

const seconds = (ns) => (ns == null ? null : ns / 1e9);

function Row({ id, label, hint, children }) {
  return (
    <div className="form-row">
      <div className="form-label">
        <label htmlFor={id}>{label}</label>
        {hint && <p id={`${id}-hint`}>{hint}</p>}
      </div>
      <div className="form-control">{children}</div>
    </div>
  );
}

function ConnectionStatus({ status, checking, untested }) {
  if (checking) return <span className="badge">Checking…</span>;
  if (untested) return <span className="badge">Not tested</span>;
  if (!status) return null;
  if (!status.reachable) return <span className="badge badge-bad">Not reachable</span>;
  const n = status.models.length;
  return (
    <span className="badge badge-ok">
      Connected{status.version ? `, v${status.version}` : ""}, {n} model{n === 1 ? "" : "s"}
    </span>
  );
}

/* ---------- Try it ---------- */

const FOLLOW_SLACK = 32; // px from the bottom that still counts as "following" the stream

function TryIt({ config, models, onFinished }) {
  const [model, setModel] = useState("");
  const [picked, setPicked] = useState(false); // follow the saved default until the user chooses
  const [prompt, setPrompt] = useState("");
  const [think, setThink] = useState(false);
  const [run, setRun] = useState(null); // { thinking, content, error, stats, running, stopped, model }
  const [following, setFollowing] = useState(true);
  const [reasoningOpen, setReasoningOpen] = useState(true);

  const abortRef = useRef(null);
  const bodyRef = useRef(null);
  const outputRef = useRef(null);
  const followRef = useRef(true);
  const reasoningToggledRef = useRef(false); // once the user opens or closes it, stop auto-toggling
  const pendingRef = useRef({ thinking: "", content: "" });
  const frameRef = useRef(0);

  useEffect(() => {
    if (picked || !models.length) return;
    const fallback = models.some((m) => m.name === config.default_model) ? config.default_model : models[0].name;
    setModel(fallback);
  }, [models, config.default_model, picked]);

  // Streams can send hundreds of tiny chunks a second; render at most once per frame.
  const flush = useCallback(() => {
    frameRef.current = 0;
    const { thinking, content } = pendingRef.current;
    if (!thinking && !content) return;
    pendingRef.current = { thinking: "", content: "" };
    setRun((r) => (r ? { ...r, thinking: r.thinking + thinking, content: r.content + content } : r));
  }, []);
  const scheduleFlush = useCallback(() => {
    if (!frameRef.current) frameRef.current = requestAnimationFrame(flush);
  }, [flush]);

  const stop = useCallback(() => {
    if (!abortRef.current) return;
    abortRef.current.abort();
    abortRef.current = null;
    cancelAnimationFrame(frameRef.current);
    flush();
    setRun((r) => (r ? { ...r, running: false, stopped: true } : r));
  }, [flush]);

  useEffect(() => () => {
    abortRef.current?.abort();
    cancelAnimationFrame(frameRef.current);
  }, []);

  // Esc stops a running reply from anywhere on the page.
  useEffect(() => {
    if (!run?.running) return undefined;
    const onKey = (e) => {
      if (e.key === "Escape") stop();
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [run?.running, stop]);

  // Keep the newest text in view unless the user has scrolled up to read.
  useLayoutEffect(() => {
    const el = bodyRef.current;
    if (!el || !followRef.current) return;
    el.scrollTop = el.scrollHeight;
    // While the box is still growing toward its max height, keep its bottom edge on screen too,
    // but only if the user is looking at it (its top is visible), so we never yank them back.
    const page = document.getElementById("main");
    const box = outputRef.current?.getBoundingClientRect();
    const view = page?.getBoundingClientRect();
    if (box && view && box.top < view.bottom && box.bottom > view.bottom) {
      page.scrollTop += box.bottom - view.bottom + 16;
    }
  }, [run?.thinking, run?.content, reasoningOpen]);

  // Reasoning stays open while the model thinks, then folds away once the answer starts.
  useEffect(() => {
    if (!reasoningToggledRef.current && run?.content) setReasoningOpen(false);
  }, [run?.content]);

  const onScroll = () => {
    const el = bodyRef.current;
    const atBottom = el.scrollHeight - el.scrollTop - el.clientHeight <= FOLLOW_SLACK;
    followRef.current = atBottom;
    setFollowing(atBottom);
  };

  const jumpToLatest = () => {
    followRef.current = true;
    setFollowing(true);
    bodyRef.current.scrollTop = bodyRef.current.scrollHeight;
  };

  const send = async (e) => {
    e.preventDefault();
    if (!prompt.trim() || !model || run?.running) return;
    const controller = new AbortController();
    abortRef.current = controller;
    pendingRef.current = { thinking: "", content: "" };
    followRef.current = true;
    reasoningToggledRef.current = false;
    setFollowing(true);
    setReasoningOpen(true);
    setRun({ thinking: "", content: "", error: null, stats: null, running: true, stopped: false, model });
    requestAnimationFrame(() => outputRef.current?.scrollIntoView({ block: "nearest", behavior: "smooth" }));

    try {
      await api.ollama.chat(
        { model, messages: [{ role: "user", content: prompt }], think: think || null },
        (ev) => {
          if (ev.type === "thinking" || ev.type === "content") {
            pendingRef.current[ev.type] += ev.text;
            scheduleFlush();
            return;
          }
          if (ev.type === "done" || ev.type === "error") {
            cancelAnimationFrame(frameRef.current);
            flush();
            setRun((r) => ({ ...r, running: false, ...(ev.type === "done" ? { stats: ev.stats } : { error: ev.message }) }));
          }
        },
        controller.signal,
      );
      flush();
      setRun((r) => (r?.running ? { ...r, running: false } : r));
    } catch (err) {
      if (err.name !== "AbortError") {
        flush();
        setRun((r) => ({ ...r, running: false, error: `The request failed: ${err.message}` }));
      }
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
      onFinished();
    }
  };

  const st = run?.stats;
  const tokPerSec = st?.eval_count && st?.eval_duration ? st.eval_count / seconds(st.eval_duration) : null;

  let stateText = "";
  if (run?.running) stateText = run.content ? "Writing…" : run.thinking ? "Reasoning…" : `Waiting for ${run.model}…`;
  else if (run?.stopped) stateText = "Stopped";
  else if (run?.error) stateText = "Failed";
  else if (run) stateText = "Done";
  const reasoningWords = run?.thinking ? run.thinking.trim().split(/\s+/).length : 0;

  return (
    <Card title="Try it" description="Send a prompt with the settings saved above. Unsaved changes aren’t used." className="mb" flush>
      {models.length === 0 ? (
        <p className="card-body muted">Install a model first to try a prompt.</p>
      ) : (
        <form className="try" onSubmit={send}>
          <div className="try-controls">
            <label className="sr-only" htmlFor="try-model">Model</label>
            <select id="try-model" className="input select" value={model} onChange={(e) => { setModel(e.target.value); setPicked(true); }} disabled={run?.running}>
              {models.map((m) => <option key={m.name} value={m.name}>{m.name}</option>)}
            </select>
            <label className="check" title="Only models that support thinking will show their reasoning">
              <input type="checkbox" checked={think} onChange={(e) => setThink(e.target.checked)} disabled={run?.running} />
              Show reasoning
            </label>
          </div>
          <label className="sr-only" htmlFor="try-prompt">Prompt</label>
          <textarea
            id="try-prompt"
            className="input textarea"
            rows={3}
            value={prompt}
            placeholder="Summarise what the TP53 gene does in two sentences."
            onChange={(e) => setPrompt(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) e.currentTarget.form.requestSubmit();
            }}
          />
          <div className="try-actions">
            <span className="muted small">{run?.running ? "Esc to stop" : "Ctrl+Enter to send"}</span>
            {/* While running, Stop lives in the reply header, which stays on screen. */}
            <button type="submit" className="btn btn-primary" disabled={!prompt.trim() || run?.running}><Send size={13} /> Send</button>
          </div>

          {run && (
            <div className="output" ref={outputRef}>
              {/* Header stays put while the body scrolls, so Stop is always in reach. */}
              <div className="output-head">
                <span className={"output-state" + (run.running ? " is-running" : "")} role="status">
                  {run.running && <i aria-hidden="true" />}
                  {stateText}
                </span>
                {st && (
                  <span className="output-stats">
                    {st.eval_count} tokens
                    {tokPerSec ? `, ${tokPerSec.toFixed(1)}/s` : ""}
                    {st.total_duration ? `, ${seconds(st.total_duration).toFixed(1)}s` : ""}
                    {st.load_duration && seconds(st.load_duration) > 1 ? ` (${seconds(st.load_duration).toFixed(1)}s loading)` : ""}
                  </span>
                )}
                {run.running && (
                  <button type="button" className="btn btn-sm" onClick={stop}><Square size={12} /> Stop</button>
                )}
              </div>

              <div className="output-body" ref={bodyRef} onScroll={onScroll} tabIndex={0} aria-label="Model reply">
                {run.thinking && (
                  <details
                    className="thinking"
                    open={reasoningOpen}
                    onToggle={(e) => {
                      if (e.currentTarget.open !== reasoningOpen) {
                        reasoningToggledRef.current = true;
                        setReasoningOpen(e.currentTarget.open);
                      }
                    }}
                  >
                    <summary>Reasoning{reasoningOpen ? "" : `, ${reasoningWords} words`}</summary>
                    <p>{run.thinking}</p>
                  </details>
                )}
                {run.content ? (
                  <p className="answer">{run.content}{run.running && <span className="caret" aria-hidden="true" />}</p>
                ) : run.running && !run.thinking ? (
                  <p className="muted">The first reply can take a while if the model has to load.</p>
                ) : null}
                {run.error && <p className="bad">{run.error}</p>}
              </div>

              {!following && run.running && (
                <button type="button" className="btn btn-sm jump" onClick={jumpToLatest}>Jump to latest</button>
              )}
            </div>
          )}
        </form>
      )}
    </Card>
  );
}

/* ---------- Page ---------- */

export function OllamaSettings({ apiOnline }) {
  const [saved, setSaved] = useState(null);
  const [form, setForm] = useState(null);
  const [status, setStatus] = useState(null);
  const [checking, setChecking] = useState(false);
  const [test, setTest] = useState(null); // result of testing an unsaved address: { url, result }
  const [save, setSave] = useState({ saving: false, done: false, error: null });

  const refresh = useCallback(async () => {
    setChecking(true);
    try {
      setStatus(await api.ollama.status());
    } catch (err) {
      setStatus({ reachable: false, models: [], running: [], error: err.message });
    } finally {
      setChecking(false);
    }
  }, []);

  useEffect(() => {
    if (!apiOnline) return;
    api.ollama.getConfig().then((c) => { setSaved(c); setForm(c); }).catch(() => {});
    refresh();
  }, [apiOnline, refresh]);

  if (apiOnline === false) return <OfflineNotice />;
  if (!form) return <p className="muted">Loading Ollama settings…</p>;

  const set = (key, value) => {
    setForm((f) => ({ ...f, [key]: value }));
    setSave({ saving: false, done: false, error: null });
  };
  const numberOrNull = (v) => (v === "" ? null : Number(v));
  const dirty = JSON.stringify(form) !== JSON.stringify(saved);
  const addressChanged = form.base_url !== saved.base_url;
  // A test result only counts for the address it was run against.
  const tested = addressChanged && test?.url === form.base_url ? test.result : null;
  const shown = addressChanged ? tested : status;

  const testConnection = async () => {
    if (!addressChanged) {
      setTest(null);
      refresh();
      return;
    }
    const url = form.base_url;
    setChecking(true);
    try {
      setTest({ url, result: await api.ollama.status(url) });
    } catch (err) {
      setTest({ url, result: { reachable: false, models: [], error: err.message } });
    } finally {
      setChecking(false);
    }
  };

  let footText;
  let footTone = "muted";
  if (addressChanged && !tested) {
    footText = "This address isn’t saved yet. Test it, then save to use it.";
  } else if (addressChanged && tested.reachable) {
    const n = tested.models.length;
    footText = `It works: ${tested.url} answered${tested.version ? ` (Ollama ${tested.version})` : ""} with ${n} model${n === 1 ? "" : "s"}. Save to use it.`;
    footTone = "ok";
  } else if (shown && !shown.reachable) {
    footText = shown.error ?? "Couldn’t connect.";
    footTone = "bad";
  } else {
    footText = shown?.reachable ? `Connected to ${shown.url}.` : "Checking the connection…";
  }

  const onSave = async (e) => {
    e.preventDefault();
    setSave({ saving: true, done: false, error: null });
    try {
      const next = await api.ollama.saveConfig(form);
      setSaved(next);
      setForm(next);
      setSave({ saving: false, done: true, error: null });
      if (addressChanged) {
        setTest(null);
        refresh();
      }
    } catch (err) {
      setSave({ saving: false, done: false, error: `Changes weren’t saved: ${err.message}` });
    }
  };

  const models = status?.models ?? [];
  const running = new Set((status?.running ?? []).map((m) => m.name));
  const isCloud = form.base_url.replace(/\/+$/, "") === CLOUD_URL;
  const defaultMissing = form.default_model && status?.reachable && !models.some((m) => m.name === form.default_model);

  return (
    <>
      <PageIntro>
        Run open-weight models on your own hardware, or Ollama’s cloud models.{" "}
        <a className="link inline" href="https://docs.ollama.com" target="_blank" rel="noreferrer">
          Ollama docs <ExternalLink size={12} />
        </a>
      </PageIntro>

      <form onSubmit={onSave}>
        <Card
          title="Connection"
          description="Where Helix finds your Ollama server."
          actions={<ConnectionStatus status={shown} checking={checking} untested={addressChanged && !tested} />}
          className="mb"
          flush
        >
          <Row id="ol-url" label="Server address" hint="Local Ollama listens on port 11434. Use https://ollama.com for cloud models.">
            <div className="stack">
              <input
                id="ol-url"
                className="input mono"
                value={form.base_url}
                onChange={(e) => set("base_url", e.target.value)}
                aria-describedby="ol-url-hint"
                spellCheck={false}
              />
              <div className="presets" role="group" aria-label="Address presets">
                <button type="button" className="btn btn-sm" aria-pressed={form.base_url === LOCAL_URL} onClick={() => set("base_url", LOCAL_URL)}>This computer</button>
                <button type="button" className="btn btn-sm" aria-pressed={isCloud} onClick={() => set("base_url", CLOUD_URL)}>Ollama cloud</button>
              </div>
            </div>
          </Row>
          {(isCloud || status?.api_key_present) && (
            <Row id="ol-key" label="API key">
              <p className={status?.api_key_present ? "ok" : "muted"}>
                {status?.api_key_present
                  ? "OLLAMA_API_KEY found in backend/.env."
                  : <>Cloud models need <code>OLLAMA_API_KEY</code> in <code>backend/.env</code>. Create one in your ollama.com account settings.</>}
              </p>
            </Row>
          )}
          <div className="card-foot">
            <p className={footTone} role="status">{footText}</p>
            <button type="button" className="btn btn-sm" onClick={testConnection} disabled={checking || !form.base_url.trim()}>
              <RefreshCw size={13} className={checking ? "spin" : ""} /> Test connection
            </button>
          </div>
        </Card>

        <Card title="Defaults" description="Applied to every request Helix sends to Ollama." className="mb" flush>
          <Row id="ol-model" label="Default model" hint={defaultMissing ? "This model isn’t installed on the server." : "Used when a request doesn’t name a model."}>
            <select id="ol-model" className="input select" value={form.default_model} onChange={(e) => set("default_model", e.target.value)} aria-describedby="ol-model-hint">
              <option value="">None</option>
              {defaultMissing && <option value={form.default_model}>{form.default_model} (not installed)</option>}
              {models.map((m) => <option key={m.name} value={m.name}>{m.name}{m.parameter_size ? `, ${m.parameter_size}` : ""}</option>)}
            </select>
          </Row>
          <Row id="ol-keep" label="Keep model loaded" hint="Keeping a model in memory makes the next reply start faster, but holds on to RAM or VRAM.">
            <select id="ol-keep" className="input select" value={form.keep_alive} onChange={(e) => set("keep_alive", e.target.value)} aria-describedby="ol-keep-hint">
              {!KEEP_ALIVE.some(([v]) => v === form.keep_alive) && <option value={form.keep_alive}>{form.keep_alive}</option>}
              {KEEP_ALIVE.map(([v, label]) => <option key={v} value={v}>{label}</option>)}
            </select>
          </Row>
          <Row id="ol-temp" label="Temperature" hint="Lower is more focused and repeatable, higher is more varied. Leave empty for the model’s default.">
            <input id="ol-temp" className="input" type="number" min="0" max="2" step="0.1" placeholder="Model default"
              value={form.temperature ?? ""} onChange={(e) => set("temperature", numberOrNull(e.target.value))} aria-describedby="ol-temp-hint" />
          </Row>
          <Row id="ol-ctx" label="Context length" hint="How many tokens the model can see at once. Larger values use more memory. Leave empty for the model’s default.">
            <input id="ol-ctx" className="input" type="number" min="256" step="256" placeholder="Model default"
              value={form.num_ctx ?? ""} onChange={(e) => set("num_ctx", numberOrNull(e.target.value))} aria-describedby="ol-ctx-hint" />
          </Row>
          <Row id="ol-timeout" label="Timeout" hint="Give up if Ollama sends nothing for this many seconds. Large models on slow hardware may need more.">
            <div className="with-unit">
              <input id="ol-timeout" className="input" type="number" min="5" max="3600" step="5"
                value={form.timeout_seconds} onChange={(e) => set("timeout_seconds", Number(e.target.value) || 120)} aria-describedby="ol-timeout-hint" />
              <span className="muted">seconds</span>
            </div>
          </Row>
        </Card>

        <div className="form-actions">
          <span role="status" className={save.error ? "bad" : save.done && !dirty ? "ok" : "muted"}>
            {save.error ?? (save.done && !dirty ? "Changes saved" : dirty ? "Unsaved changes" : "")}
          </span>
          <button type="button" className="btn" disabled={!dirty} onClick={() => setForm(saved)}>Discard</button>
          <button type="submit" className="btn btn-primary" disabled={!dirty || save.saving}>{save.saving ? "Saving…" : "Save changes"}</button>
        </div>
      </form>

      <Card
        title="Installed models"
        description={status?.reachable ? `${models.length} on ${status.url}` : "Models on the Ollama server."}
        actions={<button type="button" className="btn btn-sm" onClick={refresh} disabled={checking}><RefreshCw size={13} className={checking ? "spin" : ""} /> Refresh</button>}
        className="mb"
        flush
      >
        {!status?.reachable ? (
          <p className="card-body muted">{checking ? "Loading…" : "Connect to an Ollama server to see its models."}</p>
        ) : models.length === 0 ? (
          <Empty icon={Boxes} title="No models installed">
            Download one from a terminal, for example <code>ollama pull qwen3</code>, then refresh.
          </Empty>
        ) : (
          <table className="table models-table">
            <thead>
              <tr><th>Model</th><th>Parameters</th><th>Quantization</th><th>Size</th><th>Updated</th></tr>
            </thead>
            <tbody>
              {models.map((m) => (
                <tr key={m.name}>
                  <td className="strong">
                    <span className="mono-name">{m.name}</span>
                    {m.name === saved.default_model && <span className="badge badge-cyan">Default</span>}
                    {running.has(m.name) && <span className="badge badge-ok">Loaded</span>}
                    {m.remote && <span className="badge">Cloud</span>}
                  </td>
                  <td>{m.parameter_size ?? "–"}</td>
                  <td>{m.quantization ?? "–"}</td>
                  <td>{formatBytes(m.size)}</td>
                  <td className="muted">{formatDate(m.modified_at)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Card>

      {status?.reachable && <TryIt config={saved} models={models} onFinished={refresh} />}
    </>
  );
}
