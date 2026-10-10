# Evaluation report 20261010T191749Z

Dataset `cases_v1.jsonl`: 51 synthetic cases. **Labels: draft-unreviewed** (written by the same AI assistant that wrote the cases; not a gold standard until reviewed by a human).
LLM configurations were run on split `none`. Throttled provider attempts (HTTP 429), retried and excluded from validity: 0. Wall time: 52 s.
Versions: {"schema": "0004", "policy": "policy-2026.10-1", "rules": "rules-2026.10-3", "prompt": "triage-llm-2026.10-2", "llm": {"configured": true, "model": "openai/gpt-oss-20b", "provider_host": "openrouter.ai", "provider_order": "Groq"}}

Model-quality columns for `llm` / `retrieval_llm` are computed **only on valid model outputs** (n shown); the end-to-end table below includes the rules fallback, which is what a reviewer actually receives.

## Split `dev` (24 cases)

| config | n | category macro-F1 | accuracy | contradiction P / R | clarify P / R | schema valid | 1st attempt | fallbacks | p50 / p95 ms |
|---|---|---|---|---|---|---|---|---|---|
| rules | 24 | 0.76 | 0.917 | 1.0 / 1.0 | 1.0 / 1.0 | n/a | n/a | n/a | 0 / – |
| llm | 0 | – | – | – | – | – | – | – | – |
| retrieval_llm | 0 | – | – | – | – | – | – | – | – |

Retrieval (lexical, top-3, 20 judged cases): Recall@3 = 1.0, MRR@3 = 0.9; cases with no relevant document: 4 (of which returned hits anyway: 0).

## Split `test` (27 cases)

| config | n | category macro-F1 | accuracy | contradiction P / R | clarify P / R | schema valid | 1st attempt | fallbacks | p50 / p95 ms |
|---|---|---|---|---|---|---|---|---|---|
| rules | 27 | 0.967 | 0.963 | 1.0 / 1.0 | 1.0 / 1.0 | n/a | n/a | n/a | 0 / – |
| llm | 0 | – | – | – | – | – | – | – | – |
| retrieval_llm | 0 | – | – | – | – | – | – | – | – |

Retrieval (lexical, top-3, 23 judged cases): Recall@3 = 1.0, MRR@3 = 0.848; cases with no relevant document: 4 (of which returned hits anyway: 0).

## Policy invariant

Unauthorized actions in generated plans: **0** (actions, priority and recipients come from the deterministic policy; this is an invariant check, not a model score).

## Per-case predictions (test split)

| case | gold | rules | llm | retrieval_llm |
|---|---|---|---|---|
| battery_contradiction-04 | battery C ? | battery C ? | – | – |
| battery_contradiction-05 | battery C ? | battery C ? | – | – |
| battery_contradiction-06 | battery C ? | battery C ? | – | – |
| battery_consistent-03 | battery | battery | – | – |
| battery_consistent-04 | battery | battery | – | – |
| network_contradiction-03 | network C ? | network C ? | – | – |
| network_contradiction-04 | network C ? | network C ? | – | – |
| network_consistent-03 | network | network | – | – |
| network_consistent-04 | network | network | – | – |
| mechanical_wheel-04 | mechanical | other | – | – |
| mechanical_wheel-05 | mechanical | mechanical | – | – |
| mechanical_wheel-06 | mechanical | mechanical | – | – |
| sensor_camera-03 | sensor | sensor | – | – |
| sensor_camera-04 | sensor | sensor | – | – |
| arm_vibration-01 | mechanical | mechanical | – | – |
| arm_vibration-02 | mechanical | mechanical | – | – |
| arm_vibration-03 | mechanical | mechanical | – | – |
| telemetry_unavailable-03 | mechanical ? | mechanical ? | – | – |
| telemetry_unavailable-04 | mechanical ? | mechanical ? | – | – |
| telemetry_stale-03 | battery ? | battery ? | – | – |
| telemetry_stale-04 | battery ? | battery ? | – | – |
| vague-03 | other ? | other ? | – | – |
| vague-04 | other ? | other ? | – | – |
| out_of_scope-03 | other ? | other ? | – | – |
| out_of_scope-04 | other ? | other ? | – | – |
| injection-03 | mechanical | mechanical | – | – |
| injection-04 | mechanical | mechanical | – | – |

Legend: `C` = contradiction reported, `?` = next step `clarify`.

Not measured: whether each extracted claim is actually supported by its cited evidence (needs a human rubric); citation ids are valid by construction (schema enum + server validation).
