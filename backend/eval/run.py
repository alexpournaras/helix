"""Eval runner (PLAN Part E, Part G.3–4, 1A.10).

    .venv/Scripts/python -m eval.run --mode fake|replay|live [--case ID] [--name NAME] [--compare OLD.json] [--stop-at STAGE]

Modes:
- fake: fake model providers, network blocked. Checks the pipeline mechanics.
- replay: real adapters, every HTTP request served from eval/recordings/<name>/ (unknown requests fail).
  Use it to compare a prompt or threshold change against a recorded run (Part G.4).
- live: real providers and sources; every response is recorded to eval/recordings/<name>/ for later replay.
  Pre-flight must pass (privacy gate included).

Cases are YAML files in eval/cases/<system>/ and must be synthetic (B16.8). Each case runs in its own temporary
data folder; results go to eval/results/<name>.json (git-ignored, like recordings). Answers to the pipeline's
waits come from the case through the factory's resolvers.

The pipeline factory is `module:function`, called as factory(ctx) → (Pipeline, resolvers), where resolvers maps a
`waiting_for` value to fn(case, investigation) → the `apply` function for Machine.resolve_wait.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import shutil
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Literal

import yaml
from pydantic import BaseModel, Field

from app.research import llm, preflight, settings as S, transport
from app.research.db import ResearchDB
from app.research.prices import PriceTable
from app.research.providers import FakeProvider, default_providers
from app.research.ratelimit import Limiters
from app.research.runner import Worker

EVAL_DIR = Path(__file__).resolve().parent
CASES_DIR = EVAL_DIR / "cases"
RESULTS_DIR = EVAL_DIR / "results"
RECORDINGS_DIR = EVAL_DIR / "recordings"
DEFAULT_FACTORY = "app.research.steps:build_pipeline"
MAX_WAITS = 12
Mode = Literal["fake", "replay", "live"]


class EvalCase(BaseModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    system: str
    synthetic: Literal[True]  # real cases never go into eval (B16.8)
    problem_text: str = Field(min_length=1)
    profile: str = "dev_openai"
    answers: dict[str, str] = Field(default_factory=dict)  # question key or pattern → scripted answer
    approve: bool = True  # approve plans and selections
    acknowledge: bool = True  # acknowledge urgent/emergency safety waits
    rewrite: str | None = None  # new problem text if intake asks for a rewrite (synthetic too)
    expected: dict[str, Any] = Field(default_factory=dict)  # safety level, red flags, hypothesis families …
    notes: str = ""


def load_cases(folder: Path = CASES_DIR, only: str | None = None) -> list[EvalCase]:
    """Every `*.yaml` under folder (files starting with `_` are examples), sorted by id."""
    cases = []
    for path in sorted(Path(folder).rglob("*.yaml")):
        if path.name.startswith("_"):
            continue
        data = yaml.safe_load(path.read_text("utf-8")) or {}
        try:
            case = EvalCase.model_validate(data)
        except Exception as exc:  # noqa: BLE001 - name the file
            raise ValueError(f"{path.name}: {exc}") from exc
        if only is None or case.id == only:
            cases.append(case)
    ids = [c.id for c in cases]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate case ids.")
    return sorted(cases, key=lambda c: c.id)


# ---------- E1 pipeline checks (Part E1); steps add theirs as they are built ----------

@dataclass
class CheckResult:
    id: str
    ok: bool
    detail: str = ""


def _e1_8_no_open_tasks(db: ResearchDB, inv: dict) -> CheckResult:
    open_tasks = db.open_task_count(inv["id"])
    if inv["status"] != "complete":
        return CheckResult("E1.8", True, f"not complete ({inv['status']})")
    return CheckResult("E1.8", open_tasks == 0, f"{open_tasks} open task(s) after completion")


def _e1_13_votes(db: ResearchDB, inv: dict) -> CheckResult:
    """Placeholder until claims exist (1E): counts are reported, no claims → ok."""
    rows = db.query("SELECT COUNT(*) AS n FROM llm_calls WHERE investigation_id = ?", (inv["id"],))
    return CheckResult("E1.13", True, f"{rows[0]['n']} model call(s) logged")


E1_CHECKS: list[Callable[[ResearchDB, dict], CheckResult]] = [_e1_8_no_open_tasks, _e1_13_votes]


# ---------- running ----------

def intake_summary(db: ResearchDB, inv: dict) -> dict | None:
    """What intake decided (vocabulary and counts only), for comparing runs and expectations."""
    row = db.one("SELECT data, check_results FROM case_briefs WHERE investigation_id = ? ORDER BY version DESC "
                 "LIMIT 1", (inv["id"],))
    if row is None:
        return None
    brief, check = row["data"] or {}, row["check_results"] or {}
    return {"language": inv["problem_lang"], "complaints": [c["key"] for c in inv["complaints"]],
            "mesh": [m["name"] for c in inv["complaints"] for m in c["mesh"]],
            "mesh_dropped": [t for c in inv["complaints"] for t in c["mesh_dropped"]],
            "systems": inv["systems"], "populations": inv["special_populations"],
            "unknowns": [u["key"] for u in brief.get("unknowns", [])], "check": check.get("counts"),
            **safety_summary(db, inv), **grounding_summary(db, inv)}


def grounding_summary(db: ResearchDB, inv: dict) -> dict:
    """Per complaint: status, counts and the kept guideline items (vocabulary only)."""
    runs = []
    for ev in db.query("SELECT data FROM events WHERE investigation_id = ? AND kind = 'grounding' ORDER BY id",
                       (inv["id"],)):
        d = ev["data"] or {}
        runs.append({k: d.get(k) for k in ("complaint", "population", "candidates", "screened", "selected", "reads",
                                           "items", "failed_queries", "dropped_attestations")})
    for r in db.query("SELECT complaint_key, status, gap_reason FROM grounding_runs WHERE investigation_id = ?",
                      (inv["id"],)):
        match = next((x for x in runs if x["complaint"] == r["complaint_key"]), None)
        if match is None:
            runs.append({"complaint": r["complaint_key"]})
            match = runs[-1]
        match.update(status=r["status"], gap_reason=r["gap_reason"])
    items = db.query("SELECT kind, text, trust FROM knowledge_items WHERE investigation_id = ? AND origin = 'guideline' "
                     "AND status = 'active' ORDER BY kind, id", (inv["id"],))
    return {"knowledge_version": inv["knowledge_version"], "grounding": runs,
            "guideline_items": [f"{i['kind']}: {i['text']} ({i['trust']})" for i in items]}


def safety_summary(db: ResearchDB, inv: dict) -> dict:
    """The latest safety review: level, rule hits (refs), rule-out question keys, crisis, panel votes."""
    rev = db.one("SELECT * FROM safety_reviews WHERE investigation_id = ? ORDER BY id DESC LIMIT 1", (inv["id"],))
    if rev is None:
        return {}
    panel = rev["panel"] or {}
    return {"safety_level": rev["level"], "rule_level": rev["rule_level"],
            "fired": [h["ref"] for h in rev["rule_hits"] if h.get("state") == "true"],
            "rule_outs": panel.get("questions", []), "crisis": panel.get("crisis", False),
            "panel_levels": [m.get("value", {}).get("level") if m.get("ok") else "failed"
                             for m in panel.get("members", [])]}


def intake_misses(case: EvalCase, summary: dict | None, waits: list[str], outcome: str | None = None) -> list[str]:
    """Expectations from the case file that intake didn't meet (information; not an E1 check).
    `waits` includes the wait the run ended in, if any."""
    exp, out = case.expected, []
    if "first_wait" in exp and (waits[:1] or [None])[0] != exp["first_wait"]:
        out.append(f"first wait {waits[:1]} != {exp['first_wait']}")
    if "outcome" in exp and outcome != exp["outcome"]:
        out.append(f"outcome {outcome} != {exp['outcome']}")
    if summary is None:
        return out
    if "language" in exp and summary["language"] != exp["language"]:
        out.append(f"language {summary['language']} != {exp['language']}")
    norm = lambda t: t.lower().replace("_", " ")
    names = {norm(n) for n in summary["mesh"] + summary["complaints"]}
    out += [f"complaint {c} missing" for c in exp.get("complaints", []) if norm(c) not in names]
    out += [f"system {s} missing" for s in exp.get("systems_include", []) if s not in summary["systems"]]
    if "populations" in exp and sorted(summary["populations"]) != sorted(exp["populations"]):
        out.append(f"populations {summary['populations']} != {exp['populations']}")
    if "safety_level_in" in exp and summary.get("safety_level") not in exp["safety_level_in"]:
        out.append(f"safety level {summary.get('safety_level')} not in {exp['safety_level_in']}")
    out += [f"rule {r} didn't fire" for r in exp.get("rules_fire", []) if r not in summary.get("fired", [])]
    out += [f"rule-out {q} not asked" for q in exp.get("rule_outs", []) if q not in summary.get("rule_outs", [])]
    if exp.get("crisis") and not summary.get("crisis"):
        out.append("crisis not recognised")
    return out


LOCAL_KEYS = ("privacy", "providers")  # machine settings a live run inherits; the case picks the profile


def local_overrides(path: Path | None = None) -> dict:
    """Your saved privacy answer and provider settings (data/research.json), so the privacy gate sees them."""
    if path is None:  # resolved per call, so tests (HELIX_DATA_DIR → a temp folder) never see the real file
        from app.config import settings as app_settings
        path = Path(os.environ.get("HELIX_DATA_DIR") or app_settings.data_dir) / "research.json"
    saved = S.SettingsStore(path).raw().get("overrides") or {}
    return {k: saved[k] for k in LOCAL_KEYS if k in saved}


SHARED_CACHE = RESULTS_DIR / "llm_cache.sqlite3"  # git-ignored; live runs reuse identical model answers


def shared_cache(path: Path = SHARED_CACHE) -> ResearchDB:
    path.parent.mkdir(parents=True, exist_ok=True)
    cache = ResearchDB(path)
    cache.init()
    return cache


def _context(db: ResearchDB, mode: Mode, profile: str, cache: ResearchDB | None = None) -> llm.CallContext:
    research = S.build(profile, local_overrides() if mode == "live" else None)
    if mode == "fake":
        providers = {f: FakeProvider(f) for f in ("openai", "anthropic", "ollama")}
        available = None
    else:
        providers = default_providers(research)
        # Replay needs no keys: auth headers aren't part of a recording's key.
        available = (lambda ref: True) if mode == "replay" else None
    from app.literature.db import LiteratureDB
    library = LiteratureDB(Path(db.path).parent / "literature.db")  # each case gets its own library
    return llm.CallContext(db=db, settings=research, providers=providers, limiters=Limiters({}),
                           prices=PriceTable(), available=available, literature=library, cache=cache)


def _transport(mode: Mode, recordings: Path):
    if mode == "fake":
        return transport.BlockedTransport()
    if mode == "replay":
        if not recordings.exists():
            raise FileNotFoundError(f"No recordings at {recordings}.")
        return transport.ReplayTransport(recordings)
    return transport.RecordTransport(recordings)


def stopping(factory: Callable, stop_at: str | None) -> Callable:
    """The factory with `stop_at` passed on (live checks of the early stages without paying for the later ones)."""
    if not stop_at:
        return factory
    return lambda ctx: factory(ctx, stop_at=stop_at)


def load_factory(spec: str) -> Callable:
    module, _, name = spec.partition(":")
    try:
        return getattr(importlib.import_module(module), name)
    except (ImportError, AttributeError) as exc:
        raise SystemExit(f"Pipeline factory {spec} isn't available yet ({exc}).")


DEFAULT_TIMEOUT = 3600.0  # per case; grounding on local models takes a while


async def run_case(case: EvalCase, *, mode: Mode, factory: Callable, recordings: Path,
                   timeout: float = DEFAULT_TIMEOUT, keep: bool = False, cache: ResearchDB | None = None) -> dict:
    tmp = Path(tempfile.mkdtemp(prefix=f"helix-eval-{case.id}-"))
    worker = None
    try:
        db = ResearchDB(tmp / "research.sqlite3")
        db.init()
        ctx = _context(db, mode, case.profile, cache)
        if mode == "live":
            pf = preflight.run(ctx.settings, ctx.providers, preflight.ProbeStore(tmp / "probes.json"),
                               synthetic=True, require_probes=False)
            if not pf.ok:
                return {"id": case.id, "system": case.system, "status": "preflight_failed", "errors": pf.errors}
        pipeline, resolvers = factory(ctx)
        started = datetime.now(timezone.utc)
        with transport.use_transport(_transport(mode, recordings)):
            worker = Worker(db, pipeline, services=ctx, max_parallel=8)
            inv_id = db.create_investigation(case.problem_text, profile=case.profile,
                                             settings_snapshot=ctx.settings.model_dump(mode="json"), synthetic=True,
                                             title=case.id)
            await worker.machine.advance(inv_id)
            waits: list[str] = []
            timed_out = False
            for _ in range(MAX_WAITS + 1):
                try:
                    await worker.run_until_idle(timeout=timeout)
                except (TimeoutError, asyncio.TimeoutError):
                    timed_out = True
                    break
                inv = db.investigation(inv_id)
                if inv["status"] != "waiting_user":
                    break
                resolver = resolvers.get(inv["waiting_for"])
                if resolver is None or len(waits) >= MAX_WAITS:
                    break
                apply = resolver(case, inv)
                if apply is None:  # the case has no scripted answer for this wait
                    break
                waits.append(inv["waiting_for"])
                await worker.machine.resolve_wait(inv_id, inv["waiting_for"], apply)
            inv = db.investigation(inv_id)
        checks = [check(db, inv) for check in E1_CHECKS]
        summary = intake_summary(db, inv)
        return {
            "id": case.id, "system": case.system, "status": "timeout" if timed_out else inv["status"],
            "outcome": inv["outcome"],
            "stage": inv["stage"], "error": (inv.get("error") or "")[:800] or None,
            "waiting_for": inv["waiting_for"], "stop_reason": inv["stop_reason"],
            "waits": waits, "seconds": round((datetime.now(timezone.utc) - started).total_seconds(), 2),
            "usage": db.usage(inv_id), "e1": [c.__dict__ for c in checks], "e1_ok": all(c.ok for c in checks),
            "intake": summary,
            "expected_misses": intake_misses(case, summary, waits + ([inv["waiting_for"]] if inv["waiting_for"] else []),
                                            inv["outcome"]),
        }
    finally:
        if worker is not None:
            await worker.stop()
        if keep:
            print(f"  kept {tmp}", flush=True)
        else:
            shutil.rmtree(tmp, ignore_errors=True)


async def run(cases: list[EvalCase], *, mode: Mode, factory: Callable, name: str,
              recordings_root: Path = RECORDINGS_DIR, results_dir: Path = RESULTS_DIR,
              timeout: float = DEFAULT_TIMEOUT, keep: bool = False, cache: ResearchDB | None = None) -> dict:
    """Runs the cases one by one; the results file is rewritten after each case, so a crash keeps the rest.
    `cache`: a shared model-answer cache (live runs use eval/results/llm_cache.sqlite3 unless disabled)."""
    recordings = Path(recordings_root) / name
    results: list[dict] = []
    report: dict = {}
    for i, c in enumerate(cases, 1):
        print(f"[{i}/{len(cases)}] {c.id} …", flush=True)
        results.append(await run_case(c, mode=mode, factory=factory, recordings=recordings, timeout=timeout,
                                      keep=keep, cache=cache))
        print(f"[{i}/{len(cases)}] {c.id}: {results[-1]['status']} in {results[-1].get('seconds', 0)} s", flush=True)
        report = {"name": name, "mode": mode, "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                  "cases": results, "summary": {
                      "cases": len(results), "complete": sum(r["status"] == "complete" for r in results),
                      "e1_failures": sum(not r.get("e1_ok", False) for r in results)}}
        Path(results_dir).mkdir(parents=True, exist_ok=True)
        (Path(results_dir) / f"{name}-{mode}.json").write_text(json.dumps(report, indent=2), "utf-8")
    return report


def compare(old: dict, new: dict) -> list[str]:
    """Per-case differences in status, outcome and E1 results (for the replay comparison in a PR)."""
    before = {c["id"]: c for c in old.get("cases", [])}
    lines = []
    for case in new.get("cases", []):
        prev = before.get(case["id"])
        if prev is None:
            lines.append(f"{case['id']}: new case")
            continue
        for key in ("status", "outcome", "e1_ok"):
            if prev.get(key) != case.get(key):
                lines.append(f"{case['id']}: {key} {prev.get(key)} → {case.get(key)}")
    for missing in sorted(set(before) - {c["id"] for c in new.get("cases", [])}):
        lines.append(f"{missing}: no longer run")
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Helix research eval runner")
    parser.add_argument("--mode", choices=("fake", "replay", "live"), required=True)
    parser.add_argument("--case", help="run one case id")
    parser.add_argument("--cases", default=str(CASES_DIR))
    parser.add_argument("--name", default="default", help="recordings and results name")
    parser.add_argument("--pipeline", default=DEFAULT_FACTORY, help="module:function pipeline factory")
    parser.add_argument("--compare", help="an earlier results JSON to compare against")
    parser.add_argument("--stop-at", help="end each run when it reaches this stage (e.g. planning)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="seconds per case")
    parser.add_argument("--keep", action="store_true", help="keep each case's data folder for inspection")
    parser.add_argument("--no-shared-cache", action="store_true",
                        help="live runs: don't reuse model answers from earlier runs (eval/results/llm_cache.sqlite3)")
    args = parser.parse_args(argv)
    cases = load_cases(Path(args.cases), args.case)
    if not cases:
        print("No cases found.")
        return 1
    report = asyncio.run(run(cases, mode=args.mode, factory=stopping(load_factory(args.pipeline), args.stop_at), name=args.name,
                             timeout=args.timeout, keep=args.keep,
                             cache=shared_cache() if args.mode == "live" and not args.no_shared_cache else None))
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(errors="replace")
    for case in report["cases"]:
        print(f"{case['id']:<28} {case['status']:<14} {str(case.get('outcome')):<22} e1={'ok' if case.get('e1_ok') else 'FAIL'}")
    print(json.dumps(report["summary"]))
    if args.compare:
        for line in compare(json.loads(Path(args.compare).read_text("utf-8")), report) or ["no differences"]:
            print(line)
    return 0 if report["summary"]["e1_failures"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
