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
  return res.json();
}

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
};
