import { useCallback, useEffect, useMemo, useState } from "react";
import { Check, ExternalLink, FileText, Loader2, Minus, RefreshCw, Save } from "lucide-react";
import { api } from "../api.js";
import { RunProgress, useFollowRuns } from "./FetchRuns.jsx";
import { LIT_SOURCES, formatNumber, formatWhen } from "../literature.jsx";

const paras = (s) => (s ? s.split(/\n{2,}/) : []);

function RefLinks({ r }) {
  const links = [
    r.pmid && ["PubMed", `https://pubmed.ncbi.nlm.nih.gov/${r.pmid}/`],
    r.pmcid && ["PMC", `https://pmc.ncbi.nlm.nih.gov/articles/${r.pmcid}/`],
    r.doi && ["DOI", `https://doi.org/${r.doi}`],
  ].filter(Boolean);
  if (!links.length) return null;
  return (
    <span className="ref-links">
      {links.map(([label, href]) => (
        <a key={label} className="link" href={href} target="_blank" rel="noreferrer">{label}<span className="sr-only"> (opens in a new tab)</span></a>
      ))}
    </span>
  );
}

// Which source to save a reference from: PubMed when it has a PMID, else PubMed Central.
const saveVia = (r) => (r.pmid ? ["pubmed", r.pmid] : r.pmcid ? ["pmc", r.pmcid] : null);
const canSave = (r) => !r.saved && Boolean(saveVia(r));
const MAX_PER_RUN = 500;

function Box({ state }) {
  return (
    <span className={`multi-box is-${state}`} aria-hidden="true">
      {state === "all" ? <Check size={11} strokeWidth={3} /> : state === "some" ? <Minus size={11} strokeWidth={3} /> : null}
    </span>
  );
}

