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
- [ ] 1A.11 X3, X9, X6 — wait for the extraction prompts/schemas (1C.3, 1E) and a running pipeline
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
- [ ] 1C.3 Grounding (+ X9, `build_pack.py`): built and live-tested on `dev_openai`; X9 run 1 87.5% recall (fail) → extraction v3 / verify v2, run 2 pending
- [x] 1C.4 Questions and brief update (fake-model tests; live check with tomorrow's eval run)
- [x] 1D Search (fake-model and fake-source tests; live check pending)
  - [x] 1D.1 Planning: two planners, consolidation, knowledge coverage, `plan_approval` (fake-model tests; live check pending)
  - [x] 1D.2 Queries: allowlists, MeSH and knowledge vocabulary, hit counts, refinement (fake-model tests; live check pending)
  - [x] 1D.3 Search and seed, prefilter, dual screening, select, fetch
- [ ] 1E Reading and evidence
- [ ] 1F Report
- [ ] 1G UI and privacy
- [ ] 1H M1 gate

## Waiting for live runs

One full X9 run uses about a day's OpenAI limit, so these go one per day, in this order:
1. X9 run 2 (`python -m eval.spikes.x9_knowledge run`, then `score`): extraction v3 + verify v2. Pass: ≥ 90% recall.
2. X9 attestation hand review (`results/x9/attestations.md` → `attestation_review` in `x9_gold.yaml`). Pass: ≥ 95%.
3. Live eval for 1C.4 (`python -m eval.run --mode live --stop-at planning`): questions, answers and the brief update on the synthetic cases. Without `--stop-at` it also runs planning (1D.1), which is the next check.
4. Live check of 1D on `dev_openai`: `eval.run --mode live --stop-at reading` runs intake → fetch (planning, queries, real searches, screening). Start with one case (`--case`) to see the cost.
