Step: paper assessment (one call). You describe one research paper's study, judge its risk of bias, and judge
how well its evidence applies to one person's case.

The material gives the case summary (complaint, key features, age, sex, special populations), the paper's design,
the risk-of-bias criteria to rate for that design, and the paper's abstract and methods with sentence, caption and
cell IDs in square brackets.

Return three parts.

`profile`: every field as `{value, sentence_ids}`: a short English value as the paper states it (null when the text
doesn't say) and 1 to 4 IDs that state it. Fields: `question`, `design`, `setting`, `population`,
`inclusion_criteria`, `n` (the number analysed, as written), `exposure`, `comparator`, `outcomes`, `follow_up`,
`funding`, `conflicts`, `review_included_designs` (reviews only: the study designs they included),
`dataset_or_cohort` (a named cohort, trial, registry or dataset the data come from), `secondary_analysis`
("yes" when the paper analyses data published elsewhere, else "no").

`appraisal`: `criteria`, one entry per criterion listed, with `rating` (`low`, `some`, `high` risk, or `unclear`
when the text doesn't say), a one-sentence `reason` and up to 4 `sentence_ids` it rests on. Then `overall_risk`:
`high` if any criterion is high, `some` if any is some or several are unclear, else `low`.

`applicability`:
- `population_match`, `symptom_match`, `setting_match`: `high`, `partial`, `low` or `unknown`.
- `special_population_fit`: when the case belongs to a special population (infant, child, pregnancy, older,
  immunocompromised): `match` (the paper studied that group), `partial` (it included some), `mismatch` (it
  didn't, e.g. adults only for a child); `not_applicable` when the case belongs to none.
- `overall`: `high`, `partial` or `low`, and a one-sentence `reason`.

Only what the text states; judge only what it reports. Never cite an ID that isn't shown.
