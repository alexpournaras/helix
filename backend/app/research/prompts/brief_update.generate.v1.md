Step: brief update. The person answered clarifying questions (or added a follow-up). You update the case brief.

The material gives the previous brief (as JSON, with fact paths), every segment the person wrote (problem text
`U{n}`, answers `A{id}` written as "Q: … A: …", follow-ups `FU{id}`), and which segments are new.

Return the complete updated `brief`, `complaints` and `proposed_systems`, with the same rules as at intake:
- Each fact is `{text, seg_ids}` in English and cites the segments that state it. Keep facts from the previous
  brief that still stand, with their citations. Add what the answers state.
- Remove an unknown once an answer resolves it; keep it if the person skipped or didn't answer it.
- A fact from the previous brief may only be dropped when a segment contradicts it: list it in
  `removed_features` with its previous `key` (the fact path) and the contradicting `seg_ids`.
- A new complaint in the answers gets its own entry in `complaints` with MeSH candidates.
- If the problem text isn't in English, give `translations` for every new segment.
- `implied` is always false.
