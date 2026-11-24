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
- [ ] 1B.1 Universal safety floor
- [ ] 1B.2 Tinnitus pack v0
- [ ] 1B.3 Knowledge loader, validator and merge

### 1C–1H
- [ ] 1C Intake, safety, grounding, questions
- [ ] 1D Search
- [ ] 1E Reading and evidence
- [ ] 1F Report
- [ ] 1G UI and privacy
- [ ] 1H M1 gate
