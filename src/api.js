// FastAPI validation errors arrive as a list; turn them into one readable sentence.
function describeDetail(detail) {
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail.map((d) => String(d.msg ?? d).replace(/^Value error, /, "")).join(" ");
  }
  return JSON.stringify(detail);
}

async function request(path, options = {}) {
  const res = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      // not JSON
    }
    throw new Error(describeDetail(detail));
  }
  return res.status === 204 ? null : res.json();
}

// Arrays become repeated keys (?tag=a&tag=b); empty values are left out.
const qs = (params) => {
  const entries = Object.entries(params)
    .flatMap(([k, v]) => (Array.isArray(v) ? v.map((x) => [k, x]) : [[k, v]]))
    .filter(([, v]) => v !== undefined && v !== null && v !== "");
  return entries.length ? `?${new URLSearchParams(entries)}` : "";
};

/**
 * POST and read an NDJSON stream, calling onEvent for each parsed line.
 * Resolves when the stream ends; rejects on network errors (or AbortError when cancelled).
 */
async function stream(path, body, onEvent, signal) {
  const res = await fetch(`/api${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal,
  });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      detail = (await res.json()).detail ?? detail;
    } catch {
      // not JSON
    }
    throw new Error(describeDetail(detail));
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    let newline;
    while ((newline = buffer.indexOf("\n")) >= 0) {
      const line = buffer.slice(0, newline).trim();
      buffer = buffer.slice(newline + 1);
      if (line) onEvent(JSON.parse(line));
    }
  }
  if (buffer.trim()) onEvent(JSON.parse(buffer));
}

export const api = {
  chat: {
    providers: () => request("/chat/providers"),
    list: () => request("/chat/conversations"),
    create: () => request("/chat/conversations", { method: "POST" }),
    get: (id) => request(`/chat/conversations/${id}`),
    remove: (id) => request(`/chat/conversations/${id}`, { method: "DELETE" }),
    send: (id, body, onEvent, signal) => stream(`/chat/conversations/${id}/messages`, body, onEvent, signal),
  },
  health: () => request("/health"),
  overview: () => request("/overview"),
  integrations: () => request("/integrations"),
  getSettings: () => request("/settings"),
  saveSettings: (body) => request("/settings", { method: "PUT", body: JSON.stringify(body) }),

  ollama: {
    // Pass baseUrl to test an unsaved address.
    status: (baseUrl) =>
      request(`/integrations/ollama/status${baseUrl ? `?base_url=${encodeURIComponent(baseUrl)}` : ""}`),
    getConfig: () => request("/integrations/ollama/config"),
    saveConfig: (body) => request("/integrations/ollama/config", { method: "PUT", body: JSON.stringify(body) }),
    chat: (body, onEvent, signal) => stream("/integrations/ollama/chat", body, onEvent, signal),
  },

  openai: {
    status: () => request("/integrations/openai/status"),
    getConfig: () => request("/integrations/openai/config"),
    saveConfig: (body) => request("/integrations/openai/config", { method: "PUT", body: JSON.stringify(body) }),
    chat: (body, onEvent, signal) => stream("/integrations/openai/chat", body, onEvent, signal),
  },

  literature: {
    // source: "pubmed" | "pmc" | "europepmc"; body: { term, max_results, sort }
    peek: (source, id) => request(`/integrations/${source}/peek${qs({ id })}`),
    preview: (source, body) => request(`/integrations/${source}/preview`, { method: "POST", body: JSON.stringify(body) }),
    startFetch: (source, body) => request(`/integrations/${source}/fetch`, { method: "POST", body: JSON.stringify(body) }),
    runs: (params = {}) => request(`/literature/runs${qs(params)}`),
    run: (id) => request(`/literature/runs/${id}`),
    cancelRun: (id) => request(`/literature/runs/${id}/cancel`, { method: "POST" }),
    list: (params = {}) => request(`/literature${qs(params)}`),
    facets: () => request("/literature/facets"),
    get: (id) => request(`/literature/${id}`),
    remove: (id) => request(`/literature/${id}`, { method: "DELETE" }),
    refresh: (id) => request(`/literature/${id}/refresh`, { method: "POST" }),
    removeAll: () => request("/literature", { method: "DELETE" }),
    addTag: (id, name) => request(`/literature/${id}/tags`, { method: "POST", body: JSON.stringify({ name }) }),
    removeTag: (id, name) => request(`/literature/${id}/tags/${encodeURIComponent(name)}`, { method: "DELETE" }),
  },
};