function RefList({ refs, numbered = true, label, articleTitle, onSaved }) {
  const [find, setFind] = useState("");
  const [picked, setPicked] = useState(() => new Set());
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const follow = useFollowRuns(() => onSaved?.());

  const needle = find.trim().toLowerCase();
  const shown = needle
    ? refs.map((r, i) => [r, i]).filter(([r]) => `${r.title} ${r.authors} ${r.journal} ${r.year ?? ""}`.toLowerCase().includes(needle))
    : refs.map((r, i) => [r, i]);
  const savableShown = shown.filter(([r]) => canSave(r)).map(([, i]) => i);
  const pickedShown = savableShown.filter((i) => picked.has(i)).length;
  const allState = pickedShown === 0 ? "none" : pickedShown === savableShown.length ? "all" : "some";
  const savedCount = refs.filter((r) => r.saved).length;
  const noIds = refs.filter((r) => !r.saved && !saveVia(r)).length;

  const toggle = (i) => setPicked((p) => { const n = new Set(p); if (n.has(i)) n.delete(i); else n.add(i); return n; });
  const toggleAll = () => setPicked((p) => {
    const n = new Set(p);
    if (allState === "all") savableShown.forEach((i) => n.delete(i)); else savableShown.forEach((i) => n.add(i));
    return n;
  });

  const save = async () => {
    setSaving(true);
    setError(null);
    const groups = {};
    for (const i of picked) {
      const via = saveVia(refs[i]);
      if (via) (groups[via[0]] ??= []).push(via[1]);
    }
    const term = `${label} “${articleTitle.length > 80 ? `${articleTitle.slice(0, 79)}…` : articleTitle}”`;
    const jobs = Object.entries(groups).map(([source, ids]) => [source, ids.slice(0, MAX_PER_RUN)]);
    const settled = await Promise.allSettled(jobs.map(([source, ids]) => api.literature.startFetch(source, { term, ids })));
    const started = settled.filter((r) => r.status === "fulfilled").map((r) => r.value);
    const failed = settled.flatMap((r, i) => (r.status === "rejected" ? [`${LIT_SOURCES[jobs[i][0]].name}: ${r.reason.message}`] : []));
    follow.show(started);
    if (failed.length) setError(failed.join(" "));
    setPicked(new Set());
    setSaving(false);
  };

  return (
    <>
      <div className="ref-bar">
        {savableShown.length > 0 && (
          <label className="pick" title="Select all that can be saved">
            <input type="checkbox" className="sr-only" checked={allState === "all"} onChange={toggleAll} />
            <Box state={allState} /><span className="sr-only">Select all</span>
          </label>
        )}
        {refs.length > 10 && (
          <input className="input ref-find" type="search" value={find} onChange={(e) => setFind(e.target.value)} placeholder="Filter by title, author, journal or year" aria-label="Filter this list" />
        )}
        <span className="muted small ref-summary">
          {savedCount > 0 && `${formatNumber(savedCount)} already in the library. `}
          {noIds > 0 && `${formatNumber(noIds)} can't be saved (no PubMed or PMC ID).`}
        </span>
        {picked.size > 0 && (
          <button type="button" className="btn btn-primary btn-sm" onClick={save} disabled={saving}>
            {saving ? <Loader2 size={12} className="spin" aria-hidden="true" /> : <Save size={12} aria-hidden="true" />}
            {saving ? "Starting…" : `Save ${formatNumber(Math.min(picked.size, MAX_PER_RUN))} to library`}
          </button>
        )}
      </div>
      {picked.size > MAX_PER_RUN && <p className="muted small">At most {MAX_PER_RUN} are saved per source at a time; save the rest afterwards.</p>}
      {error && <p className="bad small" role="alert">{error}</p>}
      {follow.runs.map((run) => (
        <RunProgress key={run.id} run={run} onCancel={follow.cancel} cancelling={follow.cancelling.has(run.id)} link={(href) => ({ href })} showSource />
      ))}
      <ol className="refs">
        {shown.map(([r, i]) => (
          <li key={i} className={`ref${picked.has(i) ? " is-picked" : ""}`}>
            {canSave(r) ? (
              <label className="pick">
                <input type="checkbox" className="sr-only" checked={picked.has(i)} onChange={() => toggle(i)} />
                <Box state={picked.has(i) ? "all" : "none"} /><span className="sr-only">Save “{r.title || "untitled"}”</span>
              </label>
            ) : <span className="pick" aria-hidden="true" />}
            {numbered && <span className="ref-num">{i + 1}</span>}
            <div className="ref-body">
              <p className="ref-title">{r.title || <span className="muted">Untitled</span>}</p>
              <p className="ref-meta">
                {r.saved && <span className="badge badge-ok">In library</span>}
                {r.authors && <span>{r.authors}</span>}
                {(r.journal || r.year) && <span className="muted">{[r.journal, r.year].filter(Boolean).join(" · ")}</span>}
                <RefLinks r={r} />
              </p>
            </div>
          </li>
        ))}
        {!shown.length && <li className="muted small">Nothing matches.</li>}
      </ol>
    </>
  );
}

function FullText({ sections, from }) {
  const body = sections.filter((s) => s.kind === "body");
  const back = sections.filter((s) => s.kind === "back");
  const [openAll, setOpenAll] = useState(false);
  const [key, setKey] = useState(0); // remount <details> when "expand all" flips
  const words = useMemo(() => sections.reduce((n, s) => n + s.text.split(/\s+/).length, 0), [sections]);

  const block = (list) => list.map((s, i) => (
    <details key={`${key}-${i}`} className="ft-sec" open={openAll || undefined}>
      <summary>{s.title || "Introduction"}</summary>
      {paras(s.text).map((p, j) => <p key={j}>{p}</p>)}
    </details>
  ));

  return (
    <div className="ft">
      <div className="ft-bar">
        <span className="muted small">{body.length} section{body.length === 1 ? "" : "s"}, about {formatNumber(words)} words, from {from}</span>
        <button type="button" className="link small" onClick={() => { setOpenAll(!openAll); setKey((k) => k + 1); }}>
          {openAll ? "Collapse all" : "Expand all"}
        </button>
      </div>
      {block(body)}
      {back.length > 0 && <><h4 className="ft-group">Statements and back matter</h4>{block(back)}</>}
    </div>
  );
}

