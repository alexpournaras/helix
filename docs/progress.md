# Progress

Ticked when the task's definition of done (PLAN Part G.8) is met.

## M1: Evidence slice

### 1A Foundations
- [x] 1A.0 X4, X5, X8 run and passed (`docs/spikes.md`); X8 held-out check (X8b) passed at 98%
- [ ] 1A.0 X1 — waits for the step schemas (1C–1F)
- [x] 1A.1 Database (full B3 schema, migrations, task queue, usage) + ID formats
- [x] 1A.2 Settings and profiles
- [x] 1A.3 Strict schema
- [x] 1A.4 Providers and model layer, probes, pre-flight (+ C19 fix)
- [x] 1A.5 Ensembles
- [x] 1A.6 Worker and pipeline (lock, guards, stage machine, controls; dummy-pipeline tests)
- [x] Extra: `dev` profile (gpt-5.4-mini + Ollama) and Ollama structured adapter (pulled forward from M2 2.10)
- [x] 1A.7 Segmentation and packing (`segment.py`, `packing.py`)
- [x] 1A.8 Literature additions (flags, `search_records`, `count`, `fetch_many`, `enrich(citations=False)`, `find_existing`, `tag_origin`)
- [x] 1A.9 MeSH module (`mesh.py`; recorded fixtures; X8b held-out 98%). mapping table audited and corrected (v2)
- [x] 1A.10 Notes basics, test infrastructure, eval runner (`eval/run.py`), doctor, log canary
- [x] 1A.11 X7 passed (99.2%, `seg.v2`)
- [x] 1A.11 X9 passed (run 2: recall 80/80, attestations 163/169 = 96.4%)
- [x] 1A.11 X3 run 2026-11-28: valid IDs 100% (pass); reconciliation 11/15 = 73% (fail, abstract vs body citations; `quality` only); one extractor alone finds 21/27 = 78% → `read.claims.v2` coverage prompt in 1R.4 (docs/spikes.md)
- [ ] 1A.11 X6 — from the 1R.5 live check
- [x] Checkpoint after 1A (judged by the agent at your request; see decisions.md)

### 1B Clinical knowledge
- [x] 1B.1 Universal safety floor: 63 rules, every area and population, 96 resolved sources, held-out vignettes 40/40 after fixes (first runs 13/20 and 15/20)
- [x] 1B.2 Tinnitus pack v0 (hand-built from NICE NG155/NG98 + NHS; 7 verified seed papers)
- [x] 1B.3 Knowledge loader, validator, conditions and merge
- [ ] Clinician review of the floor and the tinnitus pack (two reviewers, recorded in decisions.md)
- [ ] `build_pack.py` after grounding (1C.3): built 2026-11-27 with fake-model tests; live run for the tinnitus draft pending

