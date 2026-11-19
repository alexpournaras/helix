import { useEffect, useId, useRef, useState } from "react";
import { BookOpen, MessageSquare, Plus, RefreshCw, Send, Square, Trash2 } from "lucide-react";
import { api } from "../api.js";
import { Empty, PageIntro } from "../components/ui.jsx";

const CHAT_SELECTION_KEY = "helix.chat.provider-model";
const SCOPE_LABELS = {
  search: "Search result", abstract: "Abstract", full_text: "Full text", figures: "Figures/tables", references: "References",
  abstract_unavailable: "Abstract unavailable", full_text_unavailable: "Full text unavailable",
  figures_unavailable: "Figures unavailable", references_unavailable: "References unavailable",
};

function savedSelection() {
  try {
    const value = JSON.parse(localStorage.getItem(CHAT_SELECTION_KEY));
    return {
      provider: typeof value?.provider === "string" ? value.provider : "ollama",
      model: typeof value?.model === "string" ? value.model : "",
    };
  } catch {
    return { provider: "ollama", model: "" };
  }
}

function Thinking({ text }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return <div className="chat-thinking">
    <button type="button" className="btn btn-sm" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}>
      {open ? "Hide thinking" : "Show thinking"}
    </button>
    <div id={id} className="answer" hidden={!open}>{text}</div>
  </div>;
}

