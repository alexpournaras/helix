import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { Check, ExternalLink, Loader2, Minus, Save } from "lucide-react";
import { Card } from "./ui.jsx";
import { LIT_SOURCES, formatAuthors, formatNumber } from "../literature.jsx";

/**
 * Search results waiting to be reviewed. `results` is one entry per source:
 * { source, total, items: [{ source_id, title, authors, journal, year, url, saved }], error? }.
 * New articles start ticked; ones already in the library start unticked.
 */
export function useSearchResults() {
  const [results, setResultsState] = useState(null);
  const [picked, setPicked] = useState({});

  const setResults = useCallback((list) => {
    setResultsState(list);
    setPicked(list ? Object.fromEntries(list.map((r) => [r.source, new Set((r.items ?? []).filter((i) => !i.saved).map((i) => i.source_id))])) : {});
  }, []);

  const toggle = useCallback((source, id) => setPicked((p) => {
    const next = new Set(p[source]);
    if (next.has(id)) next.delete(id); else next.add(id);
    return { ...p, [source]: next };
  }), []);

  const setAll = useCallback((source, ids) => setPicked((p) => ({ ...p, [source]: new Set(ids) })), []);

  const count = Object.values(picked).reduce((n, s) => n + s.size, 0);
  return { results, picked, setResults, toggle, setAll, count, clear: () => setResults(null) };
}

function BoxIcon({ state }) {
  if (state === "all") return <Check size={11} strokeWidth={3} />;
  if (state === "some") return <Minus size={11} strokeWidth={3} />;
  return null;
}

function SelectAll({ ids, chosen, onSet, label }) {
  const ref = useRef(null);
  const n = ids.filter((id) => chosen.has(id)).length;
  const state = n === 0 ? "none" : n === ids.length ? "all" : "some";
  useEffect(() => { if (ref.current) ref.current.indeterminate = state === "some"; }, [state]);
  return (
    <label className="pick">
      <input ref={ref} type="checkbox" className="sr-only" checked={state === "all"} onChange={() => onSet(state === "all" ? [] : ids)} disabled={!ids.length} />
      <span className={`multi-box is-${state}`} aria-hidden="true"><BoxIcon state={state} /></span>
      <span className="sr-only">{label}</span>
    </label>
  );
}

// The abstract is fetched only when asked for, so searching stays quick.
function Abstract({ source, id }) {
  const [state, setState] = useState({ open: false, text: null, error: null, loading: false });
  const toggle = async () => {
    if (state.open) { setState((s) => ({ ...s, open: false })); return; }
    if (state.text != null) { setState((s) => ({ ...s, open: true })); return; }
    setState((s) => ({ ...s, open: true, loading: true }));
    try {
      const { abstract } = await api.literature.peek(source, id);
      setState({ open: true, text: abstract, error: null, loading: false });
    } catch (err) {
      setState({ open: true, text: null, error: err.message, loading: false });
    }
  };
  return (
    <>
      <button type="button" className="link small" onClick={toggle} aria-expanded={state.open}>
        {state.open ? "Hide abstract" : "Abstract"}
      </button>
      {state.open && (
        <div className="result-abstract">
          {state.loading ? <span className="muted small"><Loader2 size={12} className="spin" aria-hidden="true" /> Loading…</span>
            : state.error ? <span className="bad small">{state.error}</span>
              : state.text ? state.text.split(/\n{2,}/).map((p, i) => <p key={i}>{p}</p>)
                : <span className="muted small">No abstract.</span>}
        </div>
      )}
    </>
  );
}

