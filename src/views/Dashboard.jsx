import { useEffect, useState } from "react";
import { BookOpen, Bot, Check, Database, FolderKanban } from "lucide-react";
import { api } from "../api.js";
import { DnaHelix } from "../components/DnaHelix.jsx";
import { Card, OfflineNotice } from "../components/ui.jsx";

const formatUptime = (s) => {
  if (s == null) return "–";
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h) return `${h}h ${m}m`;
  if (m) return `${m}m`;
  return `${s}s`;
};

function Stat({ icon: Icon, label, value, note }) {
  return (
    <div className="stat">
      <div className="stat-top">
        <span className="stat-label">{label}</span>
        <Icon size={16} strokeWidth={1.75} className="stat-icon" />
      </div>
      <div className="stat-value">{value ?? "–"}</div>
      <div className="stat-note">{note}</div>
    </div>
  );
}

function Setup({ settings, overview, ollama, link }) {
  const keys = overview?.ai_keys ?? [];
  const steps = [
    {
      title: "Add your organization",
      done: Boolean(settings?.organization),
      detail: settings?.organization || "Shown on exports and reports.",
      to: "/settings",
      action: "Open settings",
    },
    {
      title: "Start Ollama for local models",
      done: Boolean(ollama?.reachable),
      detail: !ollama
        ? "Checking…"
        : ollama.reachable
          ? `Found ${ollama.models.length} model${ollama.models.length === 1 ? "" : "s"} at ${ollama.url}`
          : `Nothing is answering at ${ollama.url}`,
      to: "/integrations/ollama",
      action: "Configure",
    },
    {
      title: "Add a cloud model key",
      done: keys.length > 0,
      detail: keys.length ? `Found ${keys.join(", ")}` : <>Add <code>OPENAI_API_KEY</code> or <code>ANTHROPIC_API_KEY</code> to <code>backend/.env</code></>,
    },
    {
      title: "Choose a default model",
      done: Boolean(settings?.default_model),
      detail: settings?.default_model || "Used when a run doesn’t name its own model.",
      to: "/settings",
      action: "Open settings",
    },
  ];
  const done = steps.filter((s) => s.done).length;

  return (
    <Card
      title="Get started"
      description={`${done} of ${steps.length} steps done`}
      actions={<div className="progress" aria-hidden="true"><i style={{ width: `${(done / steps.length) * 100}%` }} /></div>}
      flush
    >
      <ol className="steps">
        {steps.map((s, i) => (
          <li key={s.title} className={s.done ? "is-done" : ""}>
            <span className="step-mark" aria-hidden="true">{s.done ? <Check size={12} strokeWidth={3} /> : i + 1}</span>
            <div className="step-body">
              <div className="step-title">{s.title}{s.done && <span className="sr-only"> (done)</span>}</div>
              <div className="step-detail">{s.detail}</div>
            </div>
            {s.to && !s.done && <a className="btn btn-sm" {...link(s.to)}>{s.action}</a>}
          </li>
        ))}
      </ol>
    </Card>
  );
}

function System({ apiOnline, health, overview }) {
  const rows = [
    ["Backend", apiOnline ? <span className="ok">Running</span> : apiOnline === false ? <span className="bad">Offline</span> : "…"],
    ["Version", health?.version],
    ["Python", health?.python],
    ["Uptime", formatUptime(health?.uptime_seconds)],
    ["Integrations ready", overview && `${overview.integrations_ready} of ${overview.integrations_total}`],
  ];
  return (
    <Card title="System" description="FastAPI backend" flush>
      <dl className="kv">
        {rows.map(([k, v]) => (
          <div key={k}>
            <dt>{k}</dt>
            <dd>{v ?? "–"}</dd>
          </div>
        ))}
      </dl>
    </Card>
  );
}

export function Dashboard({ settings, apiOnline, link, onRetry }) {
  const [overview, setOverview] = useState(null);
  const [health, setHealth] = useState(null);
  const [ollama, setOllama] = useState(null);

  useEffect(() => {
    if (!apiOnline) return;
    api.overview().then(setOverview).catch(() => {});
    api.health().then(setHealth).catch(() => {});
    api.ollama.status().then(setOllama).catch(() => {});
  }, [apiOnline]);

  return (
    <>
      {apiOnline === false && <OfflineNotice onRetry={onRetry} />}

      <section className="banner">
        <div className="banner-copy">
          <h2 className="banner-title">{settings?.workspace_name || "Helix"}</h2>
          <p className="banner-text">
            Genomic data, literature and AI-assisted analysis in one workspace. Connect the models and
            sources you use, and new research tools will plug into them as they ship.
          </p>
          <div className="banner-actions">
            <a className="btn btn-primary" {...link("/integrations")}>Connect a model</a>
            <a className="btn" {...link("/settings")}>Workspace settings</a>
          </div>
        </div>
        <div className="banner-art">
          <DnaHelix still={settings?.reduce_motion} />
        </div>
      </section>

      <section className="stats" aria-label="Workspace contents">
        <Stat icon={FolderKanban} label="Projects" value={overview?.projects} note="None created yet" />
        <Stat icon={Database} label="Datasets" value={overview?.datasets} note="Nothing imported yet" />
        <Stat icon={BookOpen} label="Papers" value={overview?.literature} note="Library is empty" />
        <Stat icon={Bot} label="AI runs" value={overview?.ai_runs} note="No analyses yet" />
      </section>

      <div className="split">
        <Setup settings={settings} overview={overview} ollama={ollama} link={link} />
        <System apiOnline={apiOnline} health={health} overview={overview} />
      </div>
    </>
  );
}
