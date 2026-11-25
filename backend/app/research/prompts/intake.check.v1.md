Step: brief check. Another model built a case brief from a person's problem text. You check every fact
against the segments it cites.

The material gives the segments (original text and, when translated, English) and a list of facts, each
with a `path`, its `text` and its cited segment IDs.

For every fact, return one item with the same `path` and a `verdict`:
- `stated`: the cited segments say it, in substance (paraphrase and translation are fine).
- `implied`: the cited segments suggest it but don't say it.
- `not_stated`: the cited segments don't support it, or it adds detail they don't contain (a number, a side,
  a duration, a diagnosis).

Judge only the cited segments, not what is likely. Give a short `reason` (at most one sentence). Return
exactly one item per fact, and no other paths.
