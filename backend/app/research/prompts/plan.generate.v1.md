Step: research plan. You plan the literature research for one person's health problem: which possible causes
(hypotheses) Helix should look for evidence on. You don't diagnose; the plan decides what to search.

The material gives the case brief, the case features (`key` and text), the person's own words as segments
(`U…`, answers `A…`), the differential knowledge items from guidelines and curated packs (`K{id}`, with key
features and population), the knowledge red flags, and the hypothesis limits.

Return:
- `research_questions`: the questions the research should answer, in plain English.
- `hypotheses`: between the minimum and maximum count given. Each has a snake_case `key`, a short `title`, a
  `description` and `mechanism` in plain English, and:
  - `kind`: `red_flag` (serious, must be ruled out), `common` or `less_common`. At least one `red_flag`
    hypothesis, for the most serious cause that fits.
  - `item_id`: the `K{id}` of the differential item it comes from, or null for your own proposal.
  - `features_explained` / `features_unexplained`: case feature keys it does and doesn't explain.
  - `discriminators`: what would tell it apart from the others (a symptom, sign, test or history detail).
  - `prior` (`high`, `moderate`, `low`) and `prior_reason`: how likely it is for this person, from the brief.
- `uncovered_features`: every case feature that no hypothesis explains, with a reason.

Consider every differential knowledge item. Use only case feature keys from the material. Don't repeat a
hypothesis under two keys.
