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
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return res.json();
}

export const api = {
  health: () => request("/health"),
  overview: () => request("/overview"),
  integrations: () => request("/integrations"),
  ollama: () => request("/ollama"),
  getSettings: () => request("/settings"),
  saveSettings: (body) => request("/settings", { method: "PUT", body: JSON.stringify(body) }),
};
