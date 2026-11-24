"""Research health check: `npm run research:doctor` (PLAN B2, 1A.10).

Offline by default: keys (names only, never values), profile, pre-flight (roles, probes, independence),
privacy, database, knowledge files, and git hygiene. `--probe` also sends a tiny structured request to each
configured model that has a key and records the result (no case text). Exit code 1 if any check fails.
"""

from __future__ import annotations

import argparse
import asyncio
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

import yaml

from app.config import ProviderKeys, settings as app_settings
from app.research import mesh, preflight
from app.research.db import SCHEMA_VERSION
from app.research.providers import default_providers
from app.research.settings import ResearchSettings, SettingsStore

REPO_ROOT = Path(__file__).resolve().parents[3]
KNOWLEDGE = Path(__file__).resolve().parent / "knowledge"
# (variable, field on ProviderKeys, status when missing, why)
KEYS = (
    ("OPENAI_API_KEY", "openai_api_key", "warn", "model family A"),
    ("ANTHROPIC_API_KEY", "anthropic_api_key", "warn", "model family B, cross-family checks"),
    ("NCBI_API_KEY", "ncbi_api_key", "warn", "PubMed and MeSH at 10 requests/s instead of 3"),
    ("OPENALEX_API_KEY", "openalex_api_key", "warn", "search and citation graph (M2)"),
    ("UNPAYWALL_EMAIL", "unpaywall_email", "warn", "open-access PDFs (M2); you choose the email"),
)
SECRET_PATHS = ("backend/.env", "backend/data")


@dataclass
class Check:
    name: str
    status: str  # ok / warn / fail
    detail: str


def _git_tracked(paths: Iterable[str]) -> list[str]:
    try:
        out = subprocess.run(["git", "ls-files", "--", *paths], cwd=REPO_ROOT, capture_output=True, text=True,
                             timeout=20)
    except (OSError, subprocess.SubprocessError):
        return []
    return [line for line in out.stdout.splitlines() if line.strip()]


def _keys(keys: ProviderKeys) -> list[Check]:
    present = keys.present()
    out = []
    for var, attr, missing, why in KEYS:
        if attr in present:
            out.append(Check("keys", "ok", f"{var} set ({why})"))
        else:
            out.append(Check("keys", missing, f"{var} missing ({why})"))
    return out


def _preflight(research: ResearchSettings, probes: preflight.ProbeStore) -> list[Check]:
    result = preflight.run(research, default_providers(research), probes, synthetic=False)
    out = [Check("preflight", "fail", e) for e in result.errors]
    out += [Check("preflight", "warn", w) for w in result.warnings]
    if result.ok:
        out.insert(0, Check("preflight", "ok", f"roles resolve; providers: {', '.join(result.providers) or 'none'}"))
    return out


def _privacy(research: ResearchSettings) -> list[Check]:
    out = []
    guard = preflight.loopback_guard()
    out.append(Check("privacy", "fail", guard) if guard else Check("privacy", "ok", "bound to this machine"))
    shares = research.privacy.openai_project_shares_data
    if shares is None:
        out.append(Check("privacy", "warn", "Answer the privacy question: does your OpenAI project share data?"))
    elif shares:
        out.append(Check("privacy", "warn", "OpenAI project shares data: only synthetic cases will run"))
    else:
        out.append(Check("privacy", "ok", "OpenAI project without data sharing"))
    if research.privacy.store_prompts:
        out.append(Check("privacy", "warn", "store_prompts is on: prompt excerpts are kept in llm_calls"))
    return out


def _database(path: Path) -> Check:
    if not path.exists():
        return Check("database", "ok", "research.sqlite3 not created yet (created on first start)")
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        try:
            integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
            row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        return Check("database", "fail", f"research.sqlite3 can't be read: {exc}")
    if integrity != "ok":
        return Check("database", "fail", f"research.sqlite3 integrity check: {integrity}")
    version = int(row[0]) if row else 0
    if version > SCHEMA_VERSION:
        return Check("database", "fail", f"schema {version} is newer than this code ({SCHEMA_VERSION})")
    if version < SCHEMA_VERSION:
        return Check("database", "warn", f"schema {version}; migrates to {SCHEMA_VERSION} on next start")
    return Check("database", "ok", f"research.sqlite3 schema {version}, integrity ok")