function Table({ f }) {
  const head = f.rows.slice(0, f.head_rows);
  const body = f.rows.slice(f.head_rows);
  return (
    <div className="data-table-wrap">
      <table className="data-table">
        {head.length > 0 && <thead>{head.map((r, i) => <tr key={i}>{r.map((c, j) => <th key={j}>{c}</th>)}</tr>)}</thead>}
        <tbody>{body.map((r, i) => <tr key={i}>{r.map((c, j) => <td key={j}>{c}</td>)}</tr>)}</tbody>
      </table>
    </div>
  );
}

function Figures({ figures }) {
  return (
    <ul className="figs">
      {figures.map((f, i) => (
        <li key={i} className="fig">
          <p className="fig-head">
            <span className={`badge ${f.kind === "table" ? "badge-cyan" : ""}`}>{f.label || (f.kind === "table" ? "Table" : "Figure")}</span>
            {f.caption && <span>{f.caption}</span>}
          </p>
          {f.image && (
            <a className="fig-image" href={f.image} target="_blank" rel="noreferrer" title="Open full size">
              <img src={f.image} alt={f.caption ? `${f.label}: ${f.caption}` : f.label} loading="lazy" />
            </a>
          )}
          {f.rows?.length > 0 && <Table f={f} />}
          {f.footer && <p className="muted small">{f.footer}</p>}
          {!f.image && !f.rows?.length && f.kind === "figure" && <p className="muted small">The image isn't published openly.</p>}
        </li>
      ))}
    </ul>
  );
}

// "Not open access" won't change on a retry; anything else (a timeout, a failed download) might.
const retryable = (missing = []) => missing.some((m) => !/not open access|not in PubMed Central/.test(m));

function Refresh({ item, d, onRefreshed }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      onRefreshed(await api.literature.refresh(item.id));
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };
  const again = retryable(d.missing);
  return (
    <div className="refresh-row">
      <button type="button" className={`btn btn-sm${again ? " btn-primary" : ""}`} onClick={run} disabled={busy}>
        {busy ? <Loader2 size={12} className="spin" aria-hidden="true" /> : <RefreshCw size={12} aria-hidden="true" />}
        {busy ? "Fetching again…" : again ? "Retry missing details" : "Fetch again"}
      </button>
      <span className={error ? "bad small" : "muted small"} role="status">
        {error ?? (busy ? "Collecting full text, references and citations. This can take up to a minute." : "Gets the latest version and everything around it. Tags are kept.")}
      </span>
    </div>
  );
}

function Facts({ item, d }) {
  const authors = d.author_details?.length ? d.author_details : item.authors.map((name) => ({ name, orcid: null, affiliations: [] }));
  return (
    <div className="facts-grid">
      <dl className="article-facts">
        {authors.length > 0 && (
          <div><dt>Authors</dt><dd>
            <ul className="author-list">
              {authors.map((a, i) => (
                <li key={i}>
                  <span className="author-name">{a.name}</span>
                  {a.orcid && <a className="link small orcid" href={`https://orcid.org/${a.orcid}`} target="_blank" rel="noreferrer">ORCID<span className="sr-only"> for {a.name}</span></a>}
                  {a.affiliations.map((x, j) => <span key={j} className="affil">{x}</span>)}
                </li>
              ))}
            </ul>
          </dd></div>
        )}
        {d.grants?.length > 0 && <div><dt>Funding</dt><dd>{d.grants.map((g) => [g.agency, g.grant_id].filter(Boolean).join(" ")).join("; ")}</dd></div>}
        {item.mesh.length > 0 && <div><dt>MeSH</dt><dd>{item.mesh.join("; ")}</dd></div>}
        {item.keywords.length > 0 && <div><dt>Keywords</dt><dd>{item.keywords.join("; ")}</dd></div>}
        {d.chemicals?.length > 0 && <div><dt>Chemicals</dt><dd>{d.chemicals.join("; ")}</dd></div>}
        {item.publication_types.length > 0 && <div><dt>Type</dt><dd>{item.publication_types.join("; ")}</dd></div>}
        {item.language && <div><dt>Language</dt><dd>{item.language}</dd></div>}
        {d.license && <div><dt>Licence</dt><dd>{d.license.toUpperCase()}</dd></div>}
        {item.terms.length > 0 && <div><dt>Found by</dt><dd>{item.terms.join("; ")}</dd></div>}
        <div><dt>Saved</dt><dd>{formatWhen(item.created_at)}{item.updated_at !== item.created_at && `, refreshed ${formatWhen(item.updated_at)}`}</dd></div>
        {d.missing?.length > 0 && <div><dt>Not available</dt><dd className="muted">{d.missing.join(". ")}.</dd></div>}
      </dl>
    </div>
  );
}

