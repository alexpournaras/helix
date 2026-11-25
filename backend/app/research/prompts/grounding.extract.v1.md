Step: grounding extraction. You read part of a clinical guideline or review and list the knowledge a
clinician uses to assess one complaint: red flags, the differential, history questions, evaluations and
management topics.

The material gives the complaint, the population and the paper part. Every sentence, caption or table cell
has an ID in square brackets (e.g. `[S2.P1.s3]`, `[T1.r2.c3]`).

Rules:
- Only what this text states about this complaint (or a closely related presentation). Don't add general
  medical knowledge.
- Each item cites 1 to 4 `sentence_ids` from this text that state it. Never cite an ID that isn't shown.
- `red_flags`: features that need urgent or emergency assessment, with `urgency` as the text implies it
  (`emergency`, `urgent`, `routine`) and `population` when it applies only to one population.
- `differential`: conditions to consider, with their key features.
- `history_items`: what to ask the person (`attribute`, and `why`).
- `evaluations`: tests or examinations and when they are indicated.
- `management_topics`: management options to discuss with a clinician (no doses).
- Write each item as a short English phrase. Return empty lists if the text has nothing relevant.
