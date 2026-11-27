# Plan: 2026-11-28 to 2026-12-02

Working plan for the next five days (written 2026-11-27). `PLAN.md` stays the source of truth; this file only
orders the work. Tick items in `docs/progress.md` as usual; update this file when the plan changes.

**Rules for every day**
- One block of OpenAI runs per day, capped at **2.4M tokens** (daily limit 2.5M; gpt-5.4-mini ≈ $7–8 per full
  day). Stopped runs resume next day from the model-answer cache. Ask before each live run.
- No long local GPU runs: Ollama only for short runs (≤ ~15–20 min), with a time estimate first.
- The rest of the day is code and docs without model calls (fake-model tests, replays).
- Token estimates come from `docs/progress.md` → "Waiting for live runs".

| Day | Live (OpenAI, ≤ 2.4M) | Offline (no model calls) |
|---|---|---|
| **Sat 11-28** | 1C.4 eval on 3 cases (`ent.tinnitus-left`, `neurology.headache-afternoons`, `digestive.abdominal-pain-pregnancy-unknown`), `--stop-at planning`: ~1–1.6M. X3 run (`x3_dual_extraction run`): ~0.5M | Hand-pair X3 claims (`results/x3/pairs.md` → `x3_gold.yaml`), `score`. `build_pack.py` built 2026-11-27 (fake-model tests); its live run (~2–3M on dev_openai) gets a day of its own when the budget allows |
| **Sun 11-29** | First 1D + 1E live check, one case, `--settings eval/limits/live_check.yaml --stop-at reading`: ~0.8–1.2M; then continue towards `--stop-at synthesis` with what's left (cache resumes) | Fixes from 1C.4 and X3 (prompt changes: version bump + `eval replay` comparison). Start 1F: ranking and synthesis |
| **Mon 11-30** | Finish 1D + 1E to synthesis: ~1–1.3M. Write up X6 (cost and time) from that run | 1F: critic panel, support check, dose regex, revise and recheck loop (fake-model tests) |
| **Tue 12-01** | 1C.4 on the remaining 5 cases, or re-runs after fixes: ~1.5–2M | 1F: final check, finalize (outcomes, record mode, readiness banner); pipeline end to end with fake models |
| **Wed 12-02** | First live run through 1F on one case (live_check limits; estimate after Tue's fake runs) | 1G: UI tabs (Knowledge, coverage, votes, readiness) and privacy basics |

**After these five days:** 1C fully live-checked, X3 and X6 done, 1D + 1E run live once, 1F built and run once,
1G started.

**Still open before the M1 gate (1H):** finish 1G; run `build_pack.py` live for the tinnitus pack (strongest
models: needs the quality keys or a budget decision); clinician review of the floor and the tinnitus pack (two
reviewers); E5 on cases from ≥ 5 body systems.

**Risks**
- 1D + 1E has never run live: the first runs may show bugs or unexpected costs and take Sun–Mon for fixes.
- The 2.5M daily limit means about one live check per day; a higher limit would roughly halve this timeline.
