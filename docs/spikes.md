# Spikes (PLAN 1A.0, 1A.11)

Live runs against public APIs on 2026-11-23. Only medical vocabulary was sent (no case text). Scripts in
`backend/eval/spikes/`, raw results in `backend/eval/spikes/results/` (git-ignored). Re-run from `backend/` with
`.venv/Scripts/python -m eval.spikes.<name>`.

| # | Result | Pass |
|---|---|---|
| X1 | **Not run.** Needs the real step schemas (CaseBrief, ClaimSet, VerifyBatch, KnowledgeExtraction, Report), which are written in 1C–1F. Run then with the `dev` models; the `quality` models when keys exist. Probe-level structured output already works on gpt-5.4-mini and qwen3-coder (see decisions.md) | — |
| X4 | Europe PMC allowlist confirmed; guideline or SR found for **10/10** complaints | ✅ |
| X5 | 199-ID PubMed efetch (POST) parsed **199/199** in 3.5 s; retraction and concern fields present | ✅ |
| X8 | **48/50 (96%)** complaints map to their expected system; the 2 misses are unverified descriptors | ✅ (with caveat) |

## X4 Europe PMC (`x4_europepmc.py`)

- **Allowlist:** TITLE, ABSTRACT, TITLE_ABS, KW, MESH, PUB_TYPE ("systematic review", "practice guideline"),
  SRC, LANG, OPEN_ACCESS, HAS_FT, PUB_YEAR ranges all return hits. `MESH_HEADING` and an invented field return
  **0 hits with HTTP 200**, as C7 says. The allowlist check in the queries step is required.
- **Guideline queries** (B8.3a form, last 10 years), guideline / systematic review / SR+diagnosis hits:
  tinnitus 73 / 1,203 / 254; headache 548 / 10,724; chest pain 256 / 2,662; abdominal pain 415 / 5,798;
  low back pain 67 / 3,270; cough 459 / 5,926; urticaria 102 / 1,032; dysuria 38 / 579; fatigue 853 / 19,769;
  palpitations 113 / 1,198.
- **Core records** (40, `TITLE_ABS:"tinnitus" AND (SRC:MED OR SRC:PMC)`): 31/40 with an abstract, 17/40 open
  access, 19/40 with a PMCID, `meshHeadingList` on only 13/40. Latency 0.07–12.5 s (median 5 s).

**Differences from the plan, to act on:**
1. **C8 said 39/40 abstracts and 28/40 open access; this sample gave 31/40 and 17/40.** The plan's figures came
   from another query. Prefilter rule 5 (no abstract → excluded) will remove more than expected; keep the
   guideline/seed exception.
2. **Correction types include `Expression of concern for`** (on the notice itself) next to
   `Expression of concern in` (on the affected paper) and `Retraction in`. The concern flag must use only the
   `… in` types; using `for` would flag the notices instead of the papers.
3. **The B8.3a grounding queries are too broad for common symptoms** (19,769 SRs for fatigue; the term matches
   anywhere in title/abstract). Grounding needs the `TITLE:` or MeSH-only form, or major-topic MeSH on PubMed,
   with the hit-count refinement loop. → PLAN B8.3a to be updated in 1C.3.

## X5 PubMed (`x5_pubmed.py`)

- `esearch retmax=0` returns the count with no IDs (C10 confirmed).
- One `efetch` POST with 199 IDs (tinnitus records + retracted + concern samples; 200 requested, one
  duplicate) returned 3.3 MB of XML, parsed by `pubmed.parse` without errors in 3.5 s. **200 per call is fine**;
  use POST (a GET URL with 200 IDs is near length limits).
- Retraction: publication type `Retracted Publication` (32) agrees with `CommentsCorrections RefType="RetractionIn"`
  (32). Concern: `ExpressionOfConcernIn` (22) and also `ExpressionOfConcernFor` (1); as in X4, only `…In` flags
  the paper. `pubmed.parse` does not read CommentsCorrections yet → 1A.8 must add the flags.

## X8 MeSH mapping (`x8_mesh.py`, `knowledge/mesh_systems.yaml`)

- 50 complaints across 17 systems. Lookup: exact heading, then free search. **A free-search hit is accepted only
  if the term is one of the descriptor's entry terms.** Without that rule, "palpitations" resolved to
  *Panic Disorder* (MeSH has no Palpitations descriptor): a wrong descriptor would have sent a cardiac symptom to
  mental health only. That rule goes into `mesh.py` (1A.9).
- **Pain is filed under neurologic manifestations** (`C23.888.592.612`): without location-specific rules, chest,
  abdominal and back pain all map to neurology. The table maps the pain branch to `general` and each
  location sub-branch to its system.
- Unverified: palpitations, blurred vision (no descriptor under those words). In the pipeline the intake model
  proposes descriptors (e.g. *Tachycardia*, *Vision Disorders*), and the model's own systems are unioned in (B0).
- Ambiguous symptoms accept any clinically correct system: memory loss, syncope, edema, insomnia, polyuria.

**Caveat:** the mapping table was written from these same 50 terms' tree numbers, so 96% is an in-sample
figure. Before 1A.9 is done, run a held-out set of ≥ 50 different complaints (rare conditions, paediatric,
pregnancy, poisoning). The table also needs a human review (PLAN Part G.6).
