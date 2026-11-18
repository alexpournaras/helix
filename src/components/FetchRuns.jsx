import { useCallback, useEffect, useRef, useState } from "react";
import { Loader2, Square, Tag, X } from "lucide-react";
import { api } from "../api.js";
import { LIT_SOURCES, RUN_STATUS, formatNumber } from "../literature.jsx";

export const LIMITS = [1, 5, 10, 20, 50, 100, 200, 500];

// Mirrors text.identifier() in the backend: a link to, or an ID of, one article.
const SINGLE = [
  /^(https?:\/\/)?(www\.)?(pmc\.ncbi\.nlm\.nih\.gov|ncbi\.nlm\.nih\.gov\/pmc)\/articles\/PMC\d+/i,
  /^(https?:\/\/)?(www\.)?(pubmed\.ncbi\.nlm\.nih\.gov|ncbi\.nlm\.nih\.gov\/pubmed)\/\d+/i,
  /^(https?:\/\/)?(www\.)?europepmc\.org\/(article|abstract)\/(PMC|MED|PPR)\/\w+/i,
  /^(https?:\/\/)?(dx\.)?doi\.org\/10\.\d{4,9}\/\S+$/i,
  /^(PMCID:\s*)?PMC\d+$/i,
  /^PMID:?\s*\d+$/i,
  /^(doi:\s*)?10\.\d{4,9}\/\S+$/i,
];

/** True when the search term points at one specific article rather than a search. */
export const isSingleArticle = (term) => SINGLE.some((re) => re.test(term.trim()));
const POLL_MS = 1000;

export function Row({ id, label, hint, children, as: Label = "label" }) {
  return (
    <div className="form-row">
      <div className="form-label">
        {Label === "label" ? <label htmlFor={id}>{label}</label> : <span id={id} className="form-label-text">{label}</span>}
        {hint && <p id={`${id}-hint`}>{hint}</p>}
      </div>
      <div className="form-control">{children}</div>
    </div>
  );
}

const normal = (s) => s.split(/\s+/).filter(Boolean).join(" ");

/**
 * Pick existing tags or type new ones; Enter, comma or leaving the field adds what was typed.
 * `suggestions` are the library's existing tag names, offered as you type.
 */
export function TagPicker({ id, value, onChange, suggestions = [], disabled, describedBy }) {
  const [text, setText] = useState("");
  const listId = `${id}-suggestions`;
  const has = (name) => value.some((t) => t.toLowerCase() === name.toLowerCase());

  const add = (raw) => {
    const names = raw.split(",").map(normal).filter((n) => n && n.length <= 80);
    const next = [...value];
    for (const n of names) if (!next.some((t) => t.toLowerCase() === n.toLowerCase())) next.push(n);
    if (next.length !== value.length) onChange(next.slice(0, 20));
    setText("");
  };

  const onKeyDown = (e) => {
    if (e.key === "Enter" || e.key === ",") {
      if (!text.trim()) { if (e.key === ",") e.preventDefault(); return; } // Enter on an empty field submits the form
      e.preventDefault();
      add(text);
    } else if (e.key === "Backspace" && !text && value.length) {
      onChange(value.slice(0, -1));
    }
  };

  // Picking from the suggestion list fills the field without a keypress (no inputType): add it straight away.
  const onInput = (e) => {
    const v = e.target.value;
    if (!e.nativeEvent.inputType && suggestions.some((s) => s.toLowerCase() === v.toLowerCase())) add(v);
    else setText(v);
  };

  return (
    <div className={`tag-picker${disabled ? " is-disabled" : ""}`}>
      {value.map((t) => (
        <span key={t} className="tag">
          {t}
          <button type="button" onClick={() => onChange(value.filter((x) => x !== t))} disabled={disabled} aria-label={`Remove tag ${t}`}>
            <X size={11} />
          </button>
        </span>
      ))}
      <input
        id={id}
        className="tag-picker-input"
        value={text}
        onChange={onInput}
        onKeyDown={onKeyDown}
        onBlur={() => text.trim() && add(text)}
        list={listId}
        placeholder={value.length ? "Add another" : "Choose or create tags"}
        maxLength={80}
        disabled={disabled || value.length >= 20}
        aria-describedby={describedBy}
        autoComplete="off"
      />
      <datalist id={listId}>
        {suggestions.filter((s) => !has(s)).map((s) => <option key={s} value={s} />)}
      </datalist>
    </div>
  );
}

/** The tags a run adds to everything it saves, as small chips. */
export function RunTags({ tags }) {
  if (!tags?.length) return null;
  return (
    <span className="run-tags">
      <Tag size={11} aria-label="Tags" />
      {tags.map((t) => <span key={t} className="tag tag-static">{t}</span>)}
    </span>
  );
}

