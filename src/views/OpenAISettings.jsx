import { useCallback, useEffect, useRef, useState } from "react";
import { ExternalLink, RefreshCw, Send, Square } from "lucide-react";
import { api } from "../api.js";
import { Card, OfflineNotice, PageIntro } from "../components/ui.jsx";

function Row({ id, label, hint, children }) {
  return <div className="form-row">
    <div className="form-label"><label htmlFor={id}>{label}</label>{hint && <p id={`${id}-hint`}>{hint}</p>}</div>
    <div className="form-control">{children}</div>
  </div>;
}

function TryIt({ config, models, enabled }) {
  const [model, setModel] = useState(config.default_model);
  const [picked, setPicked] = useState(false);
  const [prompt, setPrompt] = useState("");
  const [run, setRun] = useState(null);
  const abortRef = useRef(null);

  useEffect(() => {
    if (!picked && models.some((item) => item.name === config.default_model)) setModel(config.default_model);
  }, [config.default_model, models, picked]);

  const stop = useCallback(() => {
    abortRef.current?.abort();
    abortRef.current = null;
    setRun((current) => current ? { ...current, running: false, stopped: true } : current);
  }, []);

  useEffect(() => {
    const onKey = (event) => { if (event.key === "Escape" && abortRef.current) stop(); };
    document.addEventListener("keydown", onKey);
    return () => { document.removeEventListener("keydown", onKey); abortRef.current?.abort(); };
  }, [stop]);

  const send = async (event) => {
    event.preventDefault();
    if (!prompt.trim() || !model || run?.running || !enabled) return;
    const controller = new AbortController();
    abortRef.current = controller;
    setRun({ model, content: "", error: null, stats: null, running: true, stopped: false });
    try {
      await api.openai.chat(
        { model, messages: [{ role: "user", content: prompt.trim() }] },
        (item) => {
          if (item.type === "content") setRun((current) => ({ ...current, content: current.content + item.text }));
          if (item.type === "done") setRun((current) => ({ ...current, running: false, stats: item.stats }));
          if (item.type === "error") setRun((current) => ({ ...current, running: false, error: item.message }));
        },
        controller.signal,
      );
    } catch (error) {
      if (error.name !== "AbortError") setRun((current) => ({ ...current, running: false, error: `The request failed: ${error.message}` }));
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
    }
  };

  const state = run?.running ? (run.content ? "Writing…" : `Waiting for ${run.model}…`) : run?.stopped ? "Stopped" : run?.error ? "Failed" : "Done";
  return <Card title="Try it" description="Send a prompt with the settings saved above. Unsaved changes aren’t used." className="mb" flush>
    <form className="try" onSubmit={send}>
      <div className="try-controls">
        <label className="sr-only" htmlFor="openai-try-model">Model</label>
        <select id="openai-try-model" className="input select" value={model} onChange={(e) => { setModel(e.target.value); setPicked(true); }} disabled={run?.running}>
          {models.map((item) => <option key={item.name} value={item.name}>{item.name}</option>)}
        </select>
      </div>
      <label className="sr-only" htmlFor="openai-try-prompt">Prompt</label>
      <textarea id="openai-try-prompt" className="input textarea" rows={3} value={prompt}
        placeholder="Summarise what the TP53 gene does in two sentences."
        onChange={(e) => setPrompt(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) e.currentTarget.form.requestSubmit(); }} />
      <div className="try-actions">
        <span className="muted small">{!enabled ? "Connect OpenAI to send a prompt" : run?.running ? "Esc to stop" : "Ctrl+Enter to send"}</span>
        <button type="submit" className="btn btn-primary" disabled={!enabled || !prompt.trim() || run?.running}><Send size={13} /> Send</button>
      </div>
      {run && <div className="output">
        <div className="output-head">
          <span className={`output-state${run.running ? " is-running" : ""}`} role="status">{run.running && <i aria-hidden="true" />}{state}</span>
          {run.stats && <span className="output-stats">
            {run.stats.total_tokens ?? 0} tokens ({run.stats.input_tokens ?? 0} in, {run.stats.output_tokens ?? 0} out)
          </span>}
          {run.running && <button type="button" className="btn btn-sm" onClick={stop}><Square size={12} /> Stop</button>}
        </div>
        <div className="output-body" tabIndex={0} aria-label="Model reply">
          {run.content ? <p className="answer">{run.content}{run.running && <span className="caret" aria-hidden="true" />}</p>
            : run.running ? <p className="muted">Waiting for the first response token…</p> : null}
          {run.error && <p className="bad">{run.error}</p>}
        </div>
      </div>}
    </form>
  </Card>;
}

