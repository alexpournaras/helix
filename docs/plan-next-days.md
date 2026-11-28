# Plan: 2026-11-28 to 2026-12-02

Working plan for the next five days (rewritten 2026-11-28 for PLAN v8, the token budget). `PLAN.md` stays the
source of truth; this file only orders the work. Tick items in `docs/progress.md` as usual; update this file when
the plan changes.

**Rules for every day**
- The daily OpenAI limit is 2.5M tokens (input + output) and can't be exceeded; runs stop at **2.4M**. A stopped
  run resumes next day from the model-answer cache. Ask before each live run.
- A day is either a **case day** (one full live case, ≤ 2.4M) or a **check day** (stage checks with `--stop-at`
  and small overlays). See PLAN B17.
- No long local GPU runs: Ollama only for short runs (≤ ~15–20 min), with a time estimate first.
- The rest of the day is code and docs without model calls (fake-model tests, replays).

| Day | Live (OpenAI, ≤ 2.4M) | Offline (no model calls) |
|---|---|---|
| **Sat 11-28** | X3 (`x3_dual_extraction run`): ~0.5M. It decides whether single extraction (B17) loses claims | PLAN v8 + `QUALITY.md` (done). 1R.1 budget profile and stage budgets. Hand-pair X3 claims, `score` |
| **Sun 11-29** | 1C.4 eval on 3 cases on the `budget` profile, `--stop-at planning`: estimate after 1R.1 (grounding with 2 sources) | 1R.2 grounding reuse, 1R.3 single screener and ranking |
| **Mon 11-30** | Check day: one case `--stop-at reading` on `budget` (~0.5–0.8M) to measure tokens per stage | 1R.4 reading and verification in budget, 1R.5 budget eval |
| **Tue 12-01** | **First case day:** one case through gaps on `budget` (target ≤ 1.4M before the report). X6 from its numbers | 1F: synthesis, one critic + code checks, revision, final check (fake-model tests) |
| **Wed 12-02** | Report stages on the same case (cache resumes the rest) | 1F finalize; 1G UI tabs and privacy basics |

**After these five days:** 1R done, X3 and X6 done, one case run live on the `budget` profile through its report,
1F built, 1G started.

**Still open before the M1 gate (1H):** finish 1G; full investigations on ≥ 5 cases from 5 body systems (one case
day each); `build_pack.py` live for the tinnitus pack within one day's budget; clinician review of the floor and the
tinnitus pack (two reviewers).

**Risks**
- The stage targets in B17 are estimates: the first case day may show a stage over its share. Then the stage
  shares move, not the 2.4M stop.
- If X3 shows single extraction misses many claims, add a second extraction only for results sections and take the
  tokens from screening.
