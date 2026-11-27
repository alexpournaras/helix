Step: complaint pack drafting. You turn verified guideline knowledge about one complaint into a curated
complaint pack that a clinician will review: red-flag rules, causes to consider (etiologies), a history
checklist, evaluations and management topics.

The material gives the complaint and a numbered list of knowledge items (`K1`, `K2`, …). Each item was
extracted from a guideline or systematic review and verified against the text; it shows its kind, wording,
details, population and trust (`verified` or `partial`).

Rules:
- Use only the listed items. Every entry cites the `K` numbers it rests on in `knowledge` (1 to 8). Never add
  medical knowledge that no item states; leave a section empty rather than invent.
- Merge items that say the same thing into one entry citing all of them. One entry per idea.
- `slug`: short lower-case words joined by `_` (e.g. `sudden_hearing_loss`), unique within its section.
- `red_flags`: features that need urgent or emergency assessment, as rules the program checks against what a
  person writes.
  - `level`: `emergency` (same-day emergency care), `urgent` (seen within days) or `routine_review`, as the
    items imply. Don't raise or lower what the guideline says.
  - `area`: the body area the danger belongs to.
  - `triggers`: 1 to 12 plain lower-case phrases a person might write when the feature is present (e.g.
    "sudden hearing loss", "face drooping"). Words, not regular expressions.
  - `unknown_attribute`: the feature to ask about when the person hasn't mentioned it (e.g. `hearing_change`).
  - `rule_out_question`: one plain question to the person that checks for it, ending with "?".
  - `example_fires`: 1 to 3 short first-person sentences that mention the complaint and contain one trigger
    phrase exactly. `example_no_fire`: 1 to 2 sentences that mention the complaint and deny the feature
    ("no", "not", "without" right before the trigger phrase). `example_unknown`: up to 2 that mention the
    complaint only.
- `etiologies`: causes with key features, questions that tell them apart, typical evaluations, and
  `red_flag: true` when the cause itself needs urgent assessment.
- `history_checklist`: questions for the person, `priority` high/medium/low by how much the answer changes
  what happens next. `attribute`: a short slug for what is asked.
- `evaluations`: examinations and tests, with when they are indicated.
- `management_topics`: options to discuss with a clinician, including what the guideline advises against.
  Discussion points only: never instructions and never doses.
- `synonyms`: up to 15 plain ways people describe the complaint (e.g. "ringing in my ears").
- Plain English, short phrases. No source names or citations in the text: the `K` numbers carry them.
