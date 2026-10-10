"""Evaluation campaign through the real API (PostgreSQL, rules engine, retrieval, remote LLM).

    python evaluation/runner/run_eval.py                 # rules on all cases, LLM configs on the test split
    python evaluation/runner/run_eval.py --llm-split all
    python evaluation/runner/run_eval.py --retry-unreachable evaluation/reports/<run>   # redo provider-outage cases
    python evaluation/runner/report.py <run dir>         # recompute metrics/report from results.jsonl

Configurations: rules (no LLM) | llm (no documents in context) | retrieval_llm (lexical manual evidence).
Each case creates two incidents (without / with manual evidence) under source_scope `eval-<run>`, which
are rejected at the end so they do not stay in the review queue. LLM calls are paced under the provider's
tokens-per-minute limit; throttled attempts are retried and counted separately.
"""
import argparse
import json
import pathlib
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

ROOT = pathlib.Path(__file__).resolve().parents[2]
ENV = dict(l.split("=", 1) for l in (ROOT / ".env").read_text().splitlines() if "=" in l and not l.startswith("#"))
API = "http://127.0.0.1:18000"
INTERNAL = {"X-RepairFlow-Internal": ENV["RF_INTERNAL_KEY"]}
REVIEWER = {"Authorization": f"Bearer {ENV['RF_REVIEWER_TOKEN']}"}
TPM_BUDGET = 6500  # stay under the free-tier 8000 tokens/minute
UNREACHABLE = ("ConnectError", "ConnectTimeout", "ReadTimeout", "HTTP 429", "provider HTTP 5")


def unreachable(outcome: dict | None) -> bool:
    """The configured provider was never reached for this case (network/DNS outage, throttling)."""
    return bool(outcome) and not outcome["valid"] and bool(outcome.get("errors")) and all(
        any(k in e for k in UNREACHABLE) for e in outcome["errors"])


def call(method, path, body=None, headers=None, timeout=120):
    req = urllib.request.Request(API + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read() or b"null")
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"null")


class Pacer:
    def __init__(self):
        self.window: list[tuple[float, int]] = []

    def wait(self, upcoming: int = 3000):
        while True:
            now = time.monotonic()
            self.window = [(t, n) for t, n in self.window if now - t < 60]
            if sum(n for _, n in self.window) + upcoming <= TPM_BUDGET:
                return
            time.sleep(2)

    def add(self, tokens: int):
        self.window.append((time.monotonic(), tokens or 3000))


def new_incident(case: dict, run_id: str, suffix: str, with_manuals: bool) -> tuple[str, dict | None]:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    report = {"event_id": f"eval-{run_id}-{case['id']}-{suffix}", "equipment_id": case["equipment_id"],
              "reported_at": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "language": case["language"], "description": case["description"]}
    status, body = call("POST", "/v1/incidents", {"source_scope": f"eval-{run_id}", "report": report}, INTERNAL)
    assert status == 201, body
    iid = body["incident_id"]
    tel = case["telemetry"]
    evidence = ({"source_id": "telemetry-sim", "available": False, "values": {}, "error": "HTTP 503 (evaluation case)"} if tel is None else
                {"source_id": "telemetry-sim", "available": True, "values": tel["values"],
                 "observed_at": (now + timedelta(seconds=tel["observed_offset_s"])).isoformat()})
    call("POST", f"/v1/incidents/{iid}/evidence/telemetry", evidence, INTERNAL)
    retrieval = None
    if with_manuals:
        _, retrieval = call("POST", f"/v1/incidents/{iid}/evidence/manuals", None, INTERNAL)
    return iid, retrieval


def triage(iid: str, engine: str, pacer: Pacer, stats: dict) -> None:
    if engine == "rules":
        call("POST", f"/v1/incidents/{iid}/triage?engine=rules", None, INTERNAL)
        return
    for _ in range(4):
        pacer.wait()
        call("POST", f"/v1/incidents/{iid}/triage", None, INTERNAL)
        _, runs = call("GET", f"/v1/incidents/{iid}/triage-runs", headers=REVIEWER)
        latest = [r for r in runs if r["engine"] != "rules"]
        tokens = sum(((r.get("params") or {}).get("response") or {}).get("usage", {}).get("total_tokens", 0) or 0 for r in latest[-2:])
        pacer.add(tokens)
        if latest and any("HTTP 429" in e for e in latest[-1]["errors"]):
            stats["throttled"] += 1
            time.sleep(15)
            continue
        return


def pick(runs: list[dict], *engines: str) -> dict:
    """Outcome of one configuration: the first non-throttled attempt chain for that engine.
    With manual retrieval enabled but no section found, the API labels the run `llm`: same configuration."""
    engine = engines[0]
    chain = [r for r in runs if r["engine"] in engines and not any("HTTP 429" in e for e in r["errors"])]
    if engine == "rules":
        r = next((r for r in chain if r["valid"]), None)
        return {"proposal": r and r["proposal"], "valid": bool(r), "attempts": 1, "fallback": False,
                "duration_ms": r and r["duration_ms"], "tokens": 0}
    valid = next((r for r in chain if r["valid"]), None)
    return {"proposal": valid and valid["proposal"], "valid": bool(valid), "valid_first_attempt": bool(chain and chain[0]["valid"]),
            "attempts": len(chain), "fallback": not valid, "errors": [e for r in chain for e in r["errors"]][:4],
            "duration_ms": sum(r["duration_ms"] for r in chain) if chain else None, "model": chain[0]["model"] if chain else None,
            "tokens": sum(((r.get("params") or {}).get("response") or {}).get("usage", {}).get("total_tokens", 0) or 0 for r in chain)}


