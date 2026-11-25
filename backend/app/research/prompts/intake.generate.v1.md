Step: intake. You read a person's description of a health problem and build a structured case brief.

The material lists the problem text as numbered segments ("U1: …"). Each fact you record must cite the
segment IDs that state it.

1. Acceptance.
   - `is_human`: false if the problem is about an animal (a pet, livestock).
   - `is_health_problem`: false if the text isn't about a health problem (a recipe, code, a general question).
   - `not_accepted_reason`: `not_human`, `not_health`, or `prescription_or_dose` when the person only asks for a
     prescription, a medicine choice or a dose. Otherwise null.
   - If the text isn't accepted, still fill `language` and `title`, and leave the other lists empty.
2. Language. `language` is the ISO 639-1 code of the problem text ("en", "el", …). If it isn't English, give
   `translations`: one faithful English translation per segment, for every segment. For English, leave
   `translations` empty.
3. `title`: a short neutral English title (at most 8 words), no names.
4. `complaints`: each distinct health complaint, with a lowercase snake_case `key` (e.g. `tinnitus`,
   `head_pressure`), a short English `text`, its `seg_ids`, and up to 5 `mesh_candidates`: the MeSH descriptor
   names (e.g. "Tinnitus", "Headache") or descriptor UIs (e.g. "D014012") that best describe it. Prefer
   specific symptom or disease descriptors. Code checks them against MeSH.
5. `proposed_systems`: the body systems the complaints belong to.
6. `brief`: only what the person states. Each fact is `{text, seg_ids}` in English.
   - `chief_complaint`, `symptoms` (one entry per symptom with its details), `associated_symptoms`,
     `stated_negatives` (things the person says they don't have).
   - `demographics`: `age.years` as a number when stated; `sex`; `pregnancy` only when stated.
   - `chronic_conditions` and `medications`, each with `flags` when they apply: `immunosuppressant`,
     `chemotherapy`, `anticoagulant`, `immunodeficiency`, `diabetes`, `serious_chronic`.
   - allergies, family history, lifestyle, travel, recent infections, vitals and test results as reported.
   - `case_features`: the features that matter for research, each with a snake_case `key`.
   - `unknowns`: important missing information, priority 1 (needed before research), 2 (useful) or 3 (minor).
   - `follow_up_focus`: null.
   - `implied` is always false.
   Never add a fact the text doesn't state: if something is missing, leave it null or empty and list it as
   an unknown instead.
