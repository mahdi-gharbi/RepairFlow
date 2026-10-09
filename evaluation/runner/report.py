"""Computes metrics from an evaluation run directory (results.jsonl) and writes metrics.json + report.md."""
import json
import pathlib
import statistics
import sys

CATEGORIES = ["battery", "network", "mechanical", "sensor", "other"]
CONFIGS = ["rules", "llm", "retrieval_llm"]
UNREACHABLE = ("ConnectError", "ConnectTimeout", "ReadTimeout", "HTTP 429", "provider HTTP 5")


def unreachable(o: dict) -> bool:
    return not o["valid"] and bool(o.get("errors")) and all(any(k in e for k in UNREACHABLE) for e in o["errors"])
ALLOWED_ACTIONS = {"create_ticket", "notify_team"}


def macro_f1(pairs: list[tuple[str, str]]) -> float | None:
    if not pairs:
        return None
    f1s = []
    for c in CATEGORIES:
        tp = sum(1 for g, p in pairs if g == c and p == c)
        fp = sum(1 for g, p in pairs if g != c and p == c)
        fn = sum(1 for g, p in pairs if g == c and p != c)
        if tp + fp + fn == 0:
            continue
        prec = tp / (tp + fp) if tp + fp else 0.0
        rec = tp / (tp + fn) if tp + fn else 0.0
        f1s.append(2 * prec * rec / (prec + rec) if prec + rec else 0.0)
    return round(sum(f1s) / len(f1s), 3)


def prf(pairs: list[tuple[bool, bool]]) -> dict:
    tp = sum(1 for g, p in pairs if g and p)
    fp = sum(1 for g, p in pairs if not g and p)
    fn = sum(1 for g, p in pairs if g and not p)
    prec = tp / (tp + fp) if tp + fp else None
    rec = tp / (tp + fn) if tp + fn else None
    f1 = 2 * prec * rec / (prec + rec) if prec and rec else (0.0 if prec is not None and rec is not None else None)
    return {"precision": prec and round(prec, 3), "recall": rec and round(rec, 3), "f1": f1 and round(f1, 3), "n": len(pairs)}


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return round(values[min(len(values) - 1, int(round(q * (len(values) - 1))))], 1)


def quality(usable: list[tuple[dict, dict]]) -> dict:
    return {
        "n": len(usable),
        "category_macro_f1": macro_f1([(c["labels"]["category"], p["category"]) for c, p in usable]),
        "category_accuracy": round(sum(c["labels"]["category"] == p["category"] for c, p in usable) / len(usable), 3) if usable else None,
        "contradiction_detection": prf([(c["labels"]["has_contradiction"], bool(p["contradictions"])) for c, p in usable]),
        "clarify_when_expected": prf([(c["labels"]["expects_clarify"], p["suggested_next_step"] == "clarify") for c, p in usable]),
    }


def config_metrics(rows: list[dict], name: str) -> dict:
    outs = [(r["case"], r[name]) for r in rows if r.get(name)]
    if not outs:
        return {"n": 0}
    usable = [(c, o) for c, o in outs if o["proposal"]]
    m = {
        "n": len(outs),
        "category_macro_f1": macro_f1([(c["labels"]["category"], o["proposal"]["category"]) for c, o in usable]),
        "category_accuracy": round(sum(c["labels"]["category"] == o["proposal"]["category"] for c, o in usable) / len(usable), 3) if usable else None,
        "contradiction_detection": prf([(c["labels"]["has_contradiction"], bool(o["proposal"]["contradictions"])) for c, o in usable]),
        "clarify_when_expected": prf([(c["labels"]["expects_clarify"], o["proposal"]["suggested_next_step"] == "clarify") for c, o in usable]),
    }
    if name != "rules":
        reached = [(c, o) for c, o in outs if not unreachable(o)]
        by_case = {r["case"]["id"]: r for r in rows}
        # End-to-end: what the reviewer actually sees (LLM proposal, else the rules fallback).
        system = [(c, o["proposal"] or by_case[c["id"]]["rules"]["proposal"]) for c, o in outs]
        m.update(
            provider_unreachable=len(outs) - len(reached),
            schema_valid_rate_when_reached=round(sum(o["valid"] for _, o in reached) / len(reached), 3) if reached else None,
            when_valid=quality([(c, o["proposal"]) for c, o in usable]),
            system_with_fallback=quality(system),
            schema_valid_rate=round(sum(o["valid"] for _, o in outs) / len(outs), 3),
            valid_first_attempt_rate=round(sum(bool(o.get("valid_first_attempt")) for _, o in outs) / len(outs), 3),
            fallback_to_rules=sum(o["fallback"] for _, o in outs),
            latency_ms_p50=pct([o["duration_ms"] for _, o in outs if o["duration_ms"]], 0.5),
            latency_ms_p95=pct([o["duration_ms"] for _, o in outs if o["duration_ms"]], 0.95),
            tokens_mean=round(statistics.mean([o["tokens"] for _, o in outs if o["tokens"]]), 0) if any(o["tokens"] for _, o in outs) else None,
            models=sorted({o.get("model") for _, o in outs if o.get("model")}),
        )
    else:
        m["latency_ms_p50"] = pct([o["duration_ms"] for _, o in outs if o["duration_ms"] is not None], 0.5)
    return m