export function RunBadge({ status }) {
  const [tone, label] = RUN_STATUS[status] ?? ["", status];
  return <span className={`badge ${tone}`}>{label}</span>;
}

/** Live progress for one fetch run. `showSource` labels it when several sources run side by side. */
export function RunProgress({ run, onCancel, cancelling, link, showSource = false }) {
  const running = run.status === "running";
  const done = run.fetched + run.failed;
  const pct = run.queued ? Math.round((done / run.queued) * 100) : 0;
  let headline;
  if (running && run.queued === 0 && run.total == null) headline = `Searching for “${run.term}”…`;
  else if (running) headline = `Fetching article ${Math.min(done + 1, run.queued)} of ${run.queued}`;
  else if (run.status === "done") headline = `Saved ${run.fetched} of ${run.queued} article${run.queued === 1 ? "" : "s"}`;
  else headline = run.status === "cancelled" ? `Stopped after ${run.fetched} article${run.fetched === 1 ? "" : "s"}` : "The fetch stopped";
  const meta = LIT_SOURCES[run.source];

  return (
    <div className={`run${running ? " is-running" : ""}`} aria-live="polite">
      <div className="run-head">
        {showSource && (
          <span className="run-source"><span className={`chip-dot ${meta?.color ?? ""}`} aria-hidden="true" />{meta?.name ?? run.source}</span>
        )}
        <RunBadge status={run.status} />
        <span className="run-title">
          {running && <Loader2 size={13} className="spin" aria-hidden="true" />}
          {headline}
        </span>
        {running ? (
          <button type="button" className="btn btn-sm" onClick={() => onCancel(run.id)} disabled={cancelling}>
            <Square size={12} /> {cancelling ? "Stopping…" : "Stop"}
          </button>
        ) : run.fetched > 0 ? (
          <a className="btn btn-sm" {...link(`/literature?run=${run.id}`)}>View in library</a>
        ) : null}
      </div>
      <div className={`progress run-bar${running && !run.queued ? " is-indeterminate" : ""}`} aria-hidden="true">
        <i style={{ width: running && !run.queued ? undefined : `${pct}%` }} />
      </div>
      <dl className="run-stats">
        <div><dt>Matches</dt><dd>{formatNumber(run.total)}</dd></div>
        <div><dt>New</dt><dd>{run.new}</dd></div>
        <div><dt>Updated</dt><dd>{run.updated}</dd></div>
        <div><dt>Failed</dt><dd className={run.failed ? "bad" : ""}>{run.failed}</dd></div>
      </dl>
      {run.tags?.length > 0 && <p className="muted small">Tagging every article with <RunTags tags={run.tags} /></p>}
      {running && run.current && <p className="muted small">Now fetching <span className="mono">{run.current}</span></p>}
      {run.error && <p className={`small ${run.status === "failed" || run.status === "interrupted" ? "bad" : "muted"}`}>{run.error}</p>}
      {run.status === "interrupted" && <p className="muted small">The backend restarted while this was running. Articles saved before that are kept.</p>}
    </div>
  );
}

/**
 * Keeps a list of fetch runs up to date: polls the running ones every second and calls
 * onFinished(run) as each one stops. `show(list)` replaces the list being followed.
 */
export function useFollowRuns(onFinished) {
  const [runs, setRuns] = useState([]);
  const [cancelling, setCancelling] = useState(() => new Set());
  const finishedRef = useRef(onFinished);
  finishedRef.current = onFinished;

  const show = useCallback((list) => setRuns(list), []);

  const runningKey = runs.filter((r) => r.status === "running").map((r) => r.id).join(",");
  useEffect(() => {
    if (!runningKey) return undefined;
    const ids = runningKey.split(",").map(Number);
    let alive = true;
    const timer = setInterval(async () => {
      const fresh = await Promise.all(ids.map((id) => api.literature.run(id).catch(() => null)));
      if (!alive) return;
      setRuns((rs) => rs.map((r) => fresh.find((f) => f?.id === r.id) ?? r));
      for (const run of fresh) {
        if (run && run.status !== "running") {
          setCancelling((s) => { const n = new Set(s); n.delete(run.id); return n; });
          finishedRef.current?.(run);
        }
      }
    }, POLL_MS);
    return () => { alive = false; clearInterval(timer); };
  }, [runningKey]);

  const cancel = useCallback(async (id) => {
    setCancelling((s) => new Set(s).add(id));
    try {
      const run = await api.literature.cancelRun(id);
      setRuns((rs) => rs.map((r) => (r.id === id ? run : r)));
    } catch {
      // it probably finished on its own; the next poll shows the final state
      setCancelling((s) => { const n = new Set(s); n.delete(id); return n; });
    }
  }, []);

  return { runs, show, cancel, cancelling, anyRunning: Boolean(runningKey) };
}
