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

## Day 2: prompt v2 + deterministic post-check (2026-10-10)

All runs below: same model `openai/gpt-oss-20b`, called through **OpenRouter pinned to the Groq upstream** (no fallback,
`require_parameters`; every run recorded `served_by: Groq`). The 09/10 v1 runs used Groq directly — same upstream,
different account/route. Cost of the three runs: USD 0.028.

**Dev split (24 cases), v1 vs v2 + post-check, identical conditions** (`reports/20261010T143707Z` vs `reports/20261010T143914Z`):

| config | category macro-F1 | contradiction P / R | clarify P / R |
|---|---|---|---|
| llm v1 | 0.921 | 0.714 / 1.0 | 0.542 / 1.0 |
| llm v2 | 0.921 | 0.8 / 0.8 | 1.0 / 0.846 |
| retrieval_llm v1 | 0.958 | 0.75 / 0.6 | 0.722 / 1.0 |
| retrieval_llm v2 | 0.958 | 1.0 / 1.0 | 0.923 / 0.923 |

Decision taken on dev: adopt v2 + post-check (over-clarification removed; one fewer `llm` contradiction found). The post-check
changed the model's next step in 1 of 49 valid dev runs; most of the gain comes from the prompt.

**Test split (27 cases), single run of v2 + post-check** (`reports/20261010T144107Z`), next to the 09/10 v1 test run:

| config | category macro-F1 | contradiction P / R | clarify P / R |
|---|---|---|---|
| rules | 0.967 | 1.0 / 1.0 | 1.0 / 1.0 |
| llm v1 (09/10) | 1.0 | 0.714 / 1.0 | 0.545 / 0.923 |
| llm v2 | 1.0 | 0.714 / 1.0 | 0.867 / 1.0 |
| retrieval_llm v1 (09/10) | 1.0 | 0.714 / 1.0 | 0.556 / 0.769 |
| retrieval_llm v2 | 0.912 | 0.833 / 1.0 | 0.929 / 1.0 |

Schema valid 27/27 for both LLM configurations; 0 unauthorized actions. Remaining errors: stale telemetry still read as a
contradiction even with the deterministic `stale` flag (the post-check still forces `clarify`); `retrieval_llm` put one
injection case in `sensor` and one stale case in `other`.

How much to trust this:
* **Run-to-run variability is large.** The same v1 prompt on dev moved `retrieval_llm` contradiction recall from 1.0 (Groq
  direct, 10/10 13:19) to 0.6 (OpenRouter→Groq, 14:37). Differences of one or two cases are within that noise.
* **The test split was not blind** to v2's motivation: its v1 errors were read on 09/10. v2 was written from dev errors only.
* **Labels are still unreviewed drafts** by the same author as the rules engine, and on stale telemetry v2 aligns the model
  with that author's convention.
* One run per configuration; no confidence intervals.

## Day 2, evening: deterministic safety net and a rules fix (2026-10-10)

* **Safety net** (`add_rule_contradictions`): contradictions the rules engine proves on the same evidence manifest are added to
  a model proposal that missed them (marked `[rules]`), then `clarify` is enforced. Motivated by the demo recording, where the
  real model once missed the flagship contradiction. Run `reports/20261010T191106Z` (dev + test, v2 + post-check + net):
  test contradiction P/R `llm` 0.833/1.0, `retrieval_llm` 1.0/1.0; clarify P/R 0.867/1.0 and 0.929/1.0; category macro-F1 1.0.
  **In this run the net added nothing** (the model found every rule-detectable contradiction); the next-step rule fired 5 times.
  The net is insurance against a known failure, not a measured gain. This was a second look at the test split.
* **Rules fix from a dev case** (`rules-2026.10-3`): "le wifi fonctionne sur la tablette" was not recognised as a network-OK claim.
  Rules-only run `reports/20261010T191749Z`: dev contradiction recall 0.8 → 1.0, clarify recall 0.923 → 1.0; test unchanged.

## Files

`datasets/build_cases.py` → `datasets/cases_v1.jsonl` · `runner/run_eval.py`, `runner/report.py` ·
`reports/<run>/{results.jsonl, metrics.json, report.md, meta.json}`.