export function Chat({ link }) {
  const initialSelection = useRef(savedSelection()).current;
  const [conversations, setConversations] = useState([]);
  const [conversation, setConversation] = useState(null);
  const [providers, setProviders] = useState([]);
  const [provider, setProvider] = useState(initialSelection.provider);
  const [model, setModel] = useState(initialSelection.model);
  const [think, setThink] = useState(false);
  const [prompt, setPrompt] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [loading, setLoading] = useState(true);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const abort = useRef(null);
  const mounted = useRef(false);
  const thread = useRef(null);
  const following = useRef(true);
  const selected = providers.find((p) => p.id === provider);
  const providerName = (id) => providers.find((item) => item.id === id)?.name || (id === "openai" ? "OpenAI" : id);

  async function refresh() {
    setLoading(true);
    setError("");
    try {
      const [items, available] = await Promise.all([api.chat.list(), api.chat.providers()]);
      if (!mounted.current) return;
      setConversations(items);
      setProviders(available);
    } catch (e) { if (mounted.current) setError(e.message); }
    finally { if (mounted.current) setLoading(false); }
  }
  useEffect(() => {
    mounted.current = true;
    refresh();
    const requested = new URLSearchParams(window.location.search).get("literature");
    if (/^[1-9]\d*$/.test(requested || "")) {
      api.literature.get(requested).then((item) => {
        if (mounted.current) setPrompt((current) => current || `Read the abstract of [Literature #${item.id}] “${item.title || "Untitled"}” and summarize it.`);
      }).catch(() => {
        if (mounted.current) setPrompt((current) => current || `Read the abstract of [Literature #${requested}] and summarize it.`);
      });
    }
    return () => { mounted.current = false; abort.current?.abort(); };
  }, []);
  useEffect(() => {
    const models = selected?.models || [];
    setModel((old) => models.some((m) => m.name === old) ? old :
      models.find((m) => m.name === selected.default_model)?.name || models[0]?.name || "");
  }, [selected]);
  useEffect(() => {
    if (!selected?.models.some((item) => item.name === model)) return;
    localStorage.setItem(CHAT_SELECTION_KEY, JSON.stringify({ provider, model }));
  }, [provider, model, selected]);
  useEffect(() => {
    if (following.current && thread.current) thread.current.scrollTop = thread.current.scrollHeight;
  }, [conversation]);

  async function open(id) {
    setConfirmDelete(false);
    setBusy(true);
    setError("");
    try {
      const item = await api.chat.get(id);
      if (!mounted.current) return;
      following.current = true;
      setConversation(item);
      setPrompt("");
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  }

  async function remove() {
    setBusy(true);
    try {
      await api.chat.remove(conversation.id);
      setConversation(null);
      setConfirmDelete(false);
      setConversations(await api.chat.list());
    } catch (e) { setError(e.message); }
    finally { setBusy(false); }
  }

  async function send(e) {
    e.preventDefault();
    if (busy || !prompt.trim() || !model) return;
    setBusy(true);
    setError("");
    following.current = true;
    const content = prompt.trim();
    const controller = new AbortController();
    abort.current = controller;
    let current = conversation;
    let accepted = false;
    let done = false;
    try {
      if (!current) current = await api.chat.create();
      if (!mounted.current) return;
      const reply = { role: "assistant", content: "", thinking: "", sources: [], tool_activity: [], provider, model, status: "streaming" };
      current = { ...current, title: current.messages.length ? current.title : content.slice(0, 80),
        messages: [...current.messages, { role: "user", content }, reply] };
      setConversation(current);
      setPrompt("");
      await api.chat.send(current.id, { provider, model, content, think }, (event) => {
        accepted = true;
        if (event.type === "content" || event.type === "thinking") reply[event.type] += event.text;
        if (event.type === "tool") {
          const known = new Set(reply.sources.map((source) => source.id));
          reply.sources.push(...(event.sources || []).filter((source) => !known.has(source.id)));
          const activity = { name: event.name, scope: event.scope, error: event.error, audit: event.audit,
            source_ids: (event.sources || []).map((source) => source.id) };
          if (!reply.tool_activity.some((item) => JSON.stringify(item) === JSON.stringify(activity))) reply.tool_activity.push(activity);
        }
        if (event.type === "done") { done = true; reply.status = "complete"; }
        if (event.type === "error") { done = true; reply.status = "error"; reply.error = event.message; }
        if (mounted.current) setConversation({ ...current, messages: [...current.messages] });
      }, controller.signal);
      if (!done) throw new Error("The connection ended before the reply finished.");
    } catch (e) {
      if (mounted.current) {
        if (e.name !== "AbortError") setError(e.message);
        if (!accepted && e.name !== "AbortError") setPrompt(content);
        if (current?.messages.at(-1)?.status === "streaming") {
          current.messages.at(-1).status = "interrupted";
          setConversation({ ...current });
        }
      }
    } finally {
      abort.current = null;
      if (mounted.current) {
        // The server owns history, including partial replies when generation stops.
        try {
          const items = await api.chat.list();
          if (mounted.current) setConversations(items);
          if (current && !controller.signal.aborted) {
            const saved = await api.chat.get(current.id);
            if (mounted.current) setConversation(saved);
          }
        } catch (e) { if (mounted.current) setError(e.message); }
        if (mounted.current) setBusy(false);
      }
    }
  }

  return <div className="chat-page">
    <PageIntro>Ask questions, explore ideas, work with notes, or have AI search and read papers saved in your literature library.</PageIntro>
    {error && <div className="notice" role="alert">{error}</div>}
    <div className="chat-layout">
      <aside className="card chat-history" aria-label="Conversations">
        <header className="card-head"><h2 className="card-title">Conversations</h2></header>
        <div className="chat-history-actions"><button className="btn btn-sm" disabled={busy} onClick={() => { setConversation(null); setPrompt(""); setError(""); setConfirmDelete(false); }}><Plus size={14} /> New chat</button></div>
        <div className="chat-history-list">
          {conversations.map((item) => <button key={item.id} className="chat-history-item" disabled={busy}
            aria-current={item.id === conversation?.id ? "true" : undefined} onClick={() => open(item.id)}>
            <MessageSquare size={14} /><span>{item.title}</span>
          </button>)}
          {!conversations.length && <p className="muted small">{loading ? "Loading conversations…" : "Your conversations will appear here."}</p>}
        </div>
      </aside>
      <section className="card chat-workspace" aria-label="AI conversation">
        <header className="card-head">
          <div className="chat-heading"><h2 className="card-title">{conversation?.title || "New conversation"}</h2>
            <p className="card-desc">Conversations are saved in your workspace.</p></div>
          <div className="card-actions">
            <button className="btn btn-sm" disabled={busy || loading} onClick={refresh} title="Refresh models and conversations"><RefreshCw size={13} className={loading ? "spin" : undefined} /> Refresh</button>
            {conversation && !confirmDelete && <button className="icon-action" disabled={busy} onClick={() => setConfirmDelete(true)} aria-label="Delete conversation" title="Delete conversation"><Trash2 size={14} /></button>}
          </div>
        </header>
        {confirmDelete && <div className="card-foot" role="group" aria-label="Confirm deleting conversation">
          <p className="muted">Delete this conversation? This can’t be undone.</p>
          <button className="btn btn-sm btn-danger" disabled={busy} onClick={remove}>Delete</button>
          <button className="btn btn-sm" disabled={busy} onClick={() => setConfirmDelete(false)}>Keep</button>
        </div>}
        <div className="filters chat-toolbar">
          <label>Provider<select className="input select" value={provider} disabled={busy || loading} onChange={(e) => setProvider(e.target.value)}>
            {!providers.length && <option value="ollama">Ollama</option>}
            {providers.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
          </select></label>
          <label className="chat-model">Model<select className="input select" value={model} disabled={busy || loading} onChange={(e) => setModel(e.target.value)}>
            {!model && <option value="">{loading ? "Loading models…" : "No models available"}</option>}
            {selected?.models.map((m) => <option key={m.name} value={m.name}>{m.name}</option>)}
          </select></label>
          {provider === "ollama" && <a className="btn" {...link("/integrations/ollama")}>Configure</a>}
          {provider === "openai" && <a className="btn" {...link("/integrations/openai")}>Configure</a>}
        </div>
        {provider === "ollama" && <div className="chat-options">
          <label className="check" title="Enable only for models that support thinking. Replies can take longer.">
            <input type="checkbox" checked={think} disabled={busy} onChange={(e) => setThink(e.target.checked)} />
            Thinking
          </label>
          <span className="muted small">Optional reasoning before the answer. May take longer.</span>
        </div>}
        {!loading && selected && (!selected.reachable || !selected.models.length) && <div className="notice" role="status">
          {selected.error || "No models installed. Install a model in Ollama, then refresh."}
        </div>}
        <div className="chat-thread" ref={thread} aria-label="Messages" aria-busy={busy}
          onScroll={() => { const el = thread.current; following.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80; }}>
          {!conversation?.messages.length && <Empty icon={MessageSquare} title="What would you like to explore?">Choose a model and send your first message. Conversations are saved in your workspace.</Empty>}
          {conversation?.messages.map((message, i) => <article key={i} className={`chat-message chat-message-${message.role}`}>
            <div className="chat-message-label">{message.role === "user" ? "You" : `${message.model} · ${providerName(message.provider)}`}</div>
            {message.thinking?.trim() && <Thinking key={`${conversation.id}-${i}`} text={message.thinking} />}
            <div className="answer">{message.content || (message.status === "streaming"
              ? (message.sources?.length ? "Reading saved literature…" : message.tool_activity?.length ? "Working with workspace data…" : message.thinking ? "Generating reply…" : `Waiting for ${message.model}…`) : "")}</div>
            {message.sources?.length > 0 && <div className="chat-sources" aria-label="Literature accessed for this answer">
              <div className="chat-sources-label"><BookOpen size={13} /> Literature accessed</div>
              <div className="chat-source-list">
                {message.sources.map((source) => {
                  const scopes = [...new Set((message.tool_activity || []).filter((item) => item.source_ids?.includes(source.id)).map((item) => item.scope).filter(Boolean))];
                  return <a key={source.id} className="chat-source" title={source.title} {...link(`/literature?q=${encodeURIComponent(source.title)}`)}>
                    <span>#{source.id}</span><span className="chat-source-title">{source.title}</span>
                    {scopes.map((scope) => <em key={scope} className={scope.endsWith("_unavailable") ? "is-unavailable" : ""}>{SCOPE_LABELS[scope] || scope}</em>)}
                  </a>;
                })}
              </div>
            </div>}
            {message.error && <p className="bad" role="alert">{message.error}</p>}
            {message.status === "interrupted" && <p className="muted">Reply stopped.</p>}
          </article>)}
        </div>
        <form className="card-foot chat-composer" onSubmit={send}>
          <label className="sr-only" htmlFor="chat-prompt">Message</label>
          <textarea className="input textarea" id="chat-prompt" placeholder="Ask a question…" value={prompt} maxLength={100000} disabled={busy}
            onChange={(e) => setPrompt(e.target.value)} onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) { e.preventDefault(); send(e); }
            }} />
          <div className="try-actions"><span className="muted small">Enter to send · Shift + Enter for a new line</span>
            {busy && abort.current ? <button type="button" className="btn" onClick={() => abort.current?.abort()}><Square size={14} /> Stop</button>
              : <button className="btn btn-primary" disabled={busy || loading || !prompt.trim() || !model || !selected?.reachable}><Send size={14} /> Send</button>}
          </div>
        </form>
      </section>
    </div>
  </div>;
}
