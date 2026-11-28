# Plan: 2026-11-28 to 2026-12-02

Working plan for the next five days (rewritten 2026-11-28 for PLAN v9). `PLAN.md` stays the source of truth; this
file only orders the work. Tick items in `docs/progress.md` as usual; update this file when the plan changes.

**Rules for every day**
- The OpenAI project has 2.5M complimentary tokens per **UTC** day (resets 02:00 Athens time in winter); beyond that
  is billed. Helix's ceiling is **2.4M per UTC day**. Until the ledger (1R.0) exists, a day's planned live runs stay
  **≤ 1.5M** and run one at a time, so concurrent calls can't overshoot. Ask before each live run.
- Synthetic cases only (the project shares data with OpenAI; PLAN B17.1).
- No long local GPU runs: Ollama only for short runs (≤ ~15–20 min), with a time estimate first.
- The rest of the day is code and docs without model calls (fake-model tests, replays).

| Day (UTC) | Live (OpenAI) | Offline (no model calls) |
|---|---|---|
| **Sat 11-28** | X3 (`x3_dual_extraction run`): ~0.5M. It decides whether single extraction (B17.5 item 5) loses claims | PLAN v9 (done). **1R.0 daily quota ledger.** Hand-pair X3 claims, `score` |
| **Sun 11-29** | Probe `low` (tiny). 1C.4 eval on 2 cases on `budget` (`ent.tinnitus-left`, `neurology.headache-afternoons`), `--stop-at planning`: ~1.3M (new groundings fill the cache) | 1R.1 budget profile, targets, reserves, pool |
| **Mon 11-30** | Third 1C.4 case (`digestive.abdominal-pain-pregnancy-unknown`), `--stop-at planning`: ~0.65M | 1R.2 grounding reuse and several complaints, 1R.3 screener and ranking |
| **Tue 12-01** | Check day: `ent.tinnitus-left` `--stop-at reading` on `budget` (grounding reused from Sunday): ~0.6–0.8M | 1R.4 reading and verification (triggers, audit), 1R.5 budget eval |
| **Wed 12-02** | The same case to `gaps` (cache resumes): ~0.3M. X6 from its numbers | 1R.6 second round, 1R.7 safety clearance |

**After these five days:** 1R.0–1R.7 done, X3 and X6 done, one case live on `budget` to `gaps`. Next: 1F (report,
finalization levels, publication gates), then **1R.8**: two case days (cold and warm cache) through the report.

**Still open before the M1 gate (1H):** 1G; the case set to ≥ 15 cases across ≥ 5 systems; ≥ 5 full investigations
(one case day each); `build_pack.py` live for the tinnitus pack through the ledger; clinician review of the floor and
the tinnitus pack (two reviewers).

**Risks**
- The B17.4 targets are estimates: the first measured case may move them (decision entry), never the 2.4M cap.
- If X3 shows single extraction misses many claims, add a second extraction for results sections only, paid from
  `screen`.