function SourceResults({ result, chosen, onToggle, onSetAll, showSource }) {
  const meta = LIT_SOURCES[result.source];
  const ids = result.items?.map((i) => i.source_id) ?? [];
  const savedCount = result.items?.filter((i) => i.saved).length ?? 0;
  const newIds = result.items?.filter((i) => !i.saved).map((i) => i.source_id) ?? [];

  return (
    <section className="results-group">
      <header className="results-head">
        {ids.length > 0 && <SelectAll ids={ids} chosen={chosen} onSet={(v) => onSetAll(result.source, v)} label={`Select all from ${meta?.name ?? result.source}`} />}
        {showSource && (
          <span className="run-source"><span className={`chip-dot ${meta?.color ?? ""}`} aria-hidden="true" />{meta?.name ?? result.source}</span>
        )}
        <span className="muted small results-count">
          {result.error ? <span className="bad">{result.error}</span>
            : !ids.length ? "No matches"
              : <>
                {ids.length < result.total ? `Top ${formatNumber(ids.length)} of ${formatNumber(result.total)} matches` : `${formatNumber(ids.length)} match${ids.length === 1 ? "" : "es"}`}
                {savedCount > 0 && `, ${savedCount} already saved`}
              </>}
        </span>
        {savedCount > 0 && newIds.length > 0 && (
          <button type="button" className="link small" onClick={() => onSetAll(result.source, newIds)}>Only new</button>
        )}
      </header>
      {ids.length > 0 && (
        <ul className="results">
          {result.items.map((item) => {
            const on = chosen.has(item.source_id);
            const meta2 = [item.journal, item.year].filter(Boolean).join(" · ");
            return (
              <li key={item.source_id} className={`result${on ? " is-picked" : ""}`}>
                <label className="pick">
                  <input type="checkbox" className="sr-only" checked={on} onChange={() => onToggle(result.source, item.source_id)} />
                  <span className={`multi-box is-${on ? "all" : "none"}`} aria-hidden="true"><BoxIcon state={on ? "all" : "none"} /></span>
                  <span className="sr-only">Save “{item.title || item.source_id}”</span>
                </label>
                <div className="result-body">
                  <a className="result-title" href={item.url} target="_blank" rel="noreferrer">
                    {item.title || <span className="muted">Untitled</span>}
                    <ExternalLink size={11} aria-hidden="true" /><span className="sr-only"> (opens in a new tab)</span>
                  </a>
                  <div className="result-meta">
                    {item.saved && <span className="badge badge-ok">In library</span>}
                    {item.authors.length > 0 && <span>{formatAuthors(item.authors)}</span>}
                    {meta2 && <span className="muted">{meta2}</span>}
                    <Abstract source={result.source} id={item.source_id} />
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

/** Placeholder rows that shimmer while a search is out. */
function Searching({ sources }) {
  return (
    <Card title="Results" description={`Searching ${sources.length > 1 ? `${sources.length} sources` : sources[0] ?? ""}…`} className="mt" flush>
      <div className="searching" role="status" aria-live="polite">
        <span className="sr-only">Searching…</span>
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="result skeleton" aria-hidden="true" style={{ animationDelay: `${i * 90}ms` }}>
            <span className="skel skel-box" />
            <div className="result-body">
              <span className="skel skel-title" style={{ width: `${[78, 64, 86, 58][i]}%` }} />
              <span className="skel skel-meta" style={{ width: `${[42, 36, 48, 30][i]}%` }} />
            </div>
          </div>
        ))}
      </div>
    </Card>
  );
}

/** A button icon that turns into a spinner while `busy`. */
export function BusyIcon({ busy, icon: Icon, size = 13 }) {
  return busy ? <Loader2 size={size} className="spin" aria-hidden="true" /> : <Icon size={size} aria-hidden="true" />;
}

/** The review step between searching and saving. */
export function SearchResults({ state, showSource = false, onSave, onDiscard, saving, error, blocked, searching = false, searchingSources = [] }) {
  const { results, picked, toggle, setAll, count } = state;
  if (searching) return <Searching sources={searchingSources} />;
  if (!results) return null;
  const anyHits = results.some((r) => r.items?.length);
  const someSaved = results.some((r) => picked[r.source]?.size && r.items?.some((i) => i.saved && picked[r.source].has(i.source_id)));

  return (
    <Card
      title="Results"
      description={anyHits ? "Tick the articles to keep, then save them to the library." : "Nothing matched. Try a broader term."}
      className="mt"
      flush
    >
      {results.map((r) => (
        <SourceResults key={r.source} result={r} chosen={picked[r.source] ?? new Set()} onToggle={toggle} onSetAll={setAll} showSource={showSource} />
      ))}
      <div className="card-foot">
        <p className={error ? "bad" : blocked ? "foot-wait" : "muted"} role="status">
          {error ?? blocked ?? (count === 0
            ? "Nothing selected."
            : `${count} selected.${someSaved ? " Ones already saved are refreshed, and keep their tags." : ""}`)}
        </p>
        <button type="button" className="btn btn-sm" onClick={onDiscard} disabled={saving}>Discard</button>
        {anyHits && (
          <button type="button" className="btn btn-primary btn-sm" onClick={onSave} disabled={!count || saving || Boolean(blocked)}>
            <BusyIcon busy={saving} icon={Save} /> {saving ? "Saving…" : count ? `Save ${count} to library` : "Save to library"}
          </button>
        )}
      </div>
    </Card>
  );
}
