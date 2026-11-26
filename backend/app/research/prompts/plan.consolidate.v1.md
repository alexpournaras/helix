Step: research plan consolidation. Two planners each proposed a research plan (hypotheses to look for evidence
on) for one person's health problem. You merge them into one plan and account for every differential knowledge
item.

The material gives the case brief, the case features, the person's own words as segments (`U…`, answers
`A…`), the differential knowledge items (`K{id}`), the knowledge red flags, the hypothesis limits, and the
plans.

Return one plan in the same shape:
- Keep every hypothesis that either planner gives good reason for; merge the same cause proposed twice (keep one
  key). Stay within the hypothesis limits; drop the weakest first, never the only `red_flag` one.
- Every case feature is in some hypothesis's `features_explained` or in `uncovered_features` with a reason.
- At least one `red_flag` hypothesis.

And `knowledge_coverage`: one entry for **every** differential knowledge item:
- `decision: hypothesis` with the `hypothesis_key` that covers it, or
- `decision: excluded` with a `reason` and the `seg_ids` of the person's words that rule it out (e.g. the
  stated age, or a stated negative). Exclude only what the person's words rule out; when unsure, keep it as a
  hypothesis.
