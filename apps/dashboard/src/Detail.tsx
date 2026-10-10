import { useCallback, useEffect, useState } from "react";
import { Action, Api, ApiError, AuditEvent, IncidentDetail, Me, TimelineEntry, TriageRun } from "./api";
import { Replay } from "./Replay";
import { Pill, time } from "./ui";

type Tab = "review" | "actions" | "timeline" | "replay" | "technical";

export function Detail({ api, me, id, lastEvent }: { api: Api; me: Me; id: string; lastEvent: AuditEvent | null }) {
  const [shown, setShown] = useState<IncidentDetail | null>(null);
  const [latest, setLatest] = useState<IncidentDetail | null>(null);
  const [timeline, setTimeline] = useState<TimelineEntry[]>([]);
  const [runs, setRuns] = useState<TriageRun[]>([]);
  const [tab, setTab] = useState<Tab>("review");
  const [flash, setFlash] = useState<{ kind: "ok" | "err"; text: string } | null>(null);
  const [busy, setBusy] = useState(false);
  const [highlight, setHighlight] = useState<string | null>(null);

  const load = useCallback(async (adopt: boolean) => {
    const [d, tl, tr] = await Promise.all([api.incident(id), api.timeline(id),
      me.role === "reviewer" ? api.triageRuns(id) : Promise.resolve([] as TriageRun[])]);
    setLatest(d);
    setTimeline(tl);
    setRuns(tr);
    // Keep the reviewed revision on screen when a newer one arrives: approving must be a conscious act.
    setShown((cur) => (adopt || !cur || cur.revision === d.revision ? d : cur));
  }, [api, id, me.role]);

  useEffect(() => { load(true).catch((e) => setFlash({ kind: "err", text: String(e.message ?? e) })); }, [load]);
  useEffect(() => {
    if (lastEvent && lastEvent.incident_id === id) load(false).catch(() => undefined);
  }, [lastEvent, id, load]);

  if (!shown || !latest) return <div className="card pad muted">Loading…</div>;
  const stale = latest.revision !== shown.revision;
  const canReview = me.role === "reviewer";

  const run = async (label: string, fn: () => Promise<unknown>, adopt = true) => {
    setBusy(true);
    setFlash(null);
    try {
      await fn();
      setFlash({ kind: "ok", text: label });
      await load(adopt);
    } catch (e) {
      const err = e as ApiError;
      setFlash({ kind: "err", text: `${err.status ?? ""} ${err.code ?? ""}: ${err.message}` });
      await load(false);
    } finally {
      setBusy(false);
    }
  };

  const cite = (ref: string) => {
    setTab("review");
    setHighlight(ref);
    window.setTimeout(() => document.getElementById(`ev-${ref}`)?.scrollIntoView({ block: "center", behavior: "smooth" }), 50);
  };

  return (
    <div>
      <div className="card">
        <div className="title-row">
          <h2>{shown.equipment.display_name} <span className="muted">({shown.equipment_id}, {shown.equipment.model}, {shown.equipment.site})</span></h2>
          <Pill value={latest.status} />
        </div>
        <div className="muted mono small">incident {shown.id} · revision {shown.revision} · correlation {shown.correlation_id}</div>
        {stale && (
          <div className="banner warn" role="alert" data-testid="stale-banner">
            This incident changed while you were reviewing: revision {latest.revision} is now current
            (you are looking at revision {shown.revision}). Approval is disabled until you load it.
            <button className="btn small" onClick={() => setShown(latest)}>Load revision {latest.revision}</button>
          </div>
        )}
        {flash && <div className={`banner ${flash.kind}`} role="status" data-testid="flash">{flash.text}</div>}
        <nav className="tabs" role="tablist">
          {(["review", "actions", "timeline", "replay", "technical"] as Tab[]).filter((t) => t !== "technical" || canReview).map((t) => (
            <button key={t} role="tab" aria-selected={tab === t} className={tab === t ? "on" : ""} onClick={() => setTab(t)}>
              {t}{t === "actions" && latest.actions.some((a) => a.status === "uncertain") ? " ⚠" : ""}
            </button>
          ))}
        </nav>
      </div>

      {tab === "review" && <Review d={shown} canReview={canReview && !stale && shown.status === "needs_review"} busy={busy}
                                   highlight={highlight} cite={cite} run={run} api={api} stale={stale} />}
      {tab === "actions" && <Actions actions={latest.actions} canReview={canReview} busy={busy} run={run} api={api} />}
      {tab === "timeline" && <Timeline entries={timeline} actions={latest.actions} />}
      {tab === "replay" && <Replay api={api} incidentId={shown.id} canRun={canReview} />}
      {tab === "technical" && <Technical runs={runs} />}
    </div>
  );
}

