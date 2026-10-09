import { useEffect, useState } from "react";
import { Api, ApiError } from "./api";
import { Pill } from "./ui";

interface Summary {
  provenance: string | null; category: string | null; suggested_next_step: string | null; contradictions: number;
  missing_information: number; authorized_actions: { type: string; priority?: string; to?: string }[];
  plan_digest: string | null; actions?: { type: string; status: string; attempts: number }[];
  approval_still_valid?: boolean; tickets_created?: number; emails_sent?: number; human_decision_needed?: boolean;
  notes?: string[]; deliveries?: Record<string, number>; approved?: boolean; incident_status?: string;
}

export interface ReplayRun {
  id: string; scenario: string; mode: string; created_at: string; created_by: string; source_revision: number;
  source: Summary; replay: Summary; comparison: { field: string; source: unknown; replay: unknown }[];
  trace: { t: number; event: string; [k: string]: unknown }[];
}

const ROWS: [keyof Summary, string][] = [
  ["provenance", "proposal provenance"], ["category", "category"], ["suggested_next_step", "next step"],
  ["contradictions", "contradictions"], ["missing_information", "missing information"],
];

function actionsText(s: Summary): string {
  return s.authorized_actions.length
    ? s.authorized_actions.map((a) => `${a.type}${a.priority ? ` ${a.priority}` : ""}${a.to ? ` → ${a.to}` : ""}`).join(", ")
    : "none authorized";
}

export function Replay({ api, incidentId, canRun }: { api: Api; incidentId: string; canRun: boolean }) {
  const [catalog, setCatalog] = useState<{ scenarios: Record<string, string>; modes: string[] } | null>(null);
  const [scenario, setScenario] = useState("ticket_503_twice");
  const [mode, setMode] = useState("frozen");
  const [runs, setRuns] = useState<ReplayRun[]>([]);
  const [current, setCurrent] = useState<ReplayRun | null>(null);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api.replayScenarios().then(setCatalog).catch(() => undefined);
    api.replays(incidentId).then((r) => { setRuns(r); setCurrent(r[0] ?? null); }).catch(() => undefined);
  }, [api, incidentId]);

  const start = async () => {
    setBusy(true); setError("");
    try {
      const r = await api.runReplay(incidentId, scenario, mode);
      setRuns((prev) => [r, ...prev]);
      setCurrent(r);
    } catch (e) {
      const err = e as ApiError;
      setError(`${err.status ?? ""} ${err.code ?? ""}: ${err.message}`);
    } finally { setBusy(false); }
  };

  return (
    <div className="card">
      <p className="muted small">
        A replay copies this incident's immutable snapshot into a sandbox. It uses the live rules, policy, digest and retry
        decisions, but scripted connectors: it never creates actions, never calls n8n, the ticket service or SMTP, and
        cannot re-trigger the original run. <b>frozen</b> re-validates the recorded model output; <b>requery</b> asks the model
        again (results can vary).
      </p>
      {canRun && catalog && (
        <div className="actions-row">
          <select aria-label="scenario" value={scenario} onChange={(e) => setScenario(e.target.value)}>
            {Object.keys(catalog.scenarios).map((s) => <option key={s} value={s}>{s}</option>)}
          </select>
          <select aria-label="mode" value={mode} onChange={(e) => setMode(e.target.value)} style={{ maxWidth: 130 }}>
            {catalog.modes.map((m) => <option key={m}>{m}</option>)}
          </select>
          <button className="btn" disabled={busy} onClick={start} data-testid="run-replay">{busy ? "Running…" : "Run replay"}</button>
        </div>
      )}
      {catalog && <p className="small muted">{catalog.scenarios[scenario]}</p>}
      {error && <div className="banner err">{error}</div>}

      {current && (
        <div data-testid="replay-result">
          <h3>{current.scenario} · {current.mode} · source revision {current.source_revision}</h3>
          <div className="table-wrap"><table>
            <thead><tr><th></th><th>source run</th><th>replay</th></tr></thead>
            <tbody>
              {ROWS.map(([k, label]) => {
                const changed = current.comparison.some((c) => c.field === k);
                return <tr key={k} className={changed ? "hl" : ""}><td>{label}</td><td>{String(current.source[k] ?? "–")}</td><td>{String(current.replay[k] ?? "–")}</td></tr>;
              })}
              <tr className={current.comparison.some((c) => c.field === "authorized_actions") ? "hl" : ""}>
                <td>authorized actions</td><td>{actionsText(current.source)}</td><td>{actionsText(current.replay)}</td></tr>
              <tr className={current.replay.approval_still_valid ? "" : "hl"}>
                <td>approval</td><td>{current.source.approved ? "approved" : "not approved"}</td>
                <td>{current.replay.approval_still_valid ? "same digest: still valid" : "digest changed: new review required"}</td></tr>
              <tr><td>actions</td>
                <td>{(current.source.actions ?? []).map((a) => <div key={a.type}>{a.type} <Pill value={a.status} /> ×{a.attempts}</div>)}</td>
                <td>{(current.replay.actions ?? []).map((a) => <div key={a.type}>{a.type} <Pill value={a.status} /> ×{a.attempts}</div>)}</td></tr>
              <tr><td>effects in sandbox</td><td>–</td>
                <td>{current.replay.tickets_created} ticket(s), {current.replay.emails_sent} email(s)
                  {current.replay.human_decision_needed && <> · <span className="warn-text">human decision needed</span></>}</td></tr>
              {current.replay.deliveries && <tr><td>deliveries</td><td>–</td>
                <td>{Object.entries(current.replay.deliveries).map(([k, v]) => `${k} ${v}`).join(", ")}</td></tr>}
            </tbody>
          </table></div>
          {(current.replay.notes ?? []).map((n, i) => <div key={i} className="banner info small">{n}</div>)}
          <details><summary>sandbox trace ({current.trace.length} steps)</summary>
            <ol className="timeline">{current.trace.map((s, i) => {
              const { t, event, ...rest } = s;
              return <li key={i}><span className="mono small muted">t+{t}s</span>
                <span><b>{event}</b> <span className="muted small mono">{Object.keys(rest).length ? JSON.stringify(rest) : ""}</span></span></li>;
            })}</ol>
          </details>
        </div>
      )}
      {runs.length > 1 && (<>
        <h3>Previous replays</h3>
        <ul>{runs.map((r) => <li key={r.id}><a href="#" onClick={(e) => { e.preventDefault(); setCurrent(r); }}>
          {r.scenario} · {r.mode}</a> <span className="muted small">{r.created_at.slice(11, 19)} by {r.created_by}</span></li>)}</ul>
      </>)}
    </div>
  );
}
