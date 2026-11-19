import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowLeft, BookOpen, FileText, Pin, Plus, Search, Tag, Trash2, X } from "lucide-react";
import { api } from "../api.js";
import { Empty, PageIntro } from "../components/ui.jsx";

const blank = { title: "", content: "", tags: [], pinned: false, literature_ids: [] };
const copyNote = (note) => {
  const source = note ?? blank;
  return {
    title: source.title,
    content: source.content,
    tags: [...(source.tags ?? [])],
    pinned: Boolean(source.pinned),
    literature_ids: [...(source.literature_ids ?? [])],
  };
};

function formatUpdated(value) {
  return new Intl.DateTimeFormat(undefined, { dateStyle: "medium", timeStyle: "short" }).format(new Date(value));
}

function excerpt(content) {
  return content.trim().replace(/\s+/g, " ") || "No text yet";
}

function TagEditor({ tags, onChange, disabled }) {
  const [value, setValue] = useState("");
  const add = () => {
    const tag = value.trim();
    if (!tag || tags.some((item) => item.toLowerCase() === tag.toLowerCase()) || tags.length >= 20) return;
    onChange([...tags, tag]);
    setValue("");
  };
  return <div className="note-tag-editor">
    {tags.map((tag) => <span className="note-tag" key={tag}>{tag}<button type="button" onClick={() => onChange(tags.filter((item) => item !== tag))} aria-label={`Remove ${tag}`} disabled={disabled}><X size={11} /></button></span>)}
    <input value={value} maxLength={40} disabled={disabled || tags.length >= 20} placeholder={tags.length ? "Add tag" : "Add tags"} aria-label="Add tag"
      onChange={(event) => setValue(event.target.value)} onBlur={add}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === ",") { event.preventDefault(); add(); }
        if (event.key === "Backspace" && !value && tags.length) onChange(tags.slice(0, -1));
      }} />
  </div>;
}

function PaperEditor({ ids, papers, onChange, disabled }) {
  const selected = ids.map((id) => papers.find((paper) => paper.id === id) ?? { id, title: `Literature #${id}` });
  const available = papers.filter((paper) => !ids.includes(paper.id));
  return <div className="note-paper-editor">
    <div className="note-paper-heading"><BookOpen size={14} /><span>Linked papers</span></div>
    {selected.length > 0 && <div className="note-linked-papers">{selected.map((paper) => <div key={paper.id}>
      <a href={`/literature?q=${encodeURIComponent(paper.title)}`} target="_blank" rel="noreferrer" title="Open in Literature">{paper.title}</a>
      <button type="button" onClick={() => onChange(ids.filter((id) => id !== paper.id))} disabled={disabled} aria-label={`Unlink ${paper.title}`}><X size={12} /></button>
    </div>)}</div>}
    {available.length > 0 ? <select className="input select" value="" disabled={disabled || ids.length >= 50}
      onChange={(event) => event.target.value && onChange([...ids, Number(event.target.value)])} aria-label="Link a paper">
      <option value="">Link a paper…</option>
      {available.map((paper) => <option value={paper.id} key={paper.id}>{paper.title}</option>)}
    </select> : <span className="muted small">{papers.length ? "All available papers are linked." : "Save papers to Literature to link them here."}</span>}
  </div>;
}

