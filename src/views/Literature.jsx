import { useCallback, useEffect, useRef, useState } from "react";
import { BookOpen, Bot, Check, ChevronDown, Copy, Download, ExternalLink, Layers, Plus, Search, Sparkles, Trash2, X } from "lucide-react";
import { api } from "../api.js";
import { Card, Empty, OfflineNotice } from "../components/ui.jsx";
import { MultiSelect } from "../components/MultiSelect.jsx";
import { ArticleDetails } from "../components/ArticleDetails.jsx";
import { LIT_SOURCES, SOURCE_IDS, formatAuthors, formatNumber, formatWhen } from "../literature.jsx";

const PAGE = 25;
const FILTER_KEYS = ["q", "source", "tag", "run", "sort"];
const LIST_KEYS = new Set(["source", "tag"]); // several allowed, repeated in the URL: ?tag=a&tag=b

function readFilters() {
  const p = new URLSearchParams(window.location.search);
  const out = { q: "", source: [], tag: [], run: "", sort: "added" };
  for (const k of FILTER_KEYS) {
    if (LIST_KEYS.has(k)) out[k] = [...new Set(p.getAll(k).filter(Boolean))];
    else if (p.get(k)) out[k] = p.get(k);
  }
  out.source = out.source.filter((s) => SOURCE_IDS.includes(s));
  return out;
}

// Keep the address bar in step with the filters so a filtered view can be bookmarked or shared.
function writeFilters(filters) {
  const p = new URLSearchParams();
  for (const k of FILTER_KEYS) {
    if (LIST_KEYS.has(k)) filters[k].forEach((v) => p.append(k, v));
    else if (filters[k] && !(k === "sort" && filters[k] === "added")) p.set(k, filters[k]);
  }
  const search = p.toString();
  window.history.replaceState(null, "", `/literature${search ? `?${search}` : ""}`);
}

function SourceBadge({ source }) {
  const meta = LIT_SOURCES[source];
  return (
    <span className="badge source-badge">
      <span className={`chip-dot ${meta?.color ?? ""}`} aria-hidden="true" />
      {meta?.short ?? source}
    </span>
  );
}

function CopyButton({ value, label }) {
  const [copied, setCopied] = useState(false);
  const copy = async () => {
    try {
      if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(value);
      else {
        const input = document.createElement("textarea");
        input.value = value;
        input.style.position = "fixed";
        input.style.opacity = "0";
        document.body.appendChild(input);
        input.select();
        document.execCommand("copy");
        input.remove();
      }
      setCopied(true);
      setTimeout(() => setCopied(false), 1600);
    } catch {
      setCopied(false);
    }
  };
  return <button type="button" className="btn btn-sm copy-button" onClick={copy} aria-label={`Copy ${label}`} title={copied ? "Copied" : `Copy ${label}`}>
    {copied ? <Check size={12} /> : <Copy size={12} />}<span className="sr-only">{copied ? "Copied" : `Copy ${label}`}</span>
  </button>;
}

function Identifiers({ item }) {
  const links = [
    item.pmid && ["PMID", item.pmid, `https://pubmed.ncbi.nlm.nih.gov/${item.pmid}/`],
    item.pmcid && ["", item.pmcid, `https://pmc.ncbi.nlm.nih.gov/articles/${item.pmcid}/`],
    item.doi && ["DOI", item.doi, `https://doi.org/${item.doi}`, true],
  ].filter(Boolean);
  if (!links.length) return null;
  return (
    <span className="ids">
      {links.map(([label, value, href, copyable]) => (
        <span key={value} className="identifier">
          <a className="link mono" href={href} target="_blank" rel="noreferrer">
            {label && `${label} `}{value}<span className="sr-only"> (opens in a new tab)</span>
          </a>
          {copyable && <CopyButton value={value} label="DOI" />}
        </span>
      ))}
    </span>
  );
}