def retrieval_metrics(rows: list[dict]) -> dict:
    judged = [r for r in rows if r["case"]["labels"]["relevant_chunks"]]
    recall, rr = [], []
    for r in judged:
        gold, hits = set(r["case"]["labels"]["relevant_chunks"]), r["retrieval"][:3]
        recall.append(len(gold & set(hits)) / len(gold))
        rank = next((i + 1 for i, h in enumerate(hits) if h in gold), None)
        rr.append(1 / rank if rank else 0.0)
    no_gold = [r for r in rows if not r["case"]["labels"]["relevant_chunks"]]
    return {"n_judged": len(judged), "recall_at_3": round(statistics.mean(recall), 3) if recall else None,
            "mrr_at_3": round(statistics.mean(rr), 3) if rr else None,
            "no_relevant_doc_cases": len(no_gold),
            "no_relevant_doc_cases_with_hits": sum(1 for r in no_gold if r["retrieval"])}


def invariants(rows: list[dict]) -> dict:
    bad = []
    for r in rows:
        for a in r["plan"] or []:
            if a["type"] not in ALLOWED_ACTIONS or (a["type"] == "notify_team" and not a["args"]["to"].endswith("@repairflow.local")):
                bad.append((r["case"]["id"], a["type"], a["args"].get("to")))
        if r["case"]["labels"].get("injection"):
            for a in r["plan"] or []:
                if a["type"] == "create_ticket" and a["args"]["priority"] == "P1":
                    bad.append((r["case"]["id"], "priority escalated to P1"))
    return {"unauthorized_actions": len(bad), "details": bad[:10]}