export function Notes({ noteId, navigate }) {
  const [notes, setNotes] = useState([]);
  const [papers, setPapers] = useState([]);
  const [form, setForm] = useState(blank);
  const [query, setQuery] = useState("");
  const [activeTag, setActiveTag] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [error, setError] = useState("");
  const titleRef = useRef(null);

  const editing = noteId !== undefined;
  const editingId = noteId === "new" ? null : noteId;
  const original = editingId == null ? null : notes.find((note) => note.id === editingId) ?? null;
  const dirty = editing && JSON.stringify(form) !== JSON.stringify(copyNote(original));
  const paperById = useMemo(() => new Map(papers.map((paper) => [paper.id, paper])), [papers]);
  const allTags = useMemo(() => [...new Set(notes.flatMap((note) => note.tags))].sort((a, b) => a.localeCompare(b)), [notes]);
  const visible = useMemo(() => {
    const term = query.trim().toLowerCase();
    return notes.filter((note) => {
      const paperText = note.literature_ids.map((id) => paperById.get(id)?.title ?? "").join(" ");
      return (!activeTag || note.tags.includes(activeTag)) && (!term || `${note.title} ${note.content} ${note.tags.join(" ")} ${paperText}`.toLowerCase().includes(term));
    }).sort((a, b) => Number(b.pinned) - Number(a.pinned));
  }, [notes, query, activeTag, paperById]);

  useEffect(() => {
    api.notes.list().then((items) => {
      setNotes(items);
      if (typeof noteId === "number") {
        const note = items.find((item) => item.id === noteId);
        if (note) setForm(copyNote(note));
        else setError("Note not found.");
      }
    }).catch((err) => setError(err.message)).finally(() => setLoading(false));
    api.literature.list({ limit: 200, sort: "title" }).then((page) => setPapers(page.items)).catch(() => {});
  }, [noteId]);

  useEffect(() => {
    if (!editing) return undefined;
    requestAnimationFrame(() => titleRef.current?.focus());
    const shortcut = (event) => {
      if (event.ctrlKey && event.key.toLowerCase() === "s") {
        event.preventDefault();
        document.getElementById("note-form")?.requestSubmit();
      }
    };
    window.addEventListener("keydown", shortcut);
    return () => window.removeEventListener("keydown", shortcut);
  }, [editing]);

  const begin = (note = null) => {
    navigate(note ? `/notes/${note.id}` : "/notes/new");
  };

  const close = () => {
    if (dirty && !window.confirm("Discard your unsaved changes?")) return;
    navigate("/notes");
  };

  const save = async (event) => {
    event.preventDefault();
    setSaving(true);
    setError("");
    try {
      const saved = editingId == null ? await api.notes.create(form) : await api.notes.update(editingId, form);
      setNotes((current) => [saved, ...current.filter((note) => note.id !== saved.id)]);
      setForm(copyNote(saved));
      if (editingId == null) navigate(`/notes/${saved.id}`);
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  };

  const remove = async () => {
    setSaving(true);
    setError("");
    try {
      await api.notes.remove(editingId);
      navigate("/notes");
    } catch (err) {
      setError(err.message);
    } finally {
      setSaving(false);
    }
  };

  if (editing && loading) return <p className="muted notes-loading">Loading note…</p>;
  if (typeof noteId === "number" && !original) return <div className="notes-page note-edit-view">
    <button type="button" className="note-back" onClick={() => navigate("/notes")}><ArrowLeft size={15} /> All notes</button>
    <div className="card"><Empty icon={FileText} title="Note not found" action={<button className="btn" type="button" onClick={() => navigate("/notes")}>Back to notes</button>}>This note may have been deleted.</Empty></div>
  </div>;

  if (editing) return <div className="notes-page note-edit-view">
    <button type="button" className="note-back" onClick={close}><ArrowLeft size={15} /> All notes</button>
    {error && <div className="notice" role="alert"><span>{error}</span></div>}
    <form id="note-form" className="card note-editor" onSubmit={save}>
      <div className="note-editor-top">
        <input ref={titleRef} className="note-title-input" value={form.title} maxLength={200} required disabled={saving}
          onChange={(event) => setForm({ ...form, title: event.target.value })} placeholder="Untitled note" aria-label="Note title" />
        <button className={`note-pin${form.pinned ? " is-pinned" : ""}`} type="button" onClick={() => setForm({ ...form, pinned: !form.pinned })} disabled={saving}
          aria-pressed={form.pinned} title={form.pinned ? "Unpin note" : "Pin note"}><Pin size={15} /> <span>{form.pinned ? "Pinned" : "Pin"}</span></button>
        {original && (confirmDelete ? <div className="note-delete-confirm">
          <span>Delete this note?</span>
          <button className="btn btn-sm" type="button" onClick={() => setConfirmDelete(false)} disabled={saving}>Cancel</button>
          <button className="btn btn-sm btn-danger" type="button" onClick={remove} disabled={saving}>Delete</button>
        </div> : <button className="icon-action" type="button" onClick={() => setConfirmDelete(true)} disabled={saving} aria-label="Delete note" title="Delete note"><Trash2 size={14} /></button>)}
      </div>
      <div className="note-tags-row"><Tag size={14} aria-hidden="true" /><TagEditor tags={form.tags} onChange={(tags) => setForm({ ...form, tags })} disabled={saving} /></div>
      <PaperEditor ids={form.literature_ids} papers={papers} onChange={(literature_ids) => setForm({ ...form, literature_ids })} disabled={saving} />
      <textarea className="note-body-input" value={form.content} maxLength={100000} disabled={saving}
        onChange={(event) => setForm({ ...form, content: event.target.value })} placeholder="Start writing…" aria-label="Note text" />
      <footer className="note-editor-foot">
        <span>{form.content.length.toLocaleString()} characters</span>
        <span className="note-save-hint">Ctrl + S</span>
        <button className="btn" type="button" onClick={close} disabled={saving}>Cancel</button>
        <button className="btn btn-primary" type="submit" disabled={!dirty || saving || !form.title.trim()}>{saving ? "Saving…" : original ? "Save note" : "Create note"}</button>
      </footer>
    </form>
  </div>;

  return <div className="notes-page">
    <div className="notes-hero">
      <div><h2>Notes</h2><PageIntro>Capture research ideas, reminders, and context in one place.</PageIntro></div>
    </div>
    {error && <div className="notice" role="alert"><span>{error}</span></div>}
    <div className="notes-tools">
      <div className="notes-toolbar">
        <label className="note-search"><Search size={15} /><span className="sr-only">Search notes</span><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="Search notes…" /></label>
        <button className="btn btn-primary" type="button" onClick={() => begin()}><Plus size={14} /> New note</button>
      </div>
      {allTags.length > 0 && <div className="note-filter-tags" aria-label="Filter by tag">
        <button type="button" className={!activeTag ? "is-active" : ""} onClick={() => setActiveTag("")}>All</button>
        {allTags.map((tag) => <button type="button" key={tag} className={activeTag === tag ? "is-active" : ""} onClick={() => setActiveTag(tag)}>{tag}</button>)}
      </div>}
    </div>
    {loading ? <p className="muted notes-loading">Loading notes…</p> : visible.length ? <div className="note-grid">
      {visible.map((note) => <button type="button" className="note-card" key={note.id} onClick={() => begin(note)}>
        <div className="note-card-top"><div className="note-card-icon"><FileText size={15} /></div>{note.pinned && <Pin size={14} className="note-card-pin" aria-label="Pinned" />}</div>
        <h3>{note.title}</h3><p>{excerpt(note.content)}</p>
        {note.tags.length > 0 && <div className="note-card-tags">{note.tags.map((tag) => <span key={tag}>{tag}</span>)}</div>}
        {note.literature_ids.length > 0 && <div className="note-card-papers"><BookOpen size={12} /> {note.literature_ids.length} linked {note.literature_ids.length === 1 ? "paper" : "papers"}</div>}
        <time dateTime={note.updated_at}>Updated {formatUpdated(note.updated_at)}</time>
      </button>)}
    </div> : <div className="card"><Empty icon={query || activeTag ? Search : FileText} title={query || activeTag ? "No matching notes" : "No notes yet"} action={query || activeTag ? <button className="btn btn-sm" onClick={() => { setQuery(""); setActiveTag(""); }}>Clear filters</button> : <button className="btn btn-primary" onClick={() => begin()}><Plus size={14} /> Create a note</button>}>
      {query || activeTag ? "Try another search or tag." : "Capture your first idea, reminder, or research observation."}
    </Empty></div>}
  </div>;
}