def run_case(case: dict, run_id: str, use_llm: bool, pacer: Pacer, stats: dict) -> dict:
    a, _ = new_incident(case, run_id, "a", with_manuals=False)
    b, retrieval = new_incident(case, run_id, "b", with_manuals=True)
    triage(b, "rules", pacer, stats)  # production configuration: rules with manual evidence
    if use_llm:
        triage(a, "llm", pacer, stats)
        triage(b, "retrieval_llm", pacer, stats)
    _, runs_a = call("GET", f"/v1/incidents/{a}/triage-runs", headers=REVIEWER)
    _, runs_b = call("GET", f"/v1/incidents/{b}/triage-runs", headers=REVIEWER)
    _, detail = call("GET", f"/v1/incidents/{b}", headers=REVIEWER)
    row = {"case": case, "incidents": [a, b],
           "retrieval": [h["chunk_id"] for h in (retrieval or {}).get("hits", [])],
           "rules": pick(runs_b, "rules"),
           "llm": pick(runs_a, "llm") if use_llm else None,
           "retrieval_llm": pick(runs_b, "retrieval_llm", "llm") if use_llm else None,
           "plan": detail["review"]["plan"]}
    for iid in (a, b):
        _, d = call("GET", f"/v1/incidents/{iid}", headers=REVIEWER)
        call("POST", f"/v1/incidents/{iid}/reject", {"expected_revision": d["revision"], "reason": f"evaluation run {run_id}"}, REVIEWER)
    return row


def retry_unreachable(run_dir: pathlib.Path) -> None:
    rows = [json.loads(l) for l in (run_dir / "results.jsonl").read_text(encoding="utf-8").splitlines() if l.strip()]
    meta = json.loads((run_dir / "meta.json").read_text())
    pacer, stats = Pacer(), {"throttled": 0}
    redone = []
    for i, row in enumerate(rows):
        if unreachable(row.get("llm")) or unreachable(row.get("retrieval_llm")):
            rows[i] = run_case(row["case"], meta["run_id"] + "-retry", True, pacer, stats)
            redone.append(row["case"]["id"])
            print(f"re-ran {row['case']['id']}", flush=True)
    (run_dir / "results.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows),
                                           encoding="utf-8", newline="\n")
    meta.setdefault("reruns", []).append({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                                          "reason": "provider unreachable (network/DNS) during the first pass",
                                          "cases": redone})
    (run_dir / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8", newline="\n")
    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    import report
    report.main(run_dir)


# Model text can contain any character; Windows consoles/pipes default to cp1252.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default=str(ROOT / "evaluation/datasets/cases_v1.jsonl"))
    ap.add_argument("--llm-split", default="test", choices=["test", "dev", "all", "none"])
    ap.add_argument("--retry-unreachable", metavar="RUN_DIR")
    args = ap.parse_args()
    if args.retry_unreachable:
        return retry_unreachable(pathlib.Path(args.retry_unreachable))
    cases = [json.loads(l) for l in pathlib.Path(args.dataset).read_text(encoding="utf-8").splitlines() if l.strip()]
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = ROOT / "evaluation" / "reports" / run_id
    out.mkdir(parents=True, exist_ok=True)
    pacer, stats = Pacer(), {"throttled": 0}
    _, me = call("GET", "/v1/me", headers=REVIEWER)
    started = time.monotonic()
    with (out / "results.jsonl").open("w", encoding="utf-8", newline="\n") as f:
        for n, case in enumerate(cases, 1):
            use_llm = args.llm_split == "all" or case["split"] == args.llm_split
            row = run_case(case, run_id, use_llm, pacer, stats)
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
            f.flush()
            print(f"[{n}/{len(cases)}] {case['id']:<26} rules={row['rules']['proposal']['category'] if row['rules']['proposal'] else '-'}"
                  + (f" llm={'ok' if row['llm']['valid'] else 'fail'} rllm={'ok' if row['retrieval_llm']['valid'] else 'fail'}" if use_llm else ""),
                  flush=True)
    meta = {"run_id": run_id, "dataset": pathlib.Path(args.dataset).name, "cases": len(cases), "llm_split": args.llm_split,
            "throttled_attempts": stats["throttled"], "wall_s": round(time.monotonic() - started), "api_user": me,
            "llm": {"provider": "Groq (OpenAI-compatible)", "model": "see results (model per run)"},
            "hardware": "client: i7-10510U / 20 GB RAM laptop; inference is remote (provider hardware unknown)"}
    (out / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8", newline="\n")
    print(f"wrote {out.relative_to(ROOT)}")
    sys.path.insert(0, str(pathlib.Path(__file__).parent))
    import report
    report.main(out)


if __name__ == "__main__":
    main()
