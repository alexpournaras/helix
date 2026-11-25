# Backlog

Ideas that are out of scope for the current task (PLAN Part G.5).
- Literature UI: show a "Retracted" / "Expression of concern" badge now that `literature.db` stores the flags (1A.8).
- `eval/tools/build_pack.py` (B7.2 automated pack build) once grounding exists (1C.3); compare with the hand-built tinnitus v0.
- Floor rule-out questions are English only; translate them with the brief's language in 1C.2.
- Eval fake mode: per-step fake handlers so a fake run passes intake (the fake provider's minimal answer is "not a health problem, no reason" and fails validation).
- Eval cases: grow to ≥ 15 across ≥ 5 systems for the M1 gate (E3.1), including an adversarial input and a special-population case per population.
- Eval: a planted-fact case for the brief check (the scratch probe from 1C.1) as a recorded replay.
- 1C.4 questions: the two safety panel members often ask near-duplicate rule-outs (e.g. "pulsatile sound" / "does tinnitus have a pulsatile quality"); the questions step should merge them before asking.
- Grounding merge: measure duplicate merging on a labelled item list (the grouping judge merges about half of the obvious duplicates on dev_openai; e.g. "Avoid dietary supplements" / "Dietary supplements" stay apart). Two live runs of the same case gave 88 and 122 items, so also measure run-to-run variance.
- Grounding speed on dev_openai: about 8–10 min per case (verification is ~half: 74 calls at ~30 s). Try larger verify batches and medium effort for verifiers.
