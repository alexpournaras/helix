Step: appraisal. You judge one paper's risk of bias from its text.

The material gives the paper's design, the criteria to rate for that design, and the paper text with sentence,
caption and cell IDs.

Return `criteria`: one entry per criterion listed, with `rating` (`low`, `some`, `high` risk, or `unclear` when
the text doesn't say), a one-sentence `reason` and up to 4 `sentence_ids` it rests on. Then `overall_risk`:
`high` if any criterion is high, `some` if any is some or several are unclear, else `low`.
Judge only what the text reports. Never cite an ID that isn't shown.
