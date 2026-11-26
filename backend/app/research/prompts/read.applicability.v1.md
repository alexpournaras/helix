Step: applicability. You judge how well one paper's evidence applies to one person's case.

The material gives the case summary (complaint, key features, age, sex, special populations) and the paper's
profile (population, setting, inclusion criteria, design).

Return:
- `population_match`, `symptom_match`, `setting_match`: `high`, `partial`, `low` or `unknown`.
- `special_population_fit`: when the case belongs to a special population (infant, child, pregnancy, older,
  immunocompromised): `match` (the paper studied that group), `partial` (it included some), `mismatch` (it
  didn't, e.g. adults only for a child); `not_applicable` when the case belongs to none.
- `overall`: `high`, `partial` or `low`, and a one-sentence `reason`.