function Tags({ item, onChange }) {
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const inputRef = useRef(null);

  useEffect(() => { if (adding) inputRef.current?.focus(); }, [adding]);

  const add = async (e) => {
    e.preventDefault();
    const clean = name.trim();
    if (!clean) { setAdding(false); return; }
    setBusy(true);
    setError(null);
    try {
      onChange(await api.literature.addTag(item.id, clean));
      setName("");
      setAdding(false);
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const remove = async (tag) => {
    try {
      onChange(await api.literature.removeTag(item.id, tag));
    } catch (err) {
      setError(err.message);
    }
  };

  return (
    <div className="tags">
      {item.tags.map((t) => (
        <span key={t.name} className={`tag${t.origin === "ai" ? " tag-ai" : ""}`} title={t.origin === "ai" ? "Added by AI" : "Added by hand"}>
          {t.origin === "ai" && <Sparkles size={11} aria-label="AI" />}
          {t.name}
          <button type="button" onClick={() => remove(t.name)} aria-label={`Remove tag ${t.name}`}><X size={11} /></button>
        </span>
      ))}
      {adding ? (
        <form className="tag-form" onSubmit={add}>
          <input
            ref={inputRef}
            className="input tag-input"
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Escape") { setAdding(false); setName(""); } }}
            onBlur={() => { if (!name.trim()) setAdding(false); }}
            placeholder="Tag name"
            maxLength={80}
            disabled={busy}
            aria-label="New tag"
          />
        </form>
      ) : (
        <button type="button" className="tag tag-add" onClick={() => setAdding(true)}>
          <Plus size={11} /> Tag
        </button>
      )}
      {error && <span className="bad small">{error}</span>}
    </div>
  );
}

function Article({ item, onChange, onDelete, deleting, link }) {
  const [open, setOpen] = useState(false);
  const [confirming, setConfirming] = useState(false);
  const meta = [item.journal, item.pub_date || item.year, item.volume && `${item.volume}${item.issue ? `(${item.issue})` : ""}`, item.pages]
    .filter(Boolean).join(" · ");
  const bodyId = `article-${item.id}`;

  return (
    <li className={`article${open ? " is-open" : ""}`}>
      <div className="article-head">
        <button type="button" className="article-title" onClick={() => setOpen(!open)} aria-expanded={open} aria-controls={bodyId}>
          <ChevronDown size={15} className="article-chevron" aria-hidden="true" />
          <span>{item.title || <span className="muted">Untitled</span>}</span>
        </button>
        {item.title && <div className="article-quick-actions"><CopyButton value={item.title} label="title" /></div>}
        <div className="article-delete">
          {confirming ? (
            <>
              <span className="muted small">Delete this article?</span>
              <button type="button" className="btn btn-sm btn-danger" onClick={() => onDelete(item.id)} disabled={deleting}>
                {deleting ? "Deleting…" : "Delete"}
              </button>
              <button type="button" className="btn btn-sm" onClick={() => setConfirming(false)} disabled={deleting}>Keep</button>
            </>
          ) : (
            <button type="button" className="icon-action" onClick={() => setConfirming(true)} title="Delete article">
              <Trash2 size={14} /><span className="sr-only">Delete “{item.title || "untitled article"}”</span>
            </button>
          )}
        </div>
      </div>
      <div className="article-meta">
        <SourceBadge source={item.source} />
        {item.open_access && <span className="badge badge-ok">Open access</span>}
        {item.has_full_text && <span className="badge badge-cyan">Full text</span>}
        {item.cited_by_count > 0 && <span className="badge">Cited by {formatNumber(item.cited_by_count)}</span>}
        {item.authors.length > 0 && <span>{formatAuthors(item.authors)}</span>}
        {meta && <span className="muted">{meta}</span>}
      </div>
      <Tags item={item} onChange={onChange} />

      {open && (
        <div className="article-body" id={bodyId}>
          <ArticleDetails item={item} onChange={({ details, ...rest }) => onChange(rest)} />
          <div className="article-actions">
            <Identifiers item={item} />
            <a className="btn btn-sm" {...link(`/chat?literature=${item.id}`)} title={`Chat with AI about “${item.title || "this paper"}”`}>
              <Bot size={13} /> Chat with AI
            </a>
            <a className="btn btn-sm" href={item.url} target="_blank" rel="noreferrer">
              Open on {LIT_SOURCES[item.source]?.short ?? item.source} <ExternalLink size={12} />
            </a>
          </div>
        </div>
      )}
    </li>
  );
}

// Header action that empties the whole library, behind an inline confirmation.
function DeleteAll({ total, onDeleted }) {
  const [confirming, setConfirming] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.literature.removeAll();
      setConfirming(false);
      onDeleted();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  if (!confirming) {
    return (
      <button type="button" className="btn btn-sm" onClick={() => setConfirming(true)}>
        <Trash2 size={13} /> Delete all
      </button>
    );
  }
  return (
    <span className="delete-all" role="group" aria-label="Confirm deleting the library">
      {error ? <span className="bad small" role="alert">{error}</span> : (
        <span className="muted small">Delete all {formatNumber(total)} article{total === 1 ? "" : "s"} and their tags? This can’t be undone.</span>
      )}
      <button type="button" className="btn btn-sm btn-danger" onClick={run} disabled={busy}>
        {busy ? "Deleting…" : "Delete all"}
      </button>
      <button type="button" className="btn btn-sm" onClick={() => { setConfirming(false); setError(null); }} disabled={busy}>Keep</button>
    </span>
  );
}

// "Fetch from a source ▾": one entry per literature integration, so new sources show up on their own.
function SourceMenu({ apiOnline, link }) {
  const [open, setOpen] = useState(false);
  const [sources, setSources] = useState(null);
  const wrapRef = useRef(null);
  const buttonRef = useRef(null);

  useEffect(() => {
    if (apiOnline === false) return;
    api.integrations()
      .then((all) => setSources(all.filter((i) => i.category === "literature" && i.implemented)))
      .catch(() => setSources([]));
  }, [apiOnline]);

  useEffect(() => {
    if (!open) return undefined;
    const onDown = (e) => { if (!wrapRef.current?.contains(e.target)) setOpen(false); };
    const onKey = (e) => {
      if (e.key === "Escape") { setOpen(false); buttonRef.current?.focus(); }
    };
    document.addEventListener("mousedown", onDown);
    document.addEventListener("keydown", onKey);
    wrapRef.current?.querySelector(".menu a")?.focus();
    return () => {
      document.removeEventListener("mousedown", onDown);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const list = sources ?? SOURCE_IDS.map((id) => ({ id, name: LIT_SOURCES[id].name }));

  return (
    <div className="menu-wrap" ref={wrapRef}>
      <button ref={buttonRef} type="button" className="btn" aria-haspopup="true" aria-expanded={open} onClick={() => setOpen(!open)}>
        <Download size={14} /> Fetch from a source <ChevronDown size={14} className="menu-caret" aria-hidden="true" />
      </button>
      {open && (
        <ul className="menu">
          {list.map((s) => (
            <li key={s.id}>
              <a {...link(`/literature/fetch/${s.id}`)} onClick={(e) => { setOpen(false); link(`/literature/fetch/${s.id}`).onClick(e); }}>
                <span className={`chip-dot ${LIT_SOURCES[s.id]?.color ?? "cyan"}`} aria-hidden="true" />
                {s.name}
              </a>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function Literature({ apiOnline, link }) {
  const [filters, setFilters] = useState(readFilters);
  const [query, setQuery] = useState(filters.q); // typed text, applied after a pause
  const [offset, setOffset] = useState(0);
  const [page, setPage] = useState(null);
  const [facets, setFacets] = useState(null);
  const [run, setRun] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);
  const [deletingId, setDeletingId] = useState(null);
  const [deleteError, setDeleteError] = useState(null);

  const update = useCallback((patch) => {
    setFilters((f) => ({ ...f, ...patch }));
    setOffset(0);
  }, []);

  useEffect(() => {
    const t = setTimeout(() => { if (query !== filters.q) update({ q: query }); }, 300);
    return () => clearTimeout(t);
  }, [query, filters.q, update]);

  useEffect(() => { writeFilters(filters); }, [filters]);

  const load = useCallback(() => {
    if (apiOnline === false) return;
    setLoading(true);
    const { q, source, tag, run: runId, sort } = filters;
    api.literature.list({ q, source, tag, run_id: runId, sort, limit: PAGE, offset })
      .then((p) => { setPage(p); setError(null); })
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, [apiOnline, filters, offset]);
  useEffect(load, [load]);

  const loadFacets = useCallback(() => {
    if (apiOnline === false) return;
    api.literature.facets().then(setFacets).catch(() => {});
  }, [apiOnline]);
  useEffect(loadFacets, [loadFacets]);

  useEffect(() => {
    if (!filters.run) { setRun(null); return; }
    api.literature.run(filters.run).then(setRun).catch(() => setRun(null));
  }, [filters.run]);

  const replaceItem = (next) => {
    setPage((p) => p && { ...p, items: p.items.map((i) => (i.id === next.id ? next : i)) });
    loadFacets();
  };

  const remove = async (id) => {
    setDeletingId(id);
    setDeleteError(null);
    try {
      await api.literature.remove(id);
      setPage((p) => p && { ...p, total: p.total - 1, items: p.items.filter((i) => i.id !== id) });
      // Emptied the last page: step back one; otherwise refill this page from the server.
      if (page.items.length === 1 && offset > 0) setOffset(Math.max(0, offset - PAGE));
      else load();
      loadFacets();
    } catch (err) {
      setDeleteError(`Couldn’t delete the article: ${err.message}`);
    } finally {
      setDeletingId(null);
    }
  };

  const libraryTotal = facets ? Object.values(facets.sources).reduce((a, b) => a + b, 0) : null;
  const filtered = Boolean(filters.q || filters.source.length || filters.tag.length || filters.run);
  const clearAll = () => { setQuery(""); update({ q: "", source: [], tag: [], run: "" }); };

  return (
    <>
      <div className="page-head">
        <p className="page-intro">
          Every article saved from PubMed, PubMed Central and Europe PMC. Each source keeps its own copy, so the same
          paper can appear once per source. Tags stay with an article when it’s refreshed.
        </p>
        <div className="page-actions">
          <SourceMenu apiOnline={apiOnline} link={link} />
          <a className="btn btn-primary" {...link("/literature/fetch")}><Layers size={14} /> Mass import</a>
        </div>
      </div>

      {apiOnline === false && <OfflineNotice />}

      <Card
        title="Library"
        description={page ? `${formatNumber(page.total)} ${filtered ? `of ${formatNumber(libraryTotal)} ` : ""}article${page.total === 1 ? "" : "s"}` : "Loading…"}
        actions={(filtered || libraryTotal > 0) && (
          <>
            {filtered && <button type="button" className="btn btn-sm" onClick={clearAll}>Clear filters</button>}
            {libraryTotal > 0 && <DeleteAll total={libraryTotal} onDeleted={() => { setOffset(0); load(); loadFacets(); }} />}
          </>
        )}
        flush
      >
        <div className="filters">
          <label className="search">
            <Search size={14} aria-hidden="true" />
            <span className="sr-only">Search the library</span>
            <input className="input" type="search" value={query} onChange={(e) => setQuery(e.target.value)} placeholder="Title, abstract, author, PMID or DOI" />
          </label>
          <MultiSelect
            label="Sources"
            allLabel="All sources"
            noun="sources"
            value={filters.source}
            onChange={(source) => update({ source })}
            options={SOURCE_IDS.map((id) => ({ value: id, label: LIT_SOURCES[id].name, dot: LIT_SOURCES[id].color, count: facets?.sources[id] ?? null }))}
          />
          <MultiSelect
            label="Tags"
            allLabel="All tags"
            noun="tags"
            value={filters.tag}
            onChange={(tag) => update({ tag })}
            options={(facets?.tags ?? []).map((t) => ({ value: t.name, label: t.name, count: t.count }))}
            emptyText="No tags yet"
          />
          <select className="input select" value={filters.sort} onChange={(e) => update({ sort: e.target.value })} aria-label="Sort">
            <option value="added">Recently saved</option>
            <option value="newest">Newest published</option>
            <option value="oldest">Oldest published</option>
            <option value="title">Title A–Z</option>
          </select>
        </div>

        {filters.run && (
          <div className="filter-note">
            <span>
              Showing one fetch{run ? <>: <strong>{run.term}</strong> from {LIT_SOURCES[run.source]?.name}, {formatWhen(run.started_at)}</> : ""}
            </span>
            <button type="button" className="link" onClick={() => update({ run: "" })}>Show all</button>
          </div>
        )}

        {error ? (
          <p className="card-body bad">{error}</p>
        ) : !page ? (
          <p className="card-body muted">{apiOnline === false ? "Unavailable while the backend is offline." : "Loading…"}</p>
        ) : page.total === 0 ? (
          filtered ? (
            <Empty icon={Search} title="No matching articles" action={<button type="button" className="btn" onClick={clearAll}>Clear filters</button>}>
              Try a different search, or remove a filter.
            </Empty>
          ) : (
            <Empty icon={BookOpen} title="The library is empty" action={<a className="btn btn-primary" {...link("/literature/fetch")}><Layers size={14} /> Mass import</a>}>
              Fetch articles from PubMed, PubMed Central or Europe PMC by searching for a term such as a gene symbol.
            </Empty>
          )
        ) : (
          <>
            <ul className={`articles${loading ? " is-loading" : ""}`}>
              {page.items.map((item) => (
                <Article key={item.id} item={item} onChange={replaceItem} onDelete={remove} deleting={deletingId === item.id} link={link} />
              ))}
            </ul>
            {deleteError && <p className="card-foot bad" role="alert">{deleteError}</p>}
            {page.total > PAGE && (
              <div className="card-foot pager">
                <p className="muted">{offset + 1}–{Math.min(offset + PAGE, page.total)} of {formatNumber(page.total)}</p>
                <button type="button" className="btn btn-sm" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - PAGE))}>Previous</button>
                <button type="button" className="btn btn-sm" disabled={offset + PAGE >= page.total} onClick={() => setOffset(offset + PAGE)}>Next</button>
              </div>
            )}
          </>
        )}
      </Card>
    </>
  );
}
