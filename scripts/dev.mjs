// Starts the FastAPI backend and the Vite dev server together.
// The backend restarts when a file in backend/app changes. We do this here instead of
// using uvicorn's --reload, which hangs on Windows when the process has no console.
import { spawn } from "node:child_process";
import { existsSync, watch } from "node:fs";
import net from "node:net";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const isWindows = process.platform === "win32";
const host = "127.0.0.1";
const venvPython = isWindows
  ? join(root, "backend", ".venv", "Scripts", "python.exe")
  : join(root, "backend", ".venv", "bin", "python");
const python = existsSync(venvPython) ? venvPython : isWindows ? "python" : "python3";

const apiPort = Number(process.env.HELIX_PORT ?? (await findFreePort(8000)));
const webPort = Number(process.env.VITE_PORT ?? (await findFreePort(5173)));
const apiOrigin = `http://${host}:${apiPort}`;

const defs = {
  api: {
    command: python,
    args: ["run.py"],
    cwd: join(root, "backend"),
    env: { HELIX_HOST: host, HELIX_PORT: String(apiPort), HELIX_RELOAD: "0", PYTHONUNBUFFERED: "1" },
  },
  web: {
    command: "npx",
    args: ["vite", "--host", host, "--port", String(webPort), "--strictPort"],
    cwd: root,
    env: { HELIX_API_URL: apiOrigin },
    shell: isWindows,
  },
};

console.log(`[helix] API → ${apiOrigin}  (docs: ${apiOrigin}/docs)`);
console.log(`[helix] Web → http://${host}:${webPort}`);

let shuttingDown = false;
const children = { api: start("api"), web: start("web") };
for (const signal of ["SIGINT", "SIGTERM"]) process.on(signal, () => shutdown(0));

// ---- backend auto-restart ----
let restartTimer = null;
let restarting = false;
watch(join(root, "backend", "app"), { recursive: true }, (_event, file) => {
  if (!file || !/\.(py|json)$/.test(file) || file.includes("__pycache__")) return;
  clearTimeout(restartTimer);
  restartTimer = setTimeout(() => restartApi(file), 250);
});

async function restartApi(file) {
  if (shuttingDown || restarting) return;
  restarting = true;
  console.log(`[helix] ${file} changed, restarting API`);
  await kill(children.api);
  children.api = start("api");
  restarting = false;
}

function start(name) {
  const def = defs[name];
  const child = spawn(def.command, def.args, {
    cwd: def.cwd,
    env: { ...process.env, ...def.env },
    stdio: ["ignore", "pipe", "pipe"],
    shell: def.shell ?? false,
  });
  const write = (chunk) => {
    for (const line of chunk.toString().split(/\r?\n/)) if (line) console.log(`[${name}] ${line}`);
  };
  child.stdout.on("data", write);
  child.stderr.on("data", write);
  child.on("exit", (code, signal) => {
    if (shuttingDown || child.intentionalExit) return;
    console.error(`[${name}] exited (${signal ?? `code ${code}`})`);
    // A crashing API (e.g. a syntax error mid-edit) waits for the next save instead of taking everything down.
    if (name === "api") console.error("[helix] API stopped. Fix the error and save to restart it.");
    else shutdown(code || 1);
  });
  return child;
}

function kill(child) {
  return new Promise((resolve) => {
    if (!child || child.exitCode !== null) return resolve();
    child.intentionalExit = true;
    child.once("exit", () => resolve());
    if (isWindows) spawn("taskkill", ["/pid", String(child.pid), "/t", "/f"], { stdio: "ignore" });
    else child.kill("SIGTERM");
  });
}

async function shutdown(exitCode) {
  if (shuttingDown) return;
  shuttingDown = true;
  await Promise.race([Promise.all(Object.values(children).map(kill)), new Promise((r) => setTimeout(r, 2000))]);
  process.exit(exitCode);
}

function findFreePort(port) {
  return new Promise((resolve, reject) => {
    const server = net.createServer();
    server.unref();
    server.on("error", (err) => (err.code === "EADDRINUSE" || err.code === "EACCES" ? resolve(findFreePort(port + 1)) : reject(err)));
    server.listen({ host, port }, () => server.close(() => resolve(port)));
  });
}
