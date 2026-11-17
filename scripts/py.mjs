// Runs Python with the given args: the backend venv's interpreter if it exists, otherwise the
// system one. Creating the venv itself (`-m venv`) always uses the system interpreter.
import { spawnSync } from "node:child_process";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const isWindows = process.platform === "win32";
const venv = isWindows
  ? join(root, "backend", ".venv", "Scripts", "python.exe")
  : join(root, "backend", ".venv", "bin", "python");
const args = process.argv.slice(2);
const creatingVenv = args[0] === "-m" && args[1] === "venv";

function systemPython() {
  for (const candidate of isWindows ? ["python", "py"] : ["python3", "python"]) {
    if (spawnSync(candidate, ["--version"], { stdio: "ignore" }).status === 0) return candidate;
  }
  console.error("Python 3 was not found. Install Python 3.11+ and make sure it is on your PATH.");
  process.exit(1);
}

const python = !creatingVenv && existsSync(venv) ? venv : systemPython();
const result = spawnSync(python, args, {
  stdio: "inherit",
  env: { ...process.env, HELIX_RELOAD: process.env.HELIX_RELOAD ?? "0" },
});
process.exit(result.status ?? 1);
