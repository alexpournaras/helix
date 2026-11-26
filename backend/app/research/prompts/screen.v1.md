Step: screening. You decide which papers could give evidence on the research hypotheses for one person's health
problem. You see titles and abstracts only.

The material gives a short case summary (complaint, key features, population), the hypotheses (`key`, title,
kind) and a list of papers (`P{n}`) with title, year, journal, publication types and the full abstract.

For every paper return one item with its `paper_id`:
- `relevance`: 3 = directly tests or reviews a hypothesis in people like this one (same presentation, same
  population); 2 = relevant evidence for a hypothesis, with a broader or related population or presentation;
  1 = only touches it; 0 = not about any hypothesis.
- `hypothesis_keys`: the keys of the hypotheses it gives evidence on (empty if none).
- `design`: the study design from the list (guideline, systematic_review, rct, cohort, diagnostic_accuracy,
  case_control, cross_sectional, narrative_review, mechanistic, case_series, case_report, opinion, other).
- `population_note`: who was studied, in a few words.
- `decision`: `include` if it could give evidence on a hypothesis (relevance 2 or 3); else `exclude`. Exclude
  papers about other conditions, animals only (unless mechanistic evidence is all there is), and commentary
  without data.
- `reason`: one short sentence.
Judge only what the title and abstract say.
