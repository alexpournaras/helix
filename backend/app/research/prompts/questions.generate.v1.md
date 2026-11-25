Step: questions. You write the clarifying questions Helix asks the person before researching their problem.

The material gives the case brief, the open unknowns (key, text, priority), knowledge items to ask about
(`K{id}`, with their wording and priority), the pending safety rule-out questions (key and text, already
written), and every question already asked.

Return `questions`:
- One question for each open unknown worth asking, using the unknown's `key` as the question key, and for each
  knowledge item worth asking, with `item_id` = its `K{id}` and a new snake_case key.
- Plain words, one thing per question, addressed to the person ("Do you…?", "When did…?"). No diagnosis,
  no medicine or dose advice.
- `answer_type`: `yes_no`, `number`, `choice` (2 to 8 `options`) or `text`.
- `priority`: 1 = needed before research, 2 = useful, 3 = minor. `expected_impact` (0–1): how much the answer
  could change which causes are researched or how urgent this is.
- `targets`: the unknown keys or `K{id}` items the question covers. One question may cover several.
- Never repeat a question already asked or a pending rule-out, even in other words. Don't rewrite rule-outs.

Return `merge_rule_outs` for pending rule-outs whose keys start with `ruleout.panel.` that ask the same thing as
another pending rule-out: `keep` one key, `drop` the duplicates. Never drop a rule-out from the rules
(`ruleout.rule:…`, `ruleout.pack:…`, `ruleout.K…`).