def _knowledge() -> list[Check]:
    out = []
    try:
        table = mesh.load_mapping()
        out.append(Check("knowledge", "ok", f"mesh_systems.yaml: {len(table['prefixes'])} prefixes, "
                                            f"{len(table['systems'])} systems"))
    except (OSError, ValueError, yaml.YAMLError) as exc:
        out.append(Check("knowledge", "fail", f"mesh_systems.yaml: {exc}"))
    floor = KNOWLEDGE / "universal" / "red_flags.yaml"
    if floor.exists():
        out.append(Check("knowledge", "ok", "universal/red_flags.yaml: present"))
    else:
        out.append(Check("knowledge", "warn", "universal/red_flags.yaml: not written yet (1B.1)"))
    return out


def checks(*, data_dir: Path | None = None, keys: ProviderKeys | None = None,
           git_tracked: Callable[[Iterable[str]], list[str]] = _git_tracked) -> list[Check]:
    data_dir = Path(data_dir or app_settings.data_dir)
    research = SettingsStore(data_dir / "research.json").load()
    probes = preflight.ProbeStore(data_dir / "research_probes.json")
    out = _keys(keys or ProviderKeys())
    out.append(Check("profile", "ok", f"profile {research.profile}"))
    out += _preflight(research, probes)
    out += _privacy(research)
    out.append(_database(data_dir / "research.sqlite3"))
    out += _knowledge()
    tracked = git_tracked(SECRET_PATHS)
    out.append(Check("git", "fail", f"tracked by git, remove from the index: {', '.join(tracked)}") if tracked
               else Check("git", "ok", "backend/.env and backend/data are not tracked"))
    return out


async def probe_all(data_dir: Path | None = None) -> list[Check]:
    """Probe every model the profile configures (members, fallbacks, strongest) whose provider has a key."""
    data_dir = Path(data_dir or app_settings.data_dir)
    research = SettingsStore(data_dir / "research.json").load()
    providers = default_providers(research)
    store = preflight.ProbeStore(data_dir / "research_probes.json")
    refs = {}
    for role in research.roles.values():
        for ref in [*role.members, *role.fallback]:
            refs.setdefault((ref.family, ref.model), ref)
    for ref in research.strongest.values():
        refs.setdefault((ref.family, ref.model), ref)
    out = []
    for (family, model), ref in sorted(refs.items()):
        provider = providers.get(family)
        if provider is None or not provider.has_key():
            out.append(Check("probe", "warn", f"{model}: skipped (no key for {family})"))
            continue
        record = await preflight.probe(provider, ref)
        store.put(model, record)
        out.append(Check("probe", "ok" if record.get("ok") else "fail",
                         f"{model}: {'structured output ok' if record.get('ok') else record.get('error', 'failed')}"))
    return out


def render(items: list[Check]) -> str:
    return "\n".join(f"{c.status.upper():<5} {c.name:<10} {c.detail}" for c in items)


def exit_code(items: list[Check]) -> int:
    return 1 if any(c.status == "fail" for c in items) else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Helix research health check")
    parser.add_argument("--probe", action="store_true", help="probe each configured model (sends a tiny request)")
    args = parser.parse_args(argv)
    items: list[Check] = []
    if args.probe:
        items += asyncio.run(probe_all())
    items += checks()
    if hasattr(sys.stdout, "reconfigure"):  # Windows consoles default to cp1252
        sys.stdout.reconfigure(errors="replace")
    print(render(items))
    return exit_code(items)


if __name__ == "__main__":
    sys.exit(main())
