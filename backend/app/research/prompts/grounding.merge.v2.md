Step: knowledge merge. The material lists knowledge items of one kind (red flags, conditions, history
questions, evaluations or management topics) for one investigation, each with an ID (`X{n}`), its origin
(`curated` from a reviewed knowledge file, or `guideline`) and its population.

Find the items that mean the same thing, so that one can be kept. Return `groups`: each group has `keep` (the
item to keep: a `curated` item if the group has one, else the clearest wording) and `same` (the other items of
the group).

- Group items only if a clinician would treat them as one item: "Pulsatile quality" and "Pulse-synchronous
  sound" are the same; "Hearing aids" and "Cochlear implant evaluation" are not.
- An item that is narrower or broader in a way that matters is not the same (e.g. "Avoid imaging in primary
  headaches" vs "Avoid imaging in low-risk head injury").
- Only group items of the same population.
- Leave items without duplicates out. Every ID appears at most once.
