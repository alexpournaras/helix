Step: claim extraction. You list the evidence one part of a research paper gives on the research hypotheses for
one person's health problem.

The material gives a short case summary, the hypotheses (`key`, title, description), the paper's design, and
one part of the paper with sentence, caption and cell IDs in square brackets.

Return `claims` (at most the number given), each:
- `claim`: one self-contained English sentence (≤ 400 characters) stating what this paper found or states, with
  its population and numbers. Don't add your own interpretation.
- `sentence_ids`: 1 to 4 IDs from this text that state it. Never cite an ID that isn't shown.
- `evidence_type`: `finding` (this study's own result), `mechanism`, `guideline_recommendation`,
  `epidemiology`, `diagnostic_accuracy`, or `background` (what *other* studies found, as reported here, or
  general statements).
- `population`, `design`, `n` (participants behind this result, if stated).
- `effect`: `{measure, value, ci_low, ci_high, p}` exactly as written (numbers only from the cited text), or
  null.
- `limitations`: what the paper says limits this result.
- `links`: for each hypothesis it bears on, `direction` (`supports`, `contradicts`, `neutral`), `strength`
  (`weak`, `moderate`, `strong`), `case_match` (how well the studied people match this case: `high`,
  `partial`, `low`, `unknown`) and a short `reason`.
Include evidence against a hypothesis as carefully as evidence for it. A result that isn't statistically
significant is not an effect: say so in the claim. Skip methods detail that isn't a result.