def main(run_dir: pathlib.Path) -> dict:
    rows = [json.loads(l) for l in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    meta = json.loads((run_dir / "meta.json").read_text()) if (run_dir / "meta.json").exists() else {}
    metrics = {"meta": meta, "label_status": sorted({r["case"]["label_status"] for r in rows}), "splits": {}}
    for split in ("dev", "test"):
        part = [r for r in rows if r["case"]["split"] == split]
        metrics["splits"][split] = {"cases": len(part), "configs": {c: config_metrics(part, c) for c in CONFIGS},
                                    "retrieval": retrieval_metrics(part)}
    metrics["invariants"] = invariants(rows)
    errors = {}
    for r in rows:
        for c in ("llm", "retrieval_llm"):
            for e in (r.get(c) or {}).get("errors", []):
                key = e.split(":")[0][:60]
                errors[key] = errors.get(key, 0) + 1
    metrics["llm_error_kinds"] = errors
    (run_dir / "metrics.json").write_text(json.dumps(metrics, indent=2) + "\n", encoding="utf-8", newline="\n")
    (run_dir / "report.md").write_text(render(metrics, rows), encoding="utf-8", newline="\n")
    print(json.dumps({s: {c: {k: v for k, v in m.items() if k in ("n", "category_macro_f1", "schema_valid_rate")}
                          for c, m in metrics["splits"][s]["configs"].items()} for s in metrics["splits"]}, indent=1))
    return metrics


def render(m: dict, rows: list[dict]) -> str:
    meta = m["meta"]
    lines = [f"# Evaluation report {meta.get('run_id', '')}", "",
             f"Dataset `{meta.get('dataset')}`: {meta.get('cases')} synthetic cases. **Labels: {', '.join(m['label_status'])}** "
             "(written by the same AI assistant that wrote the cases; not a gold standard until reviewed by a human).",
             f"LLM configurations were run on split `{meta.get('llm_split')}`. Throttled provider attempts (HTTP 429), retried and "
             f"excluded from validity: {meta.get('throttled_attempts')}. Wall time: {meta.get('wall_s')} s.",
             *[f"Re-run {r['at']}: {len(r['cases'])} case(s) ({r['reason']}): {', '.join(r['cases'])}." for r in meta.get("reruns", [])],
             "", "Model-quality columns for `llm` / `retrieval_llm` are computed **only on valid model outputs** (n shown); "
             "the end-to-end table below includes the rules fallback, which is what a reviewer actually receives.", ""]
    for split, s in m["splits"].items():
        lines += [f"## Split `{split}` ({s['cases']} cases)", "",
                  "| config | n | category macro-F1 | accuracy | contradiction P / R | clarify P / R | schema valid | 1st attempt | fallbacks | p50 / p95 ms |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for c, x in s["configs"].items():
            if not x.get("n"):
                lines.append(f"| {c} | 0 | – | – | – | – | – | – | – | – |")
                continue
            cd, cl = x["contradiction_detection"], x["clarify_when_expected"]
            lines.append(f"| {c} | {x['n']} | {x['category_macro_f1']} | {x['category_accuracy']} | {cd['precision']} / {cd['recall']} | "
                         f"{cl['precision']} / {cl['recall']} | {x.get('schema_valid_rate', 'n/a')} | {x.get('valid_first_attempt_rate', 'n/a')} | "
                         f"{x.get('fallback_to_rules', 'n/a')} | {x.get('latency_ms_p50')} / {x.get('latency_ms_p95', '–')} |")
        llm_cfgs = [(c, x) for c, x in s["configs"].items() if c != "rules" and x.get("n")]
        if llm_cfgs:
            lines += ["", "| config | provider unreachable | schema valid when reached | n valid | end-to-end macro-F1 (with fallback) | end-to-end contradiction P / R | end-to-end clarify P / R |",
                      "|---|---|---|---|---|---|---|"]
            for c, x in llm_cfgs:
                e = x["system_with_fallback"]
                lines.append(f"| {c} | {x['provider_unreachable']} | {x['schema_valid_rate_when_reached']} | {x['when_valid']['n']} | "
                             f"{e['category_macro_f1']} | {e['contradiction_detection']['precision']} / {e['contradiction_detection']['recall']} | "
                             f"{e['clarify_when_expected']['precision']} / {e['clarify_when_expected']['recall']} |")
        r = s["retrieval"]
        lines += ["", f"Retrieval (lexical, top-3, {r['n_judged']} judged cases): Recall@3 = {r['recall_at_3']}, MRR@3 = {r['mrr_at_3']}; "
                      f"cases with no relevant document: {r['no_relevant_doc_cases']} (of which returned hits anyway: {r['no_relevant_doc_cases_with_hits']}).", ""]
    inv = m["invariants"]
    lines += ["## Policy invariant", "", f"Unauthorized actions in generated plans: **{inv['unauthorized_actions']}** "
              "(actions, priority and recipients come from the deterministic policy; this is an invariant check, not a model score).", ""]
    if m["llm_error_kinds"]:
        lines += ["## LLM error kinds (all attempts)", ""] + [f"- `{k}`: {v}" for k, v in sorted(m["llm_error_kinds"].items())] + [""]
    lines += ["## Per-case predictions (test split)", "", "| case | gold | rules | llm | retrieval_llm |", "|---|---|---|---|---|"]
    for r in rows:
        if r["case"]["split"] != "test":
            continue
        def cell(c):
            o = r.get(c)
            if not o:
                return "–"
            if not o["proposal"]:
                return "invalid"
            p = o["proposal"]
            return f"{p['category']}{' C' if p['contradictions'] else ''}{' ?' if p['suggested_next_step'] == 'clarify' else ''}"
        g = r["case"]["labels"]
        lines.append(f"| {r['case']['id']} | {g['category']}{' C' if g['has_contradiction'] else ''}{' ?' if g['expects_clarify'] else ''} | "
                     f"{cell('rules')} | {cell('llm')} | {cell('retrieval_llm')} |")
    lines += ["", "Legend: `C` = contradiction reported, `?` = next step `clarify`.", "",
              "Not measured: whether each extracted claim is actually supported by its cited evidence (needs a human rubric); "
              "citation ids are valid by construction (schema enum + server validation)."]
    return "\n".join(lines) + "\n"


if __name__ == "__main__":
    main(pathlib.Path(sys.argv[1]))
