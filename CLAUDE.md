# Helix — agent guide

**Read the PLAN.md task first.** `PLAN.md` is the source of truth for Helix Investigations (the research
pipeline). Before coding a task, read the task in Part D, the B-sections it names and the related Part C facts.
Follow the execution protocol in PLAN.md Part G.
Every case must fit in one day's token budget (PLAN B17: stop at 2.4M of the 2.5M daily OpenAI limit).
`QUALITY.md` is the archived quality-first plan (v7), kept for reference only.

## Commands

| Command | What it does |
|---|---|
| `npm run setup` | Install Node and Python dependencies |
| `npm run dev` | API on :8000, web on :5173 |
| `npm test` | All backend tests (`backend/test_*.py`, unittest) |
| `npm run build` | Build the frontend |
| `npm run research:doctor` | Research health check (keys, models, privacy, DB) |

A single test module: `cd backend && .venv/Scripts/python -m unittest test_research_db -v`.

## Conventions

- Research code lives in `backend/app/research/` (layout: PLAN B2). Tests are `backend/test_research_*.py`
  and use `research_testkit.ResearchTestCase` (temp data folder, fake keys, no-network guard).
- **No real network calls in tests.** Use `httpx.MockTransport`, the fake provider, or recorded replays.
  Live checks only through `eval/run.py --mode live`.
- Tests first for deterministic code. Never weaken a test.
- Prompts and thresholds are versioned; a change needs a version bump and an `eval replay` comparison.
- New ideas → `docs/backlog.md`. Changes to the plan → `docs/decisions.md` (and update PLAN.md in the same PR).
- Done = tests pass, build passes, the task's done-when is met, docs updated, `docs/progress.md` ticked.
- Knowledge files (`knowledge/universal/*.yaml`, `knowledge/packs/*.yaml`) are content: agents may draft them,
  only a human merges them, and every source must resolve.
- All work goes on the `development` branch (one commit per task, named by task ID); no per-task branches.

## Privacy rules

- No health text in logs, events, fixtures or test data. Synthetic cases only.
- Logs and events contain IDs and counts, never problem text, answers or prompts (a canary test checks this).
- `store_prompts` is off by default. Real cases only go to provider projects without data sharing.
- The research API refuses requests unless the server is bound to loopback (or `HELIX_ALLOW_REMOTE=1`).
- Never commit `backend/.env` or anything under `backend/data/`.
