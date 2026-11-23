# Decisions

Changes to PLAN.md and to fixed targets are recorded here (PLAN Part G.5, E5).

| Date | Topic | Decision | Why |
|---|---|---|---|
| 2026-11-23 | Test runner | `npm test` runs `unittest discover` over `backend/test_*.py`; no pytest dependency | The existing suite is unittest; avoids a new dependency (C24: no upgrades) |
| 2026-11-23 | Branching for 1A | 1A tasks are built on one branch `feat/research-1a-foundations`, one commit per task | The tasks are tightly coupled (DB ← settings ← model layer ← worker); a PR per commit is still possible |
| 2026-11-23 | Roles not named in B6 | Added `grounding.queries` (A sol), `plan.consolidate` (A astra), `read.reconcile` (A sol, borderline-pair judge) as explicit roles; `adjudicate` / `confirm` resolve at run time from `strongest.{family}` | B8.3a, B8.6 and B8.14 name these models but B6 lists them inside other rows |
| 2026-11-23 | Development models | New `dev` profile: family A = `gpt-5.4-mini`, family B = local Ollama `qwen3-coder:latest` (30B), same ensemble structure as `quality`. Saved as the local active profile. The Ollama structured adapter (planned for M2) is pulled forward. `quality` stays the code default | Your instruction: use GPT-5.4 mini and Ollama for now. Pre-flight warns that this profile is not for real cases (models haven't passed E3; Ollama in critical roles) |
| 2026-11-23 | Probe results (live, no health text) | gpt-5.4-mini: structured output OK; accepts effort none/low/medium/high/xhigh; ~2.5 s. qwen3-coder:latest: structured output OK; no thinking capability; ~36 s on first load | Recorded in `backend/data/research_probes.json` |
