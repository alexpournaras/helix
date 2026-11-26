Step: claim verification. Each pair (`C{n}`) is a claim extracted from a research paper and the text it cites
(and, when given, the surrounding paragraphs, or the whole section and the study profile). You check whether the
cited text supports the claim.

For every pair return one entry with its `pair` ID:
- `asserts_effect`: true if the claim says there is an effect, difference or association.
- `direction_check`: `same` if the text points the same way as the claim, `opposite` if it points the other way
  (e.g. the claim says "reduced", the text "increased"), `not_applicable` for claims without a direction.
- `significance`: what the text says about statistical significance of that result: `significant`,
  `not_significant`, `not_reported`, or `not_applicable`.
- `population_check`: `same`, `broader` (the claim generalises to more people than were studied), `different`,
  or `not_stated`.
- `overgeneralization`: true if the claim states more than the text (e.g. causation from an association,
  "always" where the text says "in some").
- `context_needed`: true if you can't judge without more of the paper (only when no context is given).
- `verdict`: `supported` (the text states it), `partial` (states part of it or something weaker), or
  `unsupported` (doesn't state it, states something else, or the opposite).
- `reason`: one short sentence.
Judge only the text given, not your own knowledge.