type Runner = (label: string, fn: () => Promise<unknown>, adopt?: boolean) => Promise<void>;

function Refs({ refs, cite }: { refs: string[]; cite: (r: string) => void }) {
  return <span className="refs">[{refs.map((r, i) => (
    <span key={r}>{i > 0 && ", "}<a href={`#ev-${r}`} onClick={(e) => { e.preventDefault(); cite(r); }}>{r}</a></span>))}]</span>;
}

function Review({ d, canReview, busy, highlight, cite, run, api, stale }: {
  d: IncidentDetail; canReview: boolean; busy: boolean; highlight: string | null; cite: (r: string) => void;
  run: Runner; api: Api; stale: boolean;
}) {
  const p = d.review.proposal;
  const ov = d.revision_data.overrides ?? {};
  const [category, setCategory] = useState("");
  const [title, setTitle] = useState(ov.ticket_title ?? p?.draft_ticket.title ?? "");
  const [desc, setDesc] = useState(ov.ticket_description ?? p?.draft_ticket.description ?? "");
  const [note, setNote] = useState("");
  useEffect(() => {
    setTitle(ov.ticket_title ?? p?.draft_ticket.title ?? "");
    setDesc(ov.ticket_description ?? p?.draft_ticket.description ?? "");
    setCategory("");
  }, [d.revision]);

  return (
    <>
      <div className="card">
        <h3>Operator report</h3>
        <blockquote>{d.revision_data.description}</blockquote>
        <div className="muted small">reported {time(d.revision_data.reported_at)} · language {d.revision_data.language}</div>
        {d.revision_data.notes.length > 0 && (<>
          <h3>Reviewer notes</h3>
          <ul>{d.revision_data.notes.map((n, i) => <li key={i}><span className="muted">{n.by}, {time(n.at)}:</span> {n.note}</li>)}</ul>
        </>)}
        <h3>Evidence (revision {d.revision})</h3>
        <div className="table-wrap"><table>
          <thead><tr><th>id</th><th>kind</th><th>source · version</th><th>observed</th><th>content</th></tr></thead>
          <tbody>{d.evidence.map((e) => (
            <tr key={e.id} id={`ev-${e.id}`} className={highlight === e.id ? "hl" : ""}>
              <td className="mono nowrap">{e.id}</td><td>{e.kind}</td>
              <td className="mono small">{e.source_id}<br />{e.source_version}</td>
              <td className="mono small">{time(e.observed_at)}</td>
              <td className="small">{e.kind === "telemetry" ? (e.content.available ? <code>{JSON.stringify(e.content.values)}</code>
                                        : <span className="warn-text">unavailable: {e.content.error}</span>)
                : e.kind === "manual" ? <><b>{e.content.title}</b> — {e.content.section}<br /><span className="muted">{e.content.text}</span></>
                : e.content.description}</td>
            </tr>))}</tbody>
        </table></div>
      </div>

      {!p ? (
        <div className="card banner warn">No valid proposal for revision {d.revision}.
          {canReview && <button className="btn small" disabled={busy} onClick={() => run("triage done", () => api.triage(d.id))}>Run triage</button>}
        </div>
      ) : (
        <div className="card">
          <div className="title-row">
            <h3>Proposal</h3>
            <span className="pill info" title="which engine produced this proposal">provenance: {d.review.provenance}</span>
            <span className="pill mute">next step: {p.suggested_next_step}</span>
          </div>
          <p>{p.summary}</p>
          {(d.review.adjustments ?? []).map((a, i) => (
            <div key={i} className="banner info small" data-testid="adjustment"><b>Adjusted by rule</b>: {a}</div>))}
          {p.contradictions.map((c, i) => (
            <div key={i} className="banner warn" data-testid="contradiction"><b>Contradiction</b>: {c.claim_a} ↔ {c.claim_b} <Refs refs={c.evidence_refs} cite={cite} /></div>))}
          {p.missing_information.map((m, i) => <div key={i} className="banner info"><b>Missing</b>: {m}</div>)}
          <h3>Facts</h3>
          <ul>{p.extracted_facts.map((f, i) => <li key={i}>{f.claim} <Refs refs={f.evidence_refs} cite={cite} /></li>)}</ul>
          <h3>Plan to approve <span className="muted small">(policy {d.review.policy_version}; actions, priority and recipient come from policy, not from the model)</span></h3>
          <div className="table-wrap"><table>
            <tbody>{(d.review.plan ?? []).map((a) => (
              <tr key={a.type}><td><b>{a.type}</b>{a.depends_on && <div className="muted small">after {a.depends_on}</div>}</td>
                <td className="small">{a.type === "create_ticket"
                  ? <><b>{a.args.priority}</b> · {a.args.category} · {a.args.title}<details><summary>description</summary><pre>{a.args.description}</pre></details></>
                  : <>to <b>{a.args.to}</b> · {a.args.subject}</>}</td></tr>))}</tbody>
          </table></div>
          <div className="muted mono small" data-testid="digest">plan digest {d.review.plan_digest}</div>

          {canReview ? (
            <div className="review-form">
              <h3>Correct (creates revision {d.revision + 1}; the plan must be approved again)</h3>
              <div className="grid2">
                <label>Category
                  <select value={category} onChange={(e) => setCategory(e.target.value)}>
                    <option value="">keep {ov.category ?? p.category}</option>
                    {["battery", "network", "mechanical", "sensor", "other"].map((c) => <option key={c}>{c}</option>)}
                  </select>
                </label>
                <label>Ticket title<input value={title} onChange={(e) => setTitle(e.target.value)} /></label>
              </div>
              <label>Ticket description<textarea rows={6} value={desc} onChange={(e) => setDesc(e.target.value)} /></label>
              <label>Note (why)<input value={note} onChange={(e) => setNote(e.target.value)} placeholder="required for a correction" /></label>
              <div className="actions-row">
                <button className="btn ghost" disabled={busy || !note.trim()} onClick={() => run("correction saved: new revision",
                  () => api.correct(d.id, { expected_revision: d.revision, note, category: category || null,
                                            ticket_title: title, ticket_description: desc }))}>Save correction</button>
                <button className="btn" data-testid="approve" disabled={busy} onClick={() => run(`revision ${d.revision} approved`,
                  () => api.approve(d.id, d.revision, d.review.plan_digest!))}>Approve revision {d.revision}</button>
                <button className="btn danger" disabled={busy} onClick={() => run("rejected",
                  () => api.reject(d.id, d.revision, note || "rejected by reviewer"))}>Reject</button>
                <button className="btn ghost" disabled={busy} onClick={() => run("triage re-run", () => api.triage(d.id))}>Re-run triage</button>
              </div>
            </div>
          ) : stale ? <p className="muted">Load the current revision to review it.</p>
            : d.status !== "needs_review" ? <p className="muted">This incident is {d.status}; nothing to approve.</p>
            : <p className="muted">Read-only: approval needs the reviewer role.</p>}
        </div>
      )}
    </>
  );
}

