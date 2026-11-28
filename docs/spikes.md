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
| X7 | Paper splitter on **240 sentences** of open-access text (6 papers, 3 systems): **238/240 (99.2%)** correct with `seg.v1`; the one error fixed in `seg.v2` | ✅ |
| X9 | Run 1 (2026-11-26, `grounding.extract.v2` + `verify.v1`, `dev_openai`): red-flag recall **70/80 (87.5%)**; attestations not judged (see X9 below). Fixed in `extract.v3` + `verify.v2`. Run 2 (2026-11-27): recall **80/80 (100%)**; red-flag attestations supported **163/169 (96.4%)** by hand review | ✅ run 2 |
| X3 | Run 2026-11-28 (`read.claims.v1`, dual, `dev_openai`, 16 calls, 291k tokens): valid IDs **100%** per extractor (pass); reconciliation **11/15 (73.3%)** hand pairs (fail, abstract-vs-body citations). Each extractor alone found **21 of 27 (78%)** distinct findings: the budget profile's single extraction needs a coverage prompt (see X3 below) | ✅ IDs / ❌ reconciliation |
| X6 | **Not run.** Needs a running pipeline (the first 1D + 1E live check) | — |
| X8b | Held-out set (83 new complaints incl. rare, paediatric, pregnancy, poisoning): **80/82 verified (98%)** correct, 1 unverified (96% overall) | ✅ |

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

## X8b MeSH held-out check (`x8b_mesh_heldout.py`, 2026-11-24)

Run through `app/research/mesh.py` (the real lookup rules) on 83 complaints that weren't used to write
`mesh_systems.yaml`, across all 21 systems, with paediatric, pregnancy, poisoning/injury and rare conditions.
The expected labels were written by the agent **before** the run and need a human check (PLAN Part G.6).

- **Verified 82/83.** Unverified: *malaise* (neither the heading search nor an entry-term match found a descriptor). As in X8, the intake model's proposed systems are unioned in, so an unverified term isn't a dead end.
- **Correct 80/82 verified (98%), 96% overall. Pass.**
- **Misses, for the table review (not fixed, to keep this run held-out):**
  1. *Renal colic* → `general` only. Its trees are `C23.888.592.612.972` (an unmapped pain sub-branch) and
     `C23.888.721` (colic). Candidate rule: map that pain sub-branch to `urogenital` once a reviewer confirms
     which branch `.972` is.
  2. *Colic* → `congenital_neonatal` (`C16.614.166`, infant colic). Arguably correct for the paediatric case the
     descriptor describes; the expected label may be what's wrong.
- **Systems that come along with the expected one** (not errors, worth knowing): facial paralysis → mouth_dental;
  toothache → ent (the earache pain branch); breast neoplasms → skin (`C17.800` breast diseases);
  myasthenia gravis → oncology (thymoma branch); gout → congenital_neonatal (inborn errors). Herpes zoster maps
  to infection only, croup to ent + respiratory (not infection).

## X7 sentence splitter (`x7_splitter.py`, 2026-11-24)

- Six open-access research articles from PMC (ENT: PMC13662099, PMC13654706; neurology: PMC13657683,
  PMC13657970; skin: PMC13657800, PMC13744600), 40 pieces each from methods/results paragraphs: 240 pieces,
  every one checked by hand.
- **238/240 correct (99.2%). Pass (≥ 97%).** The only error: "IBM SPSS Statistics ver." | "30 (IBM SPSS, Tokyo,
  Japan) was used…": "ver." before a number. Fixed in `seg.v2` (with "v." the same way), plus a regression test.
- Handled correctly: author-year and numbered citations, "e.g.,", "et al.", "No. 2019-11-035-001", "St. Olavs",
  decimals, ± and CIs, "p = 0.018;", "vs.", nested brackets, "Pvt. Ltd." inside brackets, "°C", "≥ 18".
- Not covered by this sample (watch in E1/E3): PDF text (M2), Greek papers, tables.

### Table audit and fixes (2026-11-24, after X8b)

Every prefix in `mesh_systems.yaml` was checked against its live MeSH heading (in place of a separate human
review, by your instruction). Findings and changes (table version 2):
- `C23.888.592.612.330` is **Facial Pain**, not earache (the comment was wrong): now `neurology`, with Toothache
  (`.330.500`) → `mouth_dental` and Earache (`.302`) → `ent` added.
- Renal Colic (`.972`) → `urogenital` added (X8b miss 1).
- *Colic* is MeSH's infant colic (`C16.614.166`), so `congenital_neonatal` is right: the X8b label was widened
  (X8b miss 2 was a label error).
- `C13` no longer exists in current MeSH (merged into C12); kept for old tree numbers.
- Pelvic Pain stays `female_reproductive`; male pelvic pain gets its system from the model's proposed systems.

