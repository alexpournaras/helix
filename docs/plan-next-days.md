# Plan: 2026-11-28 to 2026-12-02

Working plan for the next days (updated 2026-11-28 evening, PLAN v9). `PLAN.md` stays the source of truth; this file
only orders the work. Tick items in `docs/progress.md` as usual; update this file when the plan changes.

**Rules for every day**
- The OpenAI project has 2.5M complimentary tokens per **UTC** day (resets 02:00 Athens time in winter); beyond that
  is billed. Helix's ceiling is **2.4M per UTC day**, enforced by the ledger (`npm run research:quota`) since 1R.0.
  Ask before each live run.
- Synthetic cases only (the project shares data with OpenAI; PLAN B17.1).
- No long local GPU runs: Ollama only for short runs (≤ ~15–20 min), with a time estimate first.
- The rest of the day is code and docs without model calls (fake-model tests, replays).

**Done on Sat 11-28** (ahead of the original plan): PLAN v9; 1R.0 ledger; 1R.1 budget profile; 1R.2 grounding reuse;
1R.3 ranking; 1R.4 reading and verification (code; X3 re-run 24/27 = 89% with `read.claims.v2`); 1R.5 budget report
(code); 1R.6 second round; 1R.7 safety clearance. X3 run (IDs 100%, reconciliation 73%). 1C.4 eval on 2 budget cases
to planning (tinnitus 169k, headache 46k tokens). Live check 1R.5 (a) to `reading` started.

| Day (UTC) | Live (OpenAI, ≤ 2.4M via the ledger) | Offline (no model calls) |
|---|---|---|
| **Sun 11-29** | 1R.5 (b): the tinnitus case to `gaps` (reading, verification, assessment; the cache resumes the rest), ~0.8–1.2M. X6 from its numbers. Third 1C.4 case (`digestive.abdominal-pain-pregnancy-unknown`) if room | Fixes from the live check; adjust B17.4 targets by a decision if a group is over. Start 1F: synthesis (one writer), combined critic, batched support check |
| **Mon 11-30** | Re-runs after fixes (replay first) | 1F: revision, final check, finalization levels (full / compact / record), publication gates, ranking withheld on `unresolved` clearance |
| **Tue 12-01** | First report on the cached tinnitus case (report stages only) | 1F finalize; tests end to end with fake models |
| **Wed 12-02** | **1R.8 case day 1:** a new complaint from start to report (cold grounding cache) | 1G: research API (incl. the knowledge-cache clear endpoint), UI tabs |
| **Thu 12-03** | **1R.8 case day 2:** a complaint with cached grounding (warm) | 1G |

**Still open before the M1 gate (1H):** 1G; the case set to ≥ 15 cases across ≥ 5 systems; ≥ 5 full investigations
(one case day each); `build_pack.py` live for the tinnitus pack through the ledger; clinician review of the floor and
the tinnitus pack (two reviewers).

**Risks**
- `read.claims.v2` lists ~4× more claims than v1: the `read` group (650k target) and verification may come out over.
  Levers, in order: results-only claims (no background), fewer claims per pack, then fewer papers.
- The B17.4 targets are estimates; the 2.4M cap and the daily ceiling don't move.
