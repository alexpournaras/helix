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
- [ ] 1A.11 X3, X6 — X3 can run now (1E prompts exist); X6 from the first 1D + 1E live run
- [x] Checkpoint after 1A (judged by the agent at your request; see decisions.md)

### 1B Clinical knowledge
- [x] 1B.1 Universal safety floor: 63 rules, every area and population, 96 resolved sources, held-out vignettes 40/40 after fixes (first runs 13/20 and 15/20)
- [x] 1B.2 Tinnitus pack v0 (hand-built from NICE NG155/NG98 + NHS; 7 verified seed papers)
- [x] 1B.3 Knowledge loader, validator, conditions and merge
- [ ] Clinician review of the floor and the tinnitus pack (two reviewers, recorded in decisions.md)
- [ ] `build_pack.py` after grounding (1C.3)

### 1C–1H
### 1C Intake, safety, grounding, questions
- [x] 1C.1 Intake (generate + code checks + brief check + MeSH; populations; rewrite path; 5 synthetic eval cases pass live on `dev`)
- [x] 1C.2 Safety (floor + pack rules, panel, guideline red flags, rule-outs, safety rule, `safety_finalize`, crisis resources)
- [ ] 1C.3 Grounding (+ X9, `build_pack.py`): built and live-tested on `dev_openai`; X9 run 1 87.5% recall (fail) → extraction v3 / verify v2; run 2 (2026-11-27) passed (recall 80/80, attestations 96.4%); `build_pack.py` left
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
- [ ] 1F Report
- [ ] 1G UI and privacy
- [ ] 1H M1 gate

## Waiting for live runs

Daily OpenAI limit: 2.5M tokens (input + output); runs stop at 2.4M. A stopped run resumes the next day from the
model-answer cache. Estimates (2026-11-27) from the call logs of the 11-25 eval runs and X9 run 2:

1. ~~X9 run 2~~ done 2026-11-27: recall 80/80 = 100% (pass), 2.13M tokens.
2. ~~X9 attestation hand review~~ done 2026-11-27: 163/169 = 96.4% (pass).
3. Live eval for 1C.4 (`python -m eval.run --mode live --stop-at planning`): questions, answers and the brief update
   on the synthetic cases. **~2.1–3.1M** for all 8 cases: intake + safety ~20k per case (mostly cached), grounding
   0.3–0.55M per case for the 5 cases that reach it (11-25 runs), questions ~30k. Run 2–3 cases a day
   (`--case`), or all 8 over two days.
4. Live check of 1D and 1E on `dev_openai` (one case). With the quality structure (`max_screened` 6000, dual
   screening in batches of 8, 25 full-text papers per round, up to 5 rounds, ~0.3–0.5M per paper read) a full run
   is **~10–20M+**, several days. So it runs with `--settings eval/limits/live_check.yaml` (≤ 6 hypotheses, 3
   queries each, 200 screened, 3 papers, 1 round, no snowball, `guards.max_tokens` 2M): `--stop-at reading`
   ~0.8–1.2M, then `--stop-at synthesis` ~1.5–2.5M.
5. X3 (dual extraction on 6 papers; script not written yet): **~0.8–1.5M**, ~2M with the verification panel.
6. X6 (cost and time): no extra calls, written up from the item 4 numbers.
7. X1 (structured output, after 1F): the `dev_openai` part ~0.5–1.5M. The rest uses the quality models, not this
   limit.