const UNCERTAIN_HELP: Record<string, string> = {
  create_ticket: "The ticket service deduplicates on the idempotency key. The worker looks the ticket up by key and either records it or re-dispatches safely.",
  notify_team: "SMTP has no idempotency key: the email may or may not have been sent. Reconcile (searches Mailpit for the message marker in this demo) or decide. Retrying accepts the risk of a duplicate email.",
};

function Actions({ actions, canReview, busy, run, api }: { actions: Action[]; canReview: boolean; busy: boolean; run: Runner; api: Api }) {
  if (!actions.length) return <div className="card pad muted">No action yet: actions are created by an approval.</div>;
  return (
    <div className="card">
      <div className="table-wrap"><table>
        <thead><tr><th>type</th><th>status</th><th>attempts</th><th>provider reference</th><th>last error</th></tr></thead>
        <tbody>{actions.map((a) => (
          <tr key={a.id}><td><b>{a.type}</b><div className="mono small muted">{a.action_key}</div></td><td><Pill value={a.status} /></td>
            <td>{a.attempts} <span className="muted small">(offers {a.offers}, fence {a.fencing_token})</span></td>
            <td className="mono small">{a.provider_reference ?? "–"}</td>
            <td className="mono small">{a.last_error ? `${a.last_error.outcome ?? ""} ${a.last_error.http_status ?? ""} ${a.last_error.detail ?? ""}` : ""}</td></tr>))}
        </tbody>
      </table></div>
      {actions.filter((a) => a.status === "uncertain" || a.status === "dead").map((a) => (
        <UncertainPanel key={a.id} a={a} canReview={canReview} busy={busy} run={run} api={api} />))}
    </div>
  );
}

