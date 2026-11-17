import { useEffect, useState } from "react";
import { ExternalLink } from "lucide-react";
import { api } from "../api.js";
import { Card, OfflineNotice, PageIntro } from "../components/ui.jsx";

const GROUPS = [
  { kind: "ai", title: "AI models", description: "Models that read, summarise and reason over your data." },
  { kind: "data", title: "Data sources", description: "Public databases Helix will search and import from." },
];

function Status({ item }) {
  if (item.status === "key_found") return <span className="badge badge-ok">Key found</span>;
  if (item.env_key) return <span className="badge">Needs <code>{item.env_key}</code></span>;
  if (item.id === "ollama") return <span className="badge">Runs locally</span>;
  return <span className="badge">No key needed</span>;
}

export function Integrations({ apiOnline }) {
  const [items, setItems] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (apiOnline === false) return;
    api.integrations().then(setItems).catch((e) => setError(e.message));
  }, [apiOnline]);

  return (
    <>
      <PageIntro>
        Services Helix is being built to work with. Nothing calls them yet. Keys you add to <code>backend/.env</code> show
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
                <tr><th>Name</th><th>What it’s for</th><th>Status</th><th><span className="sr-only">Docs</span></th></tr>
              </thead>
              <tbody>
                {items.filter((i) => i.kind === g.kind).map((i) => (
                  <tr key={i.id}>
                    <td className="strong">
                      <span className={"chip-dot " + (g.kind === "ai" ? "cyan" : "magenta")} aria-hidden="true" />
                      {i.name}
                    </td>
                    <td className="muted">{i.description}</td>
                    <td><Status item={i} /></td>
                    <td className="right">
                      <a className="link" href={i.docs_url} target="_blank" rel="noreferrer">
                        Docs <ExternalLink size={12} /><span className="sr-only"> for {i.name}, opens in a new tab</span>
                      </a>
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