After the fixes: X8 48/50 (unchanged), X8b 82/82 verified. **The honest held-out figure is the 98% before the
fixes;** the 100% is in-sample.

## Universal floor vignettes (`floor_vignettes.py`, 2026-11-24)

Synthetic vignettes written separately from the rules' own examples. An emergency vignette passes when an
emergency rule fires or is unknown (its rule-out question is asked); an everyday vignette passes when no
emergency rule fires.

| Set | First run | After fixes |
|---|---|---|
| Set 1 (20 emergencies, 15 everyday) | **13/20** emergencies caught; 1 false trigger ("hay fever" matched fever) | 20/20, 0 false |
| Set 2 (written after the set-1 fixes) | **15/20** caught, 0 false | 20/20, 0 false |

The first-run misses were all wording variants ("pain in the middle of his chest", "drank some bleach",
"feels like it's closing", "killing myself", "temperature is 38.6"), fixed with proximity patterns. **The honest
estimate is the first-run catch rate, about 70–75% on new wording**; the rule-based floor is one layer, the
safety panel (B8.2) the second. The real measure is the E3.7 triage benchmark (Semigran vignettes) in 1C.2.


## X9 Knowledge extraction (`x9_knowledge.py`)

Run 1, 2026-11-26: 6 guidelines, every pack, dual extraction + verification, profile `dev_openai`
(334 live calls, ~1.0M input and ~1.05M output tokens: about one day's OpenAI limit). Results in
`results/x9/extraction.run1.json`. Matching to the gold list was done by hand (an item matches a gold flag when its
text or `features` name it; only `red_flag` items count).

| Guideline | Gold | Matched | Missed |
|---|---|---|---|
| PMC6734476 headache (EHF) | 20 | 20 | — |
| PMC13405573 headache (Singapore) | 13 | 11 | pattern change / recent onset; painkiller overuse (both only as history items or a differential) |
| PMC8870422 back pain, children | 19 | 18 | pre-existing or concomitant conditions (only as a history item) |
| PMC11364714 acute vertigo | 11 | 8 | Horner syndrome, visual field defects, central HINTS pattern (dropped from the checklist bundles; HINTS only as an evaluation) |
| PMC9380508 dyspepsia (BSG) | 8 | 4 | ≥55 treatment-resistant; ≥55 raised platelets or nausea/vomiting; ≥55 abdominal pain + low Hb; >40 high-risk area or family history (referral criteria came out as `evaluation` items) |
| PMC8831363 dyspepsia (Japan) | 9 | 9 | — |
| **Total** | **80** | **70 (87.5%)** | below 90% |

Counting any item kind, 76/80 would be covered; the safety panel only sees `red_flag` items, so the strict count is
the result.

**Verification:** red-flag attestations were judged `partial` 75/110 times (other kinds ~10%). The verifier's
reasons show why: the claim it checked ended with "— urgency: urgent", the extractor's own classification, which
guidelines rarely state. So the model verdicts can't stand in for the hand review.

**Fix (PLAN 1A.11: "Prompts"):** `grounding.extract.v3` makes anything called a red flag / warning / alarm feature
a red flag (exam findings and medication overuse included), turns each referral or urgent-test criterion into a
red flag with its age threshold, and requires every feature of a warning list to appear. `grounding.verify.v2` no
longer verifies the urgency. Run 2 (then the attestation hand review) is the next live step.

Run 2, 2026-11-27: `grounding.extract.v3` + `verify.v2`, same set-up (320 live calls + 15 cache hits, ~1.0M input
and ~1.1M output tokens = 2.13M). Results in `results/x9/extraction.run2.json`; matches recorded as `matched_by` in
`x9_gold.yaml` (by hand, same rule as run 1).

| Guideline | Gold | Matched | Extracted red flags |
|---|---|---|---|
| PMC6734476 headache (EHF) | 20 | 20 | 60 |
| PMC13405573 headache (Singapore) | 13 | 13 | 22 |
| PMC8870422 back pain, children | 19 | 19 | 29 |
| PMC11364714 acute vertigo | 11 | 11 | 11 |
| PMC9380508 dyspepsia (BSG) | 8 | 8 | 20 |
| PMC8831363 dyspepsia (Japan) | 9 | 9 | 12 |
| **Total** | **80** | **80 (100%)** | 154 |

All of run 1's misses are now red flags (the BSG referral criteria with their age thresholds; Horner syndrome,
visual field defects and the central HINTS pattern as features of the vertigo checklist items). The model verifier
judged red-flag attestations supported 163/169 (partial 6), down from 75/110 partial in run 1.