function UncertainPanel({ a, canReview, busy, run, api }: { a: Action; canReview: boolean; busy: boolean; run: Runner; api: Api }) {
  const [ref, setRef] = useState("");
  const [note, setNote] = useState("");
  return (
    <div className="banner warn uncertain" data-testid="uncertain-panel">
      <b>{a.type} is {a.status}.</b> {a.status === "uncertain" ? UNCERTAIN_HELP[a.type] : "The connector rejected the action or retries are exhausted."}
      {canReview && (
        <div className="actions-row">
          {a.status === "uncertain" && <button className="btn small" disabled={busy}
            onClick={() => run("reconciliation done", async () => { const r = await api.reconcile(a.id); if (r.finding !== "found") throw new Error(`finding: ${r.finding}`); })}>Reconcile</button>}
          <input placeholder="provider reference observed" value={ref} onChange={(e) => setRef(e.target.value)} />
          <input placeholder="decision note" value={note} onChange={(e) => setNote(e.target.value)} />
          <button className="btn small ghost" disabled={busy || !ref || !note} onClick={() => run("marked as succeeded", () => api.resolve(a.id, "mark_succeeded", note, ref))}>Mark succeeded</button>
          <button className="btn small ghost" disabled={busy || !note} onClick={() => {
            if (a.type !== "notify_team" || window.confirm("Retrying an email of unknown outcome may send a duplicate. Continue?"))
              run("retry scheduled", () => api.resolve(a.id, "retry", note));
          }}>Retry</button>
          <button className="btn small danger" disabled={busy || !note} onClick={() => run("given up", () => api.resolve(a.id, "give_up", note))}>Give up</button>
        </div>
      )}
    </div>
  );
}

function Timeline({ entries, actions }: { entries: TimelineEntry[]; actions: Action[] }) {
  const kind = Object.fromEntries(actions.map((a) => [a.id, a.type]));
  return (
    <div className="card">
      <ol className="timeline" data-testid="timeline">{entries.map((t) => (
        <li key={t.id} className={/rejected|failed|dead|unknown|expired|stale|superseded|blocked/.test(t.event) ? "bad" : /succeeded|completed|found|approved/.test(t.event) ? "good" : ""}>
          <span className="mono small muted">{time(t.at).slice(11)}</span>
          <span><b>{t.event}</b>{t.action_id && <span className="muted"> · {kind[t.action_id] ?? "action"}</span>}
            {(t.from || t.to) && <span className="muted"> · {t.from ?? ""} → {t.to ?? ""}</span>}
            <span className="muted small"> · {t.actor}</span>
            {Object.keys(t.payload).length > 0 && <details><summary className="small muted">details</summary><pre>{JSON.stringify(t.payload, null, 2)}</pre></details>}
          </span>
        </li>))}
      </ol>
    </div>
  );
}

function Technical({ runs }: { runs: TriageRun[] }) {
  return (
    <div className="card">
      <p className="muted small">Every triage attempt, including failed LLM attempts and fallbacks. Raw outputs are untrusted model text.</p>
      <div className="table-wrap"><table>
        <thead><tr><th>at</th><th>rev</th><th>engine</th><th>model · fingerprint</th><th>valid</th><th>ms</th><th>errors</th></tr></thead>
        <tbody>{runs.map((r) => (
          <tr key={r.id}><td className="mono small">{time(r.at).slice(11)}</td><td>{r.revision}</td><td>{r.engine}</td>
            <td className="mono small">{r.model ?? "–"}<br />{r.model_digest ?? ""} {r.prompt_version ?? ""}</td>
            <td>{r.valid ? "yes" : <span className="bad-text">no</span>}</td><td>{r.duration_ms}</td>
            <td className="mono small">{r.errors.join(" | ").slice(0, 400)}
              {r.raw_output && <details><summary>raw output</summary><pre>{r.raw_output.slice(0, 4000)}</pre></details>}</td></tr>))}
        </tbody>
      </table></div>
    </div>
  );
}
