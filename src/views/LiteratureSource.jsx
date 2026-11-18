import { useCallback, useEffect, useState } from "react";
import { ExternalLink, Search } from "lucide-react";
import { api } from "../api.js";
import { Card, OfflineNotice, PageIntro } from "../components/ui.jsx";
import { LIMITS, Row, RunProgress, TagPicker, isSingleArticle, useFollowRuns } from "../components/FetchRuns.jsx";
import { BusyIcon, SearchResults, useSearchResults } from "../components/SearchResults.jsx";
import { LIT_SOURCES } from "../literature.jsx";

export function LiteratureSource({ source, apiOnline, link }) {
  const meta = LIT_SOURCES[source];
  const [form, setForm] = useState({ term: "", max_results: 5, sort: "relevance", tags: [] });
  const [tagNames, setTagNames] = useState([]);
  const [integration, setIntegration] = useState(null);
  const [error, setError] = useState(null);
  const [searching, setSearching] = useState(false);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);
  const found = useSearchResults();

  // New tags created by a fetch become suggestions for the next one.
  const loadTags = useCallback(() => {
    api.literature.facets().then((f) => setTagNames(f.tags.map((t) => t.name))).catch(() => {});
  }, []);

  // The progress panel follows the latest run started here, or one already running on arrival.
  const follow = useFollowRuns(loadTags);
  const active = follow.runs[0] ?? null;
  const { show } = follow;

  useEffect(() => {
    if (apiOnline === false) return;
    api.literature.runs({ source, limit: 1 }).then((list) => {
      if (list[0]?.status === "running") show([list[0]]);
    }).catch(() => {});
    loadTags();
    api.integrations().then((all) => {
      const found = all.find((i) => i.id === source) ?? null;
      setIntegration(found);
      if (found?.default_sort) setForm((f) => (found.sort_options.some((o) => o.id === f.sort) ? f : { ...f, sort: found.default_sort }));
    }).catch(() => {});
  }, [apiOnline, source, loadTags, show]);

  const body = () => ({ ...form, term: form.term.trim() });

  // Step 1: search and show the matches; nothing is saved yet.
  const search = async (e) => {
    e.preventDefault();
    setError(null);
    setSaveError(null);
    setSearching(true);
    try {
      found.setResults([await api.literature.preview(source, body())]);
    } catch (err) {
      setError(err.message);
      found.clear();
    } finally {
      setSearching(false);
    }
  };

  // Step 2: save the ticked ones.
  const save = async () => {
    setSaveError(null);
    setSaving(true);
    try {
      const run = await api.literature.startFetch(source, { ...body(), ids: [...found.picked[source]] });
      show([run]);
      found.clear();
    } catch (err) {
      setSaveError(err.message);
    } finally {
      setSaving(false);
    }
  };

  const busy = searching || saving;
  const sortOptions = integration?.sort_options ?? [];
  const keyFound = integration?.status === "key_found";

  return (
    <>
      <PageIntro>
        {meta.intro}{" "}
        <a className="link inline" href={integration?.docs_url ?? meta.docs} target="_blank" rel="noreferrer">
          API docs <ExternalLink size={12} />
        </a>
      </PageIntro>

      {apiOnline === false && <OfflineNotice />}

      <form onSubmit={search}>
        <Card
          title={<><span className={`chip-dot ${meta.color ?? "cyan"}`} aria-hidden="true" />Fetch from {meta.name}</>}
          description="Search first, then pick which of the matches to save."
          actions={<span className="badge badge-ok">Free{meta.ncbi ? "" : ", no key"}</span>}
          className="mb"
          flush
        >
          <Row id="lit-term" label="Search term" hint={meta.syntax}>
            <input
              id="lit-term"
              placeholder="A gene, disease or topic, e.g. BRCA1 or cystic fibrosis"
              className="input"
              value={form.term}
              onChange={(e) => setForm({ ...form, term: e.target.value })}
              aria-describedby="lit-term-hint"
              maxLength={500}
              required
              disabled={busy}
            />
          </Row>
          <Row id="lit-max" label="How many" hint="The most matches to show.">
            {isSingleArticle(form.term) ? (
              <select id="lit-max" className="input select" value="single" aria-describedby="lit-max-hint" disabled>
                <option value="single">Just this article</option>
              </select>
            ) : (
              <select id="lit-max" className="input select" value={form.max_results} onChange={(e) => setForm({ ...form, max_results: Number(e.target.value) })} aria-describedby="lit-max-hint" disabled={busy}>
                {LIMITS.map((n) => <option key={n} value={n}>{n} result{n === 1 ? "" : "s"}</option>)}
              </select>
            )}
          </Row>
          <Row id="lit-sort" label="Sort by" hint={`Decides which matches come first. These are the orders ${meta.name} supports.`}>
            <select id="lit-sort" className="input select" value={form.sort} onChange={(e) => setForm({ ...form, sort: e.target.value })} aria-describedby="lit-sort-hint" disabled={busy || !sortOptions.length}>
              {!sortOptions.length && <option value={form.sort}>Loading…</option>}
              {sortOptions.map((o) => <option key={o.id} value={o.id}>{o.label}</option>)}
            </select>
          </Row>
          <Row id="lit-tags" label="Tags" hint="Added to every article you save, including ones already in the library, so you can filter by them later.">
            <TagPicker id="lit-tags" value={form.tags} onChange={(tags) => setForm({ ...form, tags })} suggestions={tagNames} disabled={busy} describedBy="lit-tags-hint" />
          </Row>
          {meta.ncbi && (
            <Row id="lit-key" label="NCBI API key">
              <p className={keyFound ? "ok" : "muted"}>
                {keyFound
                  ? "NCBI_API_KEY found. Up to 10 requests a second."
                  : <>Optional. Without one NCBI allows 3 requests a second, shared with {source === "pubmed" ? "PubMed Central" : "PubMed"}. Add a free key as <code>NCBI_API_KEY</code> in <code>backend/.env</code>.</>}
              </p>
            </Row>
          )}
          <div className="card-foot">
            <p className={error ? "bad" : "muted"} role="status">
              {error ?? "Saving takes a few seconds per article, since Helix also collects the full text, references and citations."}
            </p>
            <button type="submit" className="btn btn-primary btn-sm" disabled={busy || !form.term.trim() || apiOnline === false}>
              <BusyIcon busy={searching} icon={Search} /> {searching ? "Searching…" : "Search"}
            </button>
          </div>
          {active && <RunProgress run={active} onCancel={follow.cancel} cancelling={follow.cancelling.has(active.id)} link={link} />}
        </Card>
      </form>

      <SearchResults
        state={found}
        searching={searching}
        searchingSources={[meta.name]}
        onSave={save}
        onDiscard={() => { found.clear(); setSaveError(null); }}
        saving={saving}
        error={saveError}
        blocked={follow.anyRunning ? `${meta.name} is still saving. You can save these once it finishes.` : null}
      />
    </>
  );
}
