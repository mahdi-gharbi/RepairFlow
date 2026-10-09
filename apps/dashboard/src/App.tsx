import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Api, ApiError, AuditEvent, IncidentSummary, Me, Metrics } from "./api";
import { Detail } from "./Detail";
import { Pill, ago, secs } from "./ui";

const STATUSES = ["needs_review", "approved", "dispatching", "blocked", "completed", "rejected", "received", "enriching"];
const CATEGORIES = ["battery", "network", "mechanical", "sensor", "other"];

function readToken(): string {
  try { return sessionStorage.getItem("rf_token") ?? ""; } catch { return ""; }
}

export function App() {
  const [token, setToken] = useState(readToken);
  const [me, setMe] = useState<Me | null>(null);
  const [loginError, setLoginError] = useState("");
  const api = useMemo(() => new Api(token), [token]);

  useEffect(() => {
    if (!token) return;
    api.me().then(setMe).catch((e: ApiError) => {
      setMe(null);
      setLoginError(e.status === 401 ? "Token rejected by the server." : e.message);
    });
  }, [api, token]);

  if (!token || !me) {
    return <Login error={loginError} onSubmit={(t) => {
      try { sessionStorage.setItem("rf_token", t); } catch { /* private mode: keep in memory */ }
      setLoginError("");
      setToken(t);
    }} />;
  }
  return <Shell api={api} me={me} onLogout={() => {
    try { sessionStorage.removeItem("rf_token"); } catch { /* ignore */ }
    setMe(null); setToken("");
  }} />;
}

function Login({ onSubmit, error }: { onSubmit: (t: string) => void; error: string }) {
  const [value, setValue] = useState("");
  return (
    <main className="login">
      <form className="card" onSubmit={(e) => { e.preventDefault(); if (value.trim()) onSubmit(value.trim()); }}>
        <h1>RepairFlow</h1>
        <p className="muted">Maintenance review dashboard. Sign in with your local operator or reviewer token.</p>
        <label htmlFor="token">Access token</label>
        <input id="token" type="password" autoComplete="off" value={value} onChange={(e) => setValue(e.target.value)} />
        {error && <p className="error" role="alert">{error}</p>}
        <button className="btn" type="submit">Sign in</button>
      </form>
    </main>
  );
}

function Shell({ api, me, onLogout }: { api: Api; me: Me; onLogout: () => void }) {
  const [status, setStatus] = useState("needs_review");
  const [category, setCategory] = useState("");
  const [search, setSearch] = useState("");
  const [items, setItems] = useState<IncidentSummary[]>([]);
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [selected, setSelected] = useState<string | null>(() => location.hash.slice(1) || null);
  const [live, setLive] = useState(false);
  const [lastEvent, setLastEvent] = useState<AuditEvent | null>(null);
  const refreshTimer = useRef<number | null>(null);

  const refresh = useCallback(() => {
    api.incidents(status, category).then(setItems).catch(() => undefined);
    api.metrics().then(setMetrics).catch(() => undefined);
  }, [api, status, category]);

  useEffect(refresh, [refresh]);

  // Live updates: debounce list/metrics refresh on any audit event; the detail view reacts on its own.
  useEffect(() => api.stream((e) => {
    setLastEvent(e);
    if (refreshTimer.current) window.clearTimeout(refreshTimer.current);
    refreshTimer.current = window.setTimeout(refresh, 400);
  }, setLive), [api, refresh]);

  useEffect(() => { history.replaceState(null, "", selected ? `#${selected}` : "#"); }, [selected]);

  const shown = items.filter((i) => !search || i.equipment_id.includes(search.trim()) || i.id.startsWith(search.trim()));

  return (
    <div className="shell">
      <header className="topbar">
        <strong>RepairFlow</strong>
        <span className={`live ${live ? "on" : ""}`} title="Server-sent events">{live ? "live" : "reconnecting"}</span>
        {metrics && (
          <div className="kpis" aria-label="metrics">
            <span>review <b>{metrics.incidents_by_status.needs_review ?? 0}</b></span>
            <span>blocked <b className={metrics.incidents_by_status.blocked ? "warn-text" : ""}>{metrics.incidents_by_status.blocked ?? 0}</b></span>
            <span>uncertain <b className={metrics.uncertain_actions ? "warn-text" : ""}>{metrics.uncertain_actions}</b></span>
            <span>dead <b className={metrics.dead_actions ? "bad-text" : ""}>{metrics.dead_actions}</b></span>
            <span>backlog <b>{metrics.eligible_backlog}</b></span>
            <span title="human time from ready_for_review to approval">median review <b>{secs(metrics.review_time_median_s)}</b></span>
            <span title="machine time from approval to completion">median dispatch <b>{secs(metrics.dispatch_time_median_s)}</b></span>
          </div>
        )}
        <span className="spacer" />
        <span className="muted">{me.name} · {me.role}</span>
        <button className="btn ghost" onClick={onLogout}>Sign out</button>
      </header>
      <div className="layout">
        <aside className="card list">
          <div className="filters">
            <select aria-label="status filter" value={status} onChange={(e) => setStatus(e.target.value)}>
              <option value="">all statuses</option>
              {STATUSES.map((s) => <option key={s}>{s}</option>)}
            </select>
            <select aria-label="category filter" value={category} onChange={(e) => setCategory(e.target.value)}>
              <option value="">all categories</option>
              {CATEGORIES.map((c) => <option key={c}>{c}</option>)}
            </select>
            <input aria-label="search" placeholder="equipment or id" value={search} onChange={(e) => setSearch(e.target.value)} />
          </div>
          <ul>
            {shown.map((i) => (
              <li key={i.id}>
                <button className={`row ${selected === i.id ? "sel" : ""}`} onClick={() => setSelected(i.id)} data-testid="incident-row">
                  <span className="row-main"><b>{i.equipment_id}</b> <Pill value={i.status} /></span>
                  <span className="row-sub muted">
                    {i.category ?? "no proposal"}{i.provenance ? ` · ${i.provenance}` : ""} · rev {i.revision} · {ago(i.updated_at)}
                  </span>
                </button>
              </li>
            ))}
            {!shown.length && <li className="muted pad">No incident matches.</li>}
          </ul>
        </aside>
        <section className="detail">
          {selected ? <Detail key={selected} api={api} me={me} id={selected} lastEvent={lastEvent} />
                    : <div className="card muted pad">Select an incident.</div>}
        </section>
      </div>
    </div>
  );
}