**Attestation hand review** (agent, against the snapshot text; `attestation_review` in `x9_gold.yaml`): **163/169
supported (96.4%, pass ≥ 95%)**, 6 partial, 0 unsupported. The model's verdicts agree on 4 of the 6 partials (it
also marked the two pregnancy/puerperium items, which the table row supports). Three partials are BSG list items
quoted without the list's lead-in ("People aged ≥55 years with:"), so the age condition isn't in the quote, and one
of them dropped the age from the item too; see backlog. The others: a feature not in the quote (SAH "can be the only
symptom") and two vertigo items built from history questions (tinnitus, chest pain) that the text doesn't call
warning signs.

**X9 passes** with run 2 (`grounding.extract.v3` + `verify.v2`).

**Duplicates:** the two extractors' items are often kept side by side (EHF: 60 red flags for 20 gold, the same
table row up to 3–4 times with different wording). Recall isn't affected, but the safety panel sees repeats; see
backlog.

## X3 Dual claim extraction (`x3_dual_extraction.py`, 2026-11-28)

6 open-access full texts (ENT: 2 cohorts; neurology: cohort + RCT; digestive: 2 RCTs), one pack each (82k tokens in
total), `read.claims.v1`, two members (`gpt-5.4-mini` at high and medium effort), reconciliation as in B8.14.
16 live calls: 12 extractions (163k input, 124k output) and 4 borderline judgements; **291k tokens, $0.69** at list
price. Review sheet `results/x3/pairs.md`; hand pairs (agent-drafted, like the X9 gold list) in `x3_gold.yaml`.

| Measure | Result | Pass |
|---|---|---|
| Valid sentence IDs, m0 / m1 | 64/64, 62/62 (100%); 21 claims each, none rejected | ✅ ≥ 90% |
| Hand pairs put in one cluster by reconciliation | 11/15 (73.3%) | ❌ ≥ 80% |

**Missed pairs.** All four are the same finding cited from different places: one extractor cites the abstract, the
other the results section or a table (PMC7388434 ×3, PMC10764899 ×1), so the sentence-ID overlap rule (≥ 50%) never
fires and the pair isn't even sent to the judge. Fix for the `quality` path (not needed by `budget`, which doesn't
reconcile): treat abstract and body citations of the same numbers as overlapping, or send pairs with matching
effect numbers to the judge.

**What it means for the `budget` profile (single extraction, PLAN B17.5 item 5).** The two extractors together
found 27 distinct findings (15 shared, 6 only by m0, 6 only by m1). **Each alone found 21/27 (78%).** The extractors
are selective (3–5 claims per paper with a cap of 20), and what one of them skipped includes a null result (railway
noise not associated with tinnitus, PMC9891135), secondary outcomes (EPS response, PMC10764899; amitriptyline vs
melatonin, PMC5036209) and the non-responders of the gluten challenge (PMC7016109). A second extractor mostly
compensates for selectivity, which a better prompt can fix more cheaply.

**Decision (2026-11-28):** before adding a second extraction to `budget`, task 1R.4 writes `read.claims.v2`: every
primary and secondary outcome result, null and non-significant results, harms, and subgroup results that bear on a
hypothesis, each as its own claim. Then a single-extraction re-run on these 6 papers (about 0.15M tokens) is scored
against the 27-finding union: **target ≥ 24/27 (≥ 89%)**. If it misses, B17.7's fallback applies (a second
extraction for results sections only).

### X3 re-run: one extractor with `read.claims.v2` (2026-11-28, task 1R.4)

`x3_dual_extraction run --profile budget --single --claims read.claims.v2`: one extractor (`gpt-5.4-mini`, medium),
16k packs (PMC10764899 became 2 packs), the coverage prompt. Claims in `results/x3/pairs.budget-single-v2.md`.

**Coverage, scored by hand against the 27 distinct findings the two `v1` extractors found together: 24/27 (89%), at
the target (≥ 24/27).** Per paper: PMC9891135 4/6, PMC5615572 6/6, PMC7388434 3/3, PMC5036209 4/4, PMC7016109 2/3,
PMC10764899 5/5. The three misses are not study results: a discussion statistic (19% of tinnitus cases had no prior
hearing-loss diagnosis), a background statement (hearing loss is the main risk factor) and the patient flow of the
gluten trial (452 → 193 → 77). The null railway result, the secondary outcomes, the harms and the non-responders that a
single `v1` extractor missed are all in. The extractor now lists **91 claims** where each `v1` extractor listed 21:
every outcome, arm and time point (PMC10764899 alone: 30).

**Cost: 486k tokens, $1.23**, 18 calls: 7 first calls, 6 truncation retries and 5 repairs. With 16k of output the
coverage prompt plus reasoning was cut off, and more than 15 claims per pack failed the limit. The budget profile now
gives `read.claims` 32k and allows 25 claims per pack (decision 2026-11-28), so one call per pack is expected. About
35–45k tokens per pack follows from the first calls (15k in, 11–19k out); that is more than B17.4's 24k, and 4× the
claims also means more verification and mapping. The 1R.5 live check measures the `read` group against its 650k
target; if it is over, the levers are fewer claims per pack (results only, no background) before fewer papers.

