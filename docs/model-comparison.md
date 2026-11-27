# Model comparison (development models)

How far each development model has got through the pipeline and the feasibility checks, on the same synthetic
cases. Updated after each live run. Sources: `backend/eval/results/*-live.json`, `docs/spikes.md`,
`docs/decisions.md`.

| | gpt-5.4-mini (OpenAI) | qwen3-coder:latest (Ollama, local) |
|---|---|---|
| Profile | `dev_openai`: every role, family-A members at `high` effort, family-B at `medium` | `dev`: family B next to gpt-5.4-mini; alone: `dev_ollama` (2026-11-27) |
| Runs on | OpenAI API, 2.5M tokens/day | this PC: RTX 2060 (6 GB) + CPU; the 18 GB model mostly runs on the CPU |
| Context | 400k | 32k (packs of 12k tokens) |
| Structured output probe | ✅ ~2.5 s; efforts none…xhigh | ✅ ~36 s first load; no thinking mode |
| Price | $0.75 / 1M input ($0.075 cached), $4.50 / 1M output | free (electricity) |

## Pipeline stages (live, synthetic cases)

| Stage | gpt-5.4-mini | qwen3-coder |
|---|---|---|
| 1C.1 Intake (5 cases) | ✅ 5/5 expectations met (intake writer) | ✅ as brief checker: caught 5/5 planted facts, kept 5/5 true ones. Alone: not run |
| 1C.2 Safety (8 cases) | ✅ run 2: 8/8 (emergency and crisis stops correct). Run 1: 6/8 | same runs, as the second panel member. Alone: not run |
| 1C.3 Grounding (tinnitus case) | ✅ 3 runs complete: 84–109 calls, 0.32–0.54M tokens, 8–10 min | ⚠️ started 11-25 (`dev`), stopped: too slow. Alone: not run |
| 1C.4 Questions and brief update | not run live | not run live |
| 1D Planning, queries, search, screening | not run live | not run live |
| 1E Reading, verification, evidence, gaps | not run live | not run live |
| 1F Report | not built | not built |

## Feasibility checks

| Check | gpt-5.4-mini | qwen3-coder |
|---|---|---|
| X9 knowledge extraction (6 guidelines) | ✅ run 2: recall 80/80, attestations 163/169 (96.4%); 335 calls, 2.13M tokens, ~37 min. Run 1: 70/80 ❌ | not run |
| X3 dual extraction (6 papers) | ready, not run (~0.4–0.6M tokens) | ready (`--profile dev_ollama --model …`) |
| X1 structured output, step schemas | after 1F | after 1F |

Not comparable yet: on `dev` qwen3-coder only ever ran next to gpt-5.4-mini, so no result above is the local
model on its own.

## Local speed on this PC (measured 2026-11-27)

| Model | Fits the 6 GB GPU? | Input | Output | Full X9 (estimate) |
|---|---|---|---|---|
| qwen3-coder:latest (30B MoE, 22 GB) | no: 79% CPU at 32k | ~435 tok/s | ~16–22 tok/s | ~4–5 h |
| qwen3:4b (2.3 GB) | yes at 24k context (5.0 GB, 100% GPU); 84% at 32k | ~1,650 tok/s | ~50–70 tok/s | ~1 h for 2 guidelines with thinking on; ~15–20 min with thinking off |

The 1D + 1E pipeline (millions of tokens per case) isn't practical locally on this PC. Ollama fits small checks:
short runs with thinking off, and cheap roles (brief check, second safety opinion). The reduced qwen3:4b X9 run was
stopped after its first calls (long GPU runs are avoided).

