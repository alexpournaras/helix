import { useCallback, useEffect, useMemo, useState } from "react";
import { BookOpen, Search } from "lucide-react";
import { api } from "../api.js";
import { Card, Empty, OfflineNotice, PageIntro } from "../components/ui.jsx";
import { LIMITS, Row, RunProgress, TagPicker, isSingleArticle, useFollowRuns } from "../components/FetchRuns.jsx";
import { BusyIcon, SearchResults, useSearchResults } from "../components/SearchResults.jsx";
import { LIT_SOURCES, formatNumber, formatWhen } from "../literature.jsx";

// Every integration in the "literature" category is listed, so new sources appear on their own.
export function LiteratureFetch({ apiOnline, link }) {
  const [sources, setSources] = useState(null);
  const [selected, setSelected] = useState(() => new Set());
  const [form, setForm] = useState({ term: "", max_results: 5, sort: "relevance", tags: [] });
  const [counts, setCounts] = useState({});
  const [tagNames, setTagNames] = useState([]);
  const [lastRun, setLastRun] = useState({});
  const [error, setError] = useState(null);
  const [searching, setSearching] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);
  const found = useSearchResults();

  const loadStats = useCallback(() => {
    api.literature.facets().then((f) => { setCounts(f.sources); setTagNames(f.tags.map((t) => t.name)); }).catch(() => {});
    return api.literature.runs({ limit: 50 }).then((runs) => {
      const latest = {};
      for (const r of runs) latest[r.source] ??= r;
      setLastRun(latest);
      return runs;
    });
  }, []);

  const follow = useFollowRuns(() => loadStats().catch(() => {}));
  const { show } = follow;

  useEffect(() => {
    if (apiOnline === false) return;
    api.integrations()
      .then((all) => {
        const lit = all.filter((i) => i.category === "literature");
        setSources(lit);
        setSelected(new Set(lit.filter((i) => i.implemented).map((i) => i.id)));
      })
      .catch((e) => setError(e.message));
    // Pick up fetches still running from an earlier visit.
    loadStats().then((runs) => {
      const running = runs.filter((r) => r.status === "running");
      if (running.length) show(running);
    }).catch(() => {});
  }, [apiOnline, loadStats, show]);

  const chosen = useMemo(() => (sources ?? []).filter((s) => selected.has(s.id)), [sources, selected]);

  // Only the orders every chosen source supports, in the first source's order and wording.
  const sortOptions = useMemo(() => {
    if (!chosen.length) return [];
    return chosen[0].sort_options.filter((o) => chosen.every((s) => s.sort_options.some((x) => x.id === o.id)));
  }, [chosen]);

  useEffect(() => {
    if (sortOptions.length && !sortOptions.some((o) => o.id === form.sort)) setForm((f) => ({ ...f, sort: sortOptions[0].id }));
  }, [sortOptions, form.sort]);

  const runningSources = new Set(follow.runs.filter((r) => r.status === "running").map((r) => r.source));

  const toggle = (id) => setSelected((s) => {
    const next = new Set(s);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  const body = () => ({ ...form, term: form.term.trim() });

  // Step 1: search every chosen source at once and show the matches; nothing is saved yet.
  const search = async (e) => {
    e.preventDefault();
    setSearching(true);
    setSaveError(null);
    const settled = await Promise.allSettled(chosen.map((s) => api.literature.preview(s.id, body())));
    found.setResults(settled.map((r, i) => (r.status === "fulfilled"
      ? r.value
      : { source: chosen[i].id, total: 0, items: [], error: r.reason.message })));
    setSearching(false);
  };

  // Step 2: save the ticked ones, one fetch per source.
  const save = async () => {
    setSaving(true);
    setSaveError(null);
    const todo = found.results.filter((r) => found.picked[r.source]?.size);
    const settled = await Promise.allSettled(todo.map((r) => api.literature.startFetch(r.source, { ...body(), ids: [...found.picked[r.source]] })));
    const started = settled.filter((r) => r.status === "fulfilled").map((r) => r.value);
    const failed = settled.flatMap((r, i) => (r.status === "rejected" ? [`${LIT_SOURCES[todo[i].source]?.name}: ${r.reason.message}`] : []));
    // New runs first, then anything from before that is still going.
    show([...started, ...follow.runs.filter((r) => r.status === "running" && !started.some((s) => s.source === r.source))]);
    if (failed.length) setSaveError(failed.join(" "));
    else found.clear();
    setSaving(false);
    loadStats().catch(() => {});
  };

  const single = isSingleArticle(form.term);
  const busy = searching || saving;
  const canSearch = chosen.length > 0 && form.term.trim() && !busy && apiOnline !== false;
  const footText = !chosen.length
    ? "Choose at least one source."
    : chosen.length > 1
      ? "Searches every source at once. You pick what to save next."
      : "You pick what to save next.";

  // Saving is one run per source, so a source that's still saving has to finish first.
  const waiting = (found.results ?? []).filter((r) => found.picked[r.source]?.size && runningSources.has(r.source)).map((r) => LIT_SOURCES[r.source]?.name);
  const blockedText = waiting.length
    ? `${waiting.length > 1 ? `${waiting.slice(0, -1).join(", ")} and ${waiting.at(-1)} are` : `${waiting[0]} is`} still saving. You can save these once ${waiting.length > 1 ? "they finish" : "it finishes"}.`
    : null;

  return (
    <>
      <PageIntro>
        Search several sources for the same term, such as a gene symbol, or paste a link to one article. Review what each
        source returns and save only the ones you want. For a single source with all its sort orders, use Fetch from a
        source on the Literature page.
      </PageIntro>

      {(apiOnline === false || error) && <OfflineNotice />}

      {!sources ? (
        <p className="muted">{apiOnline === false || error ? "Unavailable while the backend is offline." : "Loading…"}</p>
      ) : sources.length === 0 ? (
        <Empty icon={BookOpen} title="No literature sources">None are set up yet.</Empty>
      ) : (
        <form onSubmit={search}>
          <Card title="Mass import" description="One search, sent to several sources at once. Review the matches, then save the ones you want." flush>
            <Row id="multi-term" label="Search term" hint="Sent to every source exactly as typed. Plain words work everywhere; field tags such as [tiab] only work on PubMed and PMC. To add one article, paste its link, PMCID, PMID: … or DOI.">
              <input
                id="multi-term"
                placeholder="A gene, disease or topic, e.g. BRCA1 or cystic fibrosis"
                className="input"
                value={form.term}
                onChange={(e) => setForm({ ...form, term: e.target.value })}
                aria-describedby="multi-term-hint"
                maxLength={500}
                required
                disabled={busy}
              />
            </Row>
            <Row id="multi-sources" as="span" label="Sources" hint="Open a source for its own sort orders, such as Most cited on Europe PMC.">
              <ul className="source-picks" role="group" aria-labelledby="multi-sources" aria-describedby="multi-sources-hint">
                {sources.map((s) => {
                  const meta = LIT_SOURCES[s.id];
                  const last = lastRun[s.id];
                  return (
                    <li key={s.id} className={`source-pick${s.implemented ? "" : " is-disabled"}`}>
                      <label className="check">
                        <input type="checkbox" checked={selected.has(s.id)} onChange={() => toggle(s.id)} disabled={!s.implemented || busy} />
                        <span className={`chip-dot ${meta?.color ?? "cyan"}`} aria-hidden="true" />
                        <span className="source-pick-name">{s.name}</span>
                      </label>
                      <span className="source-pick-meta">
                        {s.implemented
                          ? <>{formatNumber(counts[s.id] ?? 0)} saved{last && <>, last “{last.term}” {formatWhen(last.started_at)}</>}</>
                          : "Coming soon"}
                      </span>
                      {s.implemented && <a className="link small" {...link(`/literature/fetch/${s.id}`)}>Options<span className="sr-only"> for {s.name}</span></a>}
                    </li>
                  );
                })}
              </ul>
            </Row>
            <Row id="multi-max" label="How many" hint="The most matches to show from each source.">
              {single ? (
                <select id="multi-max" className="input select" value="single" aria-describedby="multi-max-hint" disabled>
                  <option value="single">Just this article</option>
                </select>
              ) : (
                <select id="multi-max" className="input select" value={form.max_results} onChange={(e) => setForm({ ...form, max_results: Number(e.target.value) })} aria-describedby="multi-max-hint" disabled={busy}>
                  {LIMITS.map((n) => <option key={n} value={n}>{n === 1 ? "1 result" : `${n} results`} per source</option>)}
                </select>
              )}
            </Row>
            <Row id="multi-sort" label="Sort by" hint={chosen.length > 1 ? "Only the orders every chosen source supports. Decides which matches come first." : "Decides which matches come first."}>
              <select id="multi-sort" className="input select" value={form.sort} onChange={(e) => setForm({ ...form, sort: e.target.value })} aria-describedby="multi-sort-hint" disabled={busy || !sortOptions.length}>
                {!sortOptions.length && <option value="">Choose a source first</option>}
                {sortOptions.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
              </select>
            </Row>
            <Row id="multi-tags" label="Tags" hint="Added to every article you save, including ones already in the library, so you can filter by them later.">
              <TagPicker id="multi-tags" value={form.tags} onChange={(tags) => setForm({ ...form, tags })} suggestions={tagNames} disabled={busy} describedBy="multi-tags-hint" />
            </Row>
            <div className="card-foot">
              <p className="muted" role="status">{footText}</p>
              <button type="submit" className="btn btn-primary btn-sm" disabled={!canSearch}>
                <BusyIcon busy={searching} icon={Search} />
                {searching ? "Searching…" : chosen.length === 1 ? `Search ${chosen[0].name}` : `Search ${chosen.length} sources`}
              </button>
            </div>
            {follow.runs.map((run) => (
              <RunProgress key={run.id} run={run} onCancel={follow.cancel} cancelling={follow.cancelling.has(run.id)} link={link} showSource />
            ))}
          </Card>
        </form>
      )}

      <SearchResults
        state={found}
        showSource
        searching={searching}
        searchingSources={chosen.map((s) => s.name)}
        onSave={save}
        onDiscard={() => { found.clear(); setSaveError(null); }}
        saving={saving}
        error={saveError}
        blocked={blockedText}
      />
    </>
  );
}