/** Everything saved for one article, loaded when it's opened. */
export function ArticleDetails({ item, onChange }) {
  const [full, setFull] = useState(null);
  const [error, setError] = useState(null);
  const [tab, setTab] = useState("abstract");

  const load = useCallback(() => api.literature.get(item.id).then(setFull).catch((e) => setError(e.message)), [item.id]);
  useEffect(() => { load(); }, [load, item.updated_at]);

  const d = full?.details ?? {};
  const tabs = [
    ["abstract", "Abstract"],
    d.full_text?.length > 0 && ["full", "Full text"],
    d.figures?.length > 0 && ["figures", `Figures & tables (${d.figures.length})`],
    d.references?.length > 0 && ["refs", `References (${formatNumber(d.references.length)})`],
    d.citations?.length > 0 && ["cited", `Cited by (${formatNumber(d.cited_by_count ?? d.citations.length)})`],
    ["facts", "Details"],
  ].filter(Boolean);

  const sourceName = LIT_SOURCES[item.source]?.name ?? item.source;
  const openLinks = (d.links ?? []).filter((l) => l.open_access && l.kind !== "doi");
  const otherLinks = (d.links ?? []).filter((l) => !(l.open_access && l.kind !== "doi"));

  return (
    <>
      <div className="tabs" role="tablist" aria-label="Article sections">
        {tabs.map(([id, label]) => (
          <button key={id} type="button" role="tab" aria-selected={tab === id} className={`tab${tab === id ? " is-active" : ""}`} onClick={() => setTab(id)}>
            {label}
          </button>
        ))}
        {!full && !error && <span className="muted small tabs-loading"><Loader2 size={12} className="spin" aria-hidden="true" /> Loading full record…</span>}
      </div>

      <div role="tabpanel" className="tab-panel">
        {error && <p className="bad small">{error}</p>}
        {tab === "abstract" && (
          paras(item.abstract).length
            ? <div className="abstract">{paras(item.abstract).map((p, i) => <p key={i}>{p}</p>)}</div>
            : <p className="muted">{sourceName} has no abstract for this article.</p>
        )}
        {tab === "full" && <FullText sections={d.full_text} from={d.full_text_from || "PubMed Central"} />}
        {tab === "figures" && <Figures figures={d.figures} />}
        {tab === "refs" && <RefList refs={d.references} label="References of" articleTitle={item.title} onSaved={load} />}
        {tab === "cited" && (
          <>
            {d.cited_by_count > d.citations.length && (
              <p className="muted small">Showing the first {formatNumber(d.citations.length)} of {formatNumber(d.cited_by_count)} citing works.</p>
            )}
            <RefList refs={d.citations} numbered={false} label="Cited by" articleTitle={item.title} onSaved={load} />
          </>
        )}
        {tab === "facts" && <Facts item={full ?? item} d={d} />}
      </div>

      {full && <Refresh item={item} d={d} onRefreshed={(f) => { setFull(f); onChange?.(f); }} />}

      {(openLinks.length > 0 || otherLinks.length > 0) && (
        <div className="article-actions">
          {openLinks.map((l) => (
            <a key={l.url} className="btn btn-sm" href={l.url} target="_blank" rel="noreferrer">
              <FileText size={12} /> {l.label}<span className="sr-only"> (opens in a new tab)</span>
            </a>
          ))}
          {otherLinks.map((l) => (
            <a key={l.url} className="link small" href={l.url} target="_blank" rel="noreferrer">
              {l.label}{l.open_access === false ? " (may need a subscription)" : ""} <ExternalLink size={11} />
            </a>
          ))}
        </div>
      )}
    </>
  );
}