export function OpenAISettings({ apiOnline }) {
  const [form, setForm] = useState(null);
  const [saved, setSaved] = useState(null);
  const [connection, setConnection] = useState(null);
  const [checking, setChecking] = useState(false);
  const [save, setSave] = useState({ saving: false, done: false, error: null });

  const refresh = async () => {
    setChecking(true);
    try { setConnection(await api.openai.status()); }
    catch (e) { setConnection({ reachable: false, api_key_present: false, models: [], error: e.message }); }
    finally { setChecking(false); }
  };

  useEffect(() => {
    if (apiOnline === false) return;
    api.openai.getConfig().then((value) => { setForm(value); setSaved(value); }).catch(() => {});
    refresh();
  }, [apiOnline]);

  if (apiOnline === false) return <OfflineNotice />;
  if (!form) return <p className="muted">Loading OpenAI settings…</p>;

  const models = connection?.models ?? [];
  const dirty = JSON.stringify(form) !== JSON.stringify(saved);
  const modelMissing = form.default_model && !models.some((m) => m.name === form.default_model);
  const set = (key, value) => {
    setForm((current) => ({ ...current, [key]: value }));
    setSave({ saving: false, done: false, error: null });
  };
  const onSave = async (event) => {
    event.preventDefault();
    setSave({ saving: true, done: false, error: null });
    try {
      const value = await api.openai.saveConfig(form);
      setForm(value); setSaved(value); setSave({ saving: false, done: true, error: null });
    } catch (e) { setSave({ saving: false, done: false, error: e.message }); }
  };

  return <>
    <PageIntro>
      Use selected OpenAI models in Helix chat through the Responses API. The key stays on the backend.{" "}
      <a className="link inline" href="https://developers.openai.com/api/docs" target="_blank" rel="noreferrer">OpenAI docs <ExternalLink size={12} /></a>
    </PageIntro>

    <Card title="Connection" description="Helix reads the API key from the backend environment." className="mb" flush>
      <Row id="openai-key" label="API key">
        <p className={connection?.api_key_present ? "ok" : "muted"}>
          {connection?.api_key_present ? "OPENAI_API_KEY found in backend/.env." : <>Add <code>OPENAI_API_KEY</code> to <code>backend/.env</code>, then restart the backend.</>}
        </p>
      </Row>
      <div className="card-foot">
        <p className={connection?.reachable ? "ok" : connection?.error ? "bad" : "muted"} role="status">
          {checking ? "Checking the connection…" : connection?.reachable
            ? `Connected. ${models.length} models are configured for Helix.`
            : connection?.error || "Not checked yet."}
        </p>
        <button type="button" className="btn btn-sm" onClick={refresh} disabled={checking}>
          <RefreshCw size={13} className={checking ? "spin" : ""} /> Test connection
        </button>
      </div>
    </Card>

    <form onSubmit={onSave}>
      <Card title="Defaults" description="Used for new OpenAI chats." className="mb" flush>
        <Row id="openai-model" label="Default model" hint={modelMissing ? "This saved model is no longer in Helix's supported list." : "The model selected when you switch to OpenAI."}>
          <select id="openai-model" className="input select" value={form.default_model} onChange={(e) => set("default_model", e.target.value)} aria-describedby="openai-model-hint">
            {modelMissing && <option value={form.default_model}>{form.default_model} (unsupported)</option>}
            {models.map((model) => <option key={model.name} value={model.name}>{model.name}</option>)}
          </select>
        </Row>
        <Row id="openai-timeout" label="Timeout" hint="Give up if OpenAI sends nothing for this many seconds.">
          <div className="with-unit">
            <input id="openai-timeout" className="input" type="number" min="5" max="3600" step="5" value={form.timeout_seconds}
              onChange={(e) => set("timeout_seconds", Number(e.target.value) || 120)} aria-describedby="openai-timeout-hint" />
            <span className="muted">seconds</span>
          </div>
        </Row>
      </Card>
      <div className="form-actions">
        <span role="status" className={save.error ? "bad" : save.done && !dirty ? "ok" : "muted"}>{save.error ?? (save.done && !dirty ? "Changes saved" : dirty ? "Unsaved changes" : "")}</span>
        <button type="button" className="btn" disabled={!dirty} onClick={() => setForm(saved)}>Discard</button>
        <button type="submit" className="btn btn-primary" disabled={!dirty || save.saving}>{save.saving ? "Saving…" : "Save changes"}</button>
      </div>
    </form>
    <TryIt config={saved} models={models} enabled={connection?.reachable === true} />
  </>;
}
