import { useEffect, useState } from "react";
import { api } from "../api.js";
import { Card, OfflineNotice, PageIntro } from "../components/ui.jsx";

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

export function Settings({ settings, onSaved, apiOnline, onRetry }) {
  const [form, setForm] = useState(settings);
  const [status, setStatus] = useState({ saving: false, saved: false, error: null });
  const [models, setModels] = useState([]);

  useEffect(() => { setForm(settings); }, [settings]);
  useEffect(() => {
    if (!apiOnline) return;
    api.ollama().then((o) => setModels(o.models)).catch(() => {});
  }, [apiOnline, settings?.ollama_base_url]);

  if (!form) {
    return apiOnline === false ? <OfflineNotice onRetry={onRetry} /> : <p className="muted">Loading settings…</p>;
  }

  const update = (key) => (e) => {
    const { type, checked, value } = e.target;
    setForm({ ...form, [key]: type === "checkbox" ? checked : value });
    setStatus({ saving: false, saved: false, error: null });
  };
  const dirty = JSON.stringify(form) !== JSON.stringify(settings);

  const save = async (e) => {
    e.preventDefault();
    setStatus({ saving: true, saved: false, error: null });
    try {
      onSaved(await api.saveSettings(form));
      setStatus({ saving: false, saved: true, error: null });
    } catch (err) {
      setStatus({ saving: false, saved: false, error: `Changes weren’t saved: ${err.message}` });
    }
  };

  return (
    <form onSubmit={save}>
      <PageIntro>Stored on the backend, so everyone using this Helix sees the same values.</PageIntro>

      <Card title="Workspace" description="How this Helix names itself." className="mb" flush>
        <Row id="ws-name" label="Workspace name">
          <input id="ws-name" className="input" value={form.workspace_name} onChange={update("workspace_name")} required />
        </Row>
        <Row id="ws-org" label="Organization" hint="Shown on exports and reports.">
          <input id="ws-org" className="input" value={form.organization} onChange={update("organization")} placeholder="Institute of Molecular Biology" aria-describedby="ws-org-hint" />
        </Row>
        <Row id="ws-email" label="Contact email">
          <input id="ws-email" className="input" type="email" value={form.contact_email} onChange={update("contact_email")} placeholder="lab@example.org" />
        </Row>
      </Card>

      <Card title="Models" description="Defaults for AI runs. API keys belong in backend/.env, never here." className="mb" flush>
        <Row id="ollama-url" label="Ollama address" hint="Where your local Ollama server listens. Default port is 11434.">
          <input id="ollama-url" className="input mono" value={form.ollama_base_url} onChange={update("ollama_base_url")} aria-describedby="ollama-url-hint" />
        </Row>
        <Row
          id="model"
          label="Default model"
          hint={models.length ? `Suggestions come from your ${models.length} installed Ollama models.` : "For example llama3.1, gpt-5 or claude-opus-5-5."}
        >
          <input id="model" className="input mono" list="model-list" value={form.default_model} onChange={update("default_model")} aria-describedby="model-hint" />
          <datalist id="model-list">{models.map((m) => <option key={m} value={m} />)}</datalist>
        </Row>
      </Card>

      <Card title="Appearance" description="The helix on the dashboard." className="mb" flush>
        <Row id="motion" label="Animation">
          <label className="check">
            <input id="motion" type="checkbox" checked={form.reduce_motion} onChange={update("reduce_motion")} />
            Keep the helix still
          </label>
        </Row>
      </Card>

      <div className="form-actions">
        <span role="status" className={status.error ? "bad" : status.saved && !dirty ? "ok" : "muted"}>
          {status.error ?? (status.saved && !dirty ? "Changes saved" : dirty ? "Unsaved changes" : "")}
        </span>
        <button type="button" className="btn" disabled={!dirty} onClick={() => setForm(settings)}>Discard</button>
        <button type="submit" className="btn btn-primary" disabled={!dirty || status.saving}>
          {status.saving ? "Saving…" : "Save changes"}
        </button>
      </div>
    </form>
  );
}