### 1C Intake, safety, grounding, questions
- [x] 1C.1 Intake (generate + code checks + brief check + MeSH; populations; rewrite path; 5 synthetic eval cases pass live on `dev`)
- [x] 1C.2 Safety (floor + pack rules, panel, guideline red flags, rule-outs, safety rule, `safety_finalize`, crisis resources)
- [ ] 1C.3 Grounding (+ X9, `build_pack.py`): built and live-tested on `dev_openai`; X9 run 1 87.5% recall (fail) → extraction v3 / verify v2; run 2 (2026-11-27) passed (recall 80/80, attestations 96.4%); `build_pack.py` built, live run pending
- [x] 1C.4 Questions and brief update (fake-model tests; live check with tomorrow's eval run)
- [x] 1D Search (fake-model and fake-source tests; live check pending)
  - [x] 1D.1 Planning: two planners, consolidation, knowledge coverage, `plan_approval` (fake-model tests; live check pending)
  - [x] 1D.2 Queries: allowlists, MeSH and knowledge vocabulary, hit counts, refinement (fake-model tests; live check pending)
  - [x] 1D.3 Search and seed, prefilter, dual screening, select, fetch
- [x] 1E Reading and evidence (fake-model tests; live check pending)
  - [x] 1E.1 Reading: profile, appraisal panel, study identity, dual extraction, whole-paper pass, reconciliation, applicability (fake-model tests)
  - [x] 1E.2 Verification panel, escalation, adjudication, re-read (fake-model tests)
  - [x] 1E.3 Map, group, confirm (fake-model tests)
  - [x] 1E.4 Matrix (code; unit tests for every B8.19 rule)
  - [x] 1E.5 Fit and analysis (fake-model tests)
  - [x] 1E.6 Gaps panel (reporting only; fake-model tests)
- [ ] 1R Budget rework (PLAN B17 v9, decisions 2026-11-28): one case within 2.4M, ledger-enforced daily ceiling
  - [x] 1R.0 Daily quota ledger: `quota.py`, reservation before every provider call, `npm run research:quota`, doctor line, worker waits for the UTC reset (14 tests); model snapshot `gpt-5.4-mini-2026-03-17` recorded
  - [x] 1R.1 Budget profile (default; B17.2 values and B17.3 roles with per-role output caps), stage groups, acquisition rule with protected reserves, `budget_skip` / `budget_over` events, B10 rule ignores budget skips, privacy modes, `same_model` label; eval cases on `budget`; `low` effort probed live (accepted, 243 tokens via the ledger)
  - [x] 1R.2 Grounding reuse (`knowledge_cache`, schema 2; hit = no model calls; `ok` 180 days, `gap` 30 days only after a full search; operational failures not cached) and the 2-new-complaints cap with the "not grounded: token budget" gap and `broad_case` event; doctor line + `--clear-knowledge-cache` (7 tests)
  - [x] 1R.3 Code ranking before screening (`screening.prerank`, `round_caps` 300/100) in the prefilter; one screener, batches of 20 (2 tests)
  - [ ] 1R.4 Reading and verification in budget (triggers, audit)
  - [ ] 1R.5 Budget eval (+ X6)
  - [ ] 1R.6 Second round from the pool
  - [ ] 1R.7 Safety clearance
  - [ ] 1R.8 End-to-end quota validation (after 1F; cold and warm cache)
- [ ] 1F Report
- [ ] 1G UI and privacy
- [ ] 1H M1 gate

## Waiting for live runs

Daily limit (PLAN B17.1): 2.5M complimentary tokens per **UTC** day, Helix ceiling **2.4M**, enforced by the ledger
(`npm run research:quota`) since 1R.0. Synthetic cases only. One live block per day, asked first; the day-by-day
order is in `docs/plan-next-days.md`. Estimates below are for the `budget` profile (B17.4) unless marked.

| # | Run | Tokens | Status |
|---|---|---|---|
| 1 | X9 run 2 + attestation review | 2.13M | done 2026-11-27 (recall 80/80, attestations 96.4%) |
| 2 | X3 dual extraction + hand pairing + `score` | 291k | done 2026-11-28 (see 1A.11) |
| 2b | X3 single-extraction re-run with `read.claims.v2` (1R.4) | ~0.15M | with 1R.4 |
| 3 | Probe `low` effort on `gpt-5.4-mini` | 243 | done 2026-11-28 (accepted) |
| 4 | 1C.4 eval on `budget`, `--stop-at planning`: 3 cases over two days (grounding fills the cache) | ~0.65M per new complaint | after 1R.1 |
| 5 | One case `--stop-at reading`, then to `gaps` (1R.5) | ~0.6–0.8M, then ~0.3M | after 1R.4 |
| 6 | X6 (cost and time) | none: written up from item 5 | after item 5 |
| 7 | 1R.8: two full cases to the report (cold and warm cache), one case day each | ≤ 2.4M each | after 1F |
| 8 | `build_pack.py` for the tinnitus pack draft (resumes across days through the ledger) | ~2–3M (`dev_openai`) | when a day is free |
| 9 | X1 structured output | ~0.5–1.5M | after 1F |
