import { useEffect, useState } from "react";
import { Download, ExternalLink, Settings2 } from "lucide-react";
import { api } from "../api.js";
import { Card, OfflineNotice, PageIntro } from "../components/ui.jsx";

const GROUPS = [
  { kind: "ai", title: "AI models", description: "Models that read, summarise and reason over your data." },
  { kind: "data", title: "Data sources", description: "Public databases Helix will search and import from." },
];

// Every row is one short badge; the detail is a tooltip, so the column reads the same everywhere.
function statusOf(item, ollama, openai) {
  if (item.id === "ollama") {
    if (!ollama) return { label: "Checking…" };
    if (!ollama.reachable) return { label: "Not reachable", tone: "badge-bad", note: "Is Ollama running?" };
    const n = ollama.models.length;
    return { label: "Connected", tone: "badge-ok", note: `${n} model${n === 1 ? "" : "s"} available` };
  }
  if (item.id === "openai") {
    if (!openai) return { label: "Checking…" };
    if (!openai.api_key_present) return { label: "Needs key", note: "Add OPENAI_API_KEY to backend/.env" };
    if (!openai.reachable) return { label: "Connection failed", tone: "badge-bad", note: openai.error };
    return { label: "Connected", tone: "badge-ok", note: `${openai.models.length} supported models available` };
  }
  const key = item.env_key;
  if (!item.implemented) {
    return { label: "Coming soon", note: key && (item.status === "key_found" ? `${key} found` : `Will need ${key}`) };
  }
  if (item.status === "key_found") return { label: "Ready", tone: "badge-ok", note: `${key} found` };
  if (item.status === "ready") return { label: "Ready", tone: "badge-ok", note: item.key_optional && `Works without a key; ${key} raises the rate limit` };
  return { label: "Needs key", note: key && `Add ${key} to backend/.env` };
}

function Status({ item, ollama, openai }) {
  const { label, tone = "", note } = statusOf(item, ollama, openai);
  return <span className={`badge ${tone}`} title={note || undefined}>{label}</span>;
}

export function Integrations({ apiOnline, link }) {
  const [items, setItems] = useState(null);
  const [error, setError] = useState(null);
  const [ollama, setOllama] = useState(null);
  const [openai, setOpenai] = useState(null);

  useEffect(() => {
    if (apiOnline === false) return;
    api.integrations().then(setItems).catch((e) => setError(e.message));
    api.ollama.status().then(setOllama).catch(() => setOllama({ reachable: false, models: [] }));
    api.openai.status().then(setOpenai).catch(() => setOpenai({ reachable: false, api_key_present: false, models: [] }));
  }, [apiOnline]);

  return (
    <>
      <PageIntro>
        Services Helix works with. OpenAI, Ollama, PubMed, PubMed Central and Europe PMC work today; the others are coming. Keys you add to <code>backend/.env</code> show
        up here after a backend restart.
      </PageIntro>

      {(apiOnline === false || error) && <OfflineNotice />}

      {GROUPS.map((g) => (
        <Card key={g.kind} title={g.title} description={g.description} className="mb" flush>
          {!items ? (
            <p className="card-body muted">{apiOnline === false || error ? "Unavailable while the backend is offline." : "Loading…"}</p>
          ) : (
            <table className="table">
              <thead>
                <tr><th>Name</th><th>What it’s for</th><th className="center">Status</th><th><span className="sr-only">Actions</span></th></tr>
              </thead>
              <tbody>
                {items.filter((i) => i.kind === g.kind).map((i) => (
                  <tr key={i.id}>
                    <td className="strong">
                      <span className={"chip-dot " + (g.kind === "ai" ? "cyan" : "magenta")} aria-hidden="true" />
                      {i.name}
                    </td>
                    <td className="muted">{i.description}</td>
                    <td className="center"><Status item={i} ollama={ollama} openai={openai} /></td>
                    <td className="right">
                      {/* Docs stays last so it lines up in every row */}
                      <div className="row-actions">
                        {i.configurable && (
                          <a className="btn btn-sm" {...link(i.category === "literature" ? `/literature/fetch/${i.id}` : `/integrations/${i.id}`)}>
                            {i.kind === "data"
                              ? <><Download size={13} /> Fetch<span className="sr-only"> from {i.name}</span></>
                              : <><Settings2 size={13} /> Configure<span className="sr-only"> {i.name}</span></>}
                          </a>
                        )}
                        <a className="link" href={i.docs_url} target="_blank" rel="noreferrer">
                          Docs <ExternalLink size={12} /><span className="sr-only"> for {i.name}, opens in a new tab</span>
                        </a>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </Card>
      ))}
    </>
  );
}
