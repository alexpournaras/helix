Step: grounding verification. Each pair (`X{n}`) is a knowledge item extracted from a guideline or review and
the quote it cites. You check whether the quote supports the item.

For every pair return one entry with its `pair` ID:
- `verdict`: `supported` (the quote states it), `partial` (states part of it, or something weaker), or
  `unsupported` (doesn't state it, states something else, or the opposite).
- `population_check`: `same` (same population as the item), `broader`, `different`, or `not_stated`.
- `overgeneralization`: true if the item states more than the quote (e.g. "always" where the quote says
  "consider").
- `context_needed`: true if you can't judge without the surrounding text (only when no context is given).
- `reason`: one short sentence.
A red flag is supported when the quote presents it (and its listed features) as a warning feature or a reason
for urgent assessment, referral or testing; how urgent it is isn't part of the claim.
Judge only the quote (and the context, when given), not your own knowledge.
