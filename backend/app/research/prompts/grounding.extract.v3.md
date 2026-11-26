Step: grounding extraction. You read part of a clinical guideline or review and list the knowledge a
clinician uses to assess one complaint: red flags, the differential, history questions, evaluations and
management topics.

The material gives the complaint, the case's population and the paper part. Every sentence, caption or table
cell has an ID in square brackets (e.g. `[S2.P1.s3]`, `[T1.r2.c3]`).

Rules:
- Only what this text states about this complaint (or a closely related presentation). Don't add general
  medical knowledge, and skip items about other conditions the paper also covers.
- Each item cites 1 to 4 `sentence_ids` from this text that state it. Never cite an ID that isn't shown.
- `population`: `all` unless the text limits the item to one group (`infant`, `child`, `pregnancy`, `older`,
  `immunocompromised`). Tag it; don't drop it.
- `red_flags`: specific features that need urgent or emergency assessment (e.g. "Pulsatile tinnitus",
  "Sudden hearing loss in one ear", "Tinnitus with facial weakness"). Never a vague category such as
  "serious underlying disease". `features`: the concrete signs or symptoms. `urgency` as the text implies it
  (`emergency`, `urgent`, `routine`). `question`: one short question to the person that checks for it, in
  plain words ("Do you…?", "Have you…?").
  - Anything the text calls a red flag, warning feature, alarm sign or "sinister" feature is a red flag,
    including examination findings (e.g. papilloedema) and history items such as medication overuse.
  - Referral and urgent-test criteria are red flags too (e.g. "aged 55 or over with weight loss → urgent
    endoscopy"): one red flag per criterion, keeping its age threshold and combination of features. List the
    test under `evaluations` as well.
  - When the text gives a list or table of warning features, every listed feature must appear in some red
    flag's `features`; don't drop any to keep an item short.
- `differential`: conditions to consider, with their key features.
- `history_items`: things to ask the person about their symptoms or history. Not examinations, tests or
  "take a history". `question`: the plain question to ask.
- `evaluations`: examinations and tests and when they are indicated.
- `management_topics`: management options to discuss with a clinician, including what the guideline advises
  against. No doses.
- Write each item as a short English phrase. One item per idea: don't list the same thing twice in different
  words. Return empty lists if the text has nothing relevant.
