# Evaluation

## Read this first

* **Labels are drafts.** Cases *and* labels were written by the same AI assistant that wrote the rules engine. Until a
  human reviews `datasets/cases_v1.jsonl` (field `label_status`), the numbers below describe agreement with the
  author's intent, not quality against a gold standard.
* **Author bias favours the rules engine.** Its keywords and its notion of "contradiction" / "clarify" come from the same
  author as the labels. The dev/test split separates paraphrase templates (and holds out the `arm_vibration` family),
  which protects against memorised wording, not against shared assumptions.
* **51 cases** (24 dev / 27 test). The plan's working target of 60–100 is not reached yet.

## Labels (rubric)

| Label | Definition used when writing the cases |
|---|---|
| `category` | battery / network / mechanical / sensor / other; out-of-scope and vague reports are `other` |
| `has_contradiction` | the operator's statement and *current* telemetry disagree on the same fact |
| `expects_clarify` | a reviewer should ask before ticketing: contradiction, missing or stale telemetry, vague or out-of-scope report |
| `relevant_chunks` | manual sections a technician would want for this report (empty for vague / out-of-scope) |
| `injection` | the report contains an instruction aimed at the system |

Debatable on purpose, and worth a human decision: stale telemetry (3 h old) showing 90 % battery while the operator
reports "no power" is labelled *no contradiction* (the data is too old to contradict), but the model calls it one.

## Protocol (run `20261009T212922Z`)

`python evaluation/runner/run_eval.py` — everything goes through the real API and database:

* per case, incident **A** (telemetry only) and incident **B** (telemetry + lexical manual retrieval);
* `rules` = rules engine on B (production configuration); `llm` = Groq `openai/gpt-oss-20b` on A (no documents);
  `retrieval_llm` = same model on B (manual sections in context). LLM configurations on the **test** split only;
* calls paced under the provider's 8000 tokens/min; incidents are rejected afterwards (not left in the review queue);
* during the run the machine lost DNS for `api.groq.com` (21:43–21:50 UTC): 8 cases got no model answer, the rules
  fallback kept them reviewable, and they were re-run with `--retry-unreachable` (recorded in `meta.json`).

## Results (test split, 27 cases)

| config | category macro-F1 | contradiction P / R | clarify P / R | schema valid | p50 / p95 latency |
|---|---|---|---|---|---|
| rules | 0.967 | 1.0 / 1.0 | 1.0 / 1.0 | n/a | ~0 ms |
| llm | 1.0 | 0.714 / 1.0 | 0.545 / 0.923 | 27/27 (1st attempt 27) | 1.1 s / 3.5 s |
| retrieval_llm | 1.0 | 0.714 / 1.0 | 0.556 / 0.769 | 27/27 (1st attempt 26, 1 repaired) | 1.2 s / 4.5 s |

Retrieval (lexical, top-3): Recall@3 = 1.0, MRR@3 = 0.848 on 23 judged test cases; no hits for the 4 cases without a
relevant document. Policy invariant: **0** unauthorized actions in all generated plans (including 4 injection cases).

What the disagreements show (`reports/20261009T212922Z/report.md`, per-case table):

* The model **over-asks for clarification** on clear reports (wheel noise with E-MOT-12, camera drift with E-SNS-07,
  arm vibration) — 10 of the 22 "clarify" answers on the `llm` side were not expected.
* `retrieval_llm` suggested `prepare_ticket` for an out-of-scope request ("office air conditioning too strong") and
  for a network contradiction. The policy still requires a human approval; this is why it does.
* The rules engine missed one category (`mechanical_wheel-04`: "squeaking caster" has no rules keyword).
* Categories are easy on this dataset (strong error codes, short reports): the 1.0 says little about harder inputs.

Not measured: whether each extracted claim is supported by its cited evidence (needs a human rubric; citation ids are
valid by construction), and energy or cost (remote model, free tier).

## Day 2: dev baseline and prompt v2 (in progress)

* Dev baseline, prompt v1 (`reports/20261010T131909Z`, 24 dev cases): category macro-F1 `llm` 0.921 / `retrieval_llm` 0.958;
  contradiction P/R 0.833/1.0; clarify P/R 0.565/1.0 and 0.706/0.923 — the over-clarification seen on test is confirmed on dev.
* Prompt v2 written from dev errors only (`docs/model-card.md`). Partial run `reports/20261010T133820Z` stopped by the provider's
  daily token limit after 12 dev cases. On the 10 dev cases with valid outputs in both runs: clarify P/R `llm` 0.556/1.0 → 1.0/1.0;
  `retrieval_llm` 0.667/0.8 → 1.0/0.4 (it suggested `prepare_ticket` while reporting a contradiction, 3/3).
  A deterministic post-check now forces `clarify` in that situation. **v2 + post-check is not measured yet.**
* Next: `run_eval.py --llm-split dev` (v2 + post-check), then a single `--llm-split test` run. The test split is no longer fully
  blind for this comparison: its errors were read on 2026-10-09 before v2 existed.

## Files

`datasets/build_cases.py` → `datasets/cases_v1.jsonl` · `runner/run_eval.py`, `runner/report.py` ·
`reports/<run>/{results.jsonl, metrics.json, report.md, meta.json}`.
