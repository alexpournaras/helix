# Decisions

Changes to PLAN.md and to fixed targets are recorded here (PLAN Part G.5, E5).

| Date | Topic | Decision | Why |
|---|---|---|---|
| 2026-11-23 | Test runner | `npm test` runs `unittest discover` over `backend/test_*.py`; no pytest dependency | The existing suite is unittest; avoids a new dependency (C24: no upgrades) |
| 2026-11-23 | Branching for 1A | 1A tasks are built on one branch `feat/research-1a-foundations`, one commit per task | The tasks are tightly coupled (DB ← settings ← model layer ← worker); a PR per commit is still possible |
| 2026-11-23 | Roles not named in B6 | Added `grounding.queries` (A sol), `plan.consolidate` (A astra), `read.reconcile` (A sol, borderline-pair judge) as explicit roles; `adjudicate` / `confirm` resolve at run time from `strongest.{family}` | B8.3a, B8.6 and B8.14 name these models but B6 lists them inside other rows |
