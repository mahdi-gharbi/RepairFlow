// Thin client for the RepairFlow API. The bearer token travels in a header (no cookie): no ambient
// credential, hence no CSRF surface. The server enforces every role check; the UI only hides buttons.

import type { ReplayRun } from "./Replay";

export type Role = "operator" | "reviewer";

export interface Me { name: string; role: Role }

export interface IncidentSummary {
  id: string; equipment_id: string; status: string; revision: number; correlation_id: string;
  created_at: string; updated_at: string; category: string | null; provenance: string | null;
}

export interface Evidence {
  id: string; kind: "message" | "telemetry" | "manual"; source_id: string; source_version: string;
  observed_at: string | null; retrieved_at: string; content: Record<string, any>;
}

export interface Fact { claim: string; evidence_refs: string[] }
export interface Contradiction { claim_a: string; claim_b: string; evidence_refs: string[] }

export interface Proposal {
  incident_revision: number; category: string; summary: string; extracted_facts: Fact[];
  missing_information: string[]; contradictions: Contradiction[];
  suggested_next_step: "clarify" | "review" | "prepare_ticket";
  draft_ticket: { title: string; description: string }; evidence_manifest_id: string;
}

export interface PlanStep { type: string; args: Record<string, any>; depends_on?: string }

export interface Action {
  id: string; type: string; status: string; action_key: string; args: Record<string, any>;
  provider_reference: string | null; last_error: Record<string, any> | null; attempts: number;
  offers: number; fencing_token: number; available_at: string | null; approval_id: string;
}

export interface IncidentDetail extends IncidentSummary {
  equipment: { id: string; model: string; display_name: string; site: string };
  revision_data: { description: string; language: string; reported_at: string; overrides: Record<string, string>;
                   notes: { by: string; at: string; note: string }[] };
  evidence: Evidence[];
  review: { revision: number; proposal: Proposal | null; provenance: string | null; plan: PlanStep[] | null;
            plan_digest: string | null; policy_version: string; adjustments?: string[] };
  actions: Action[];
}

export interface TimelineEntry {
  id: number; at: string; event: string; actor: string; action_id: string | null;
  from: string | null; to: string | null; payload: Record<string, any>;
}

export interface TriageRun {
  id: string; at: string; revision: number; engine: string; model: string | null; model_digest: string | null;
  prompt_version: string | null; valid: boolean; errors: string[]; duration_ms: number; raw_output: string | null;
  params: Record<string, any> | null;
}

export interface Metrics {
  incidents_by_status: Record<string, number>; eligible_backlog: number; oldest_eligible_age_s: number | null;
  uncertain_actions: number; dead_actions: number; review_time_median_s: number | null;
  dispatch_time_median_s: number | null; dispatch_time_p95_s: number | null;
}

export interface AuditEvent {
  id: number; at: string; incident_id: string | null; action_id: string | null; event: string;
  from: string | null; to: string | null;
}

export class ApiError extends Error {
  constructor(public status: number, public code: string, message: string, public body: any) { super(message); }
}

export class Api {
  constructor(private token: string) {}

  private async req<T>(method: string, path: string, body?: unknown): Promise<T> {
    const r = await fetch(path, {
      method,
      headers: { Authorization: `Bearer ${this.token}`, "Content-Type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
    const data = await r.json().catch(() => ({}));
    if (!r.ok) throw new ApiError(r.status, data.error ?? String(r.status), data.detail ?? data.error ?? r.statusText, data);
    return data as T;
  }

  me = () => this.req<Me>("GET", "/v1/me");
  metrics = () => this.req<Metrics>("GET", "/v1/metrics");
  incidents = (status: string, category: string) => {
    const q = new URLSearchParams({ limit: "200" });
    if (status) q.set("status", status);
    if (category) q.set("category", category);
    return this.req<IncidentSummary[]>("GET", `/v1/incidents?${q}`);
  };
  incident = (id: string) => this.req<IncidentDetail>("GET", `/v1/incidents/${id}`);
  timeline = (id: string) => this.req<TimelineEntry[]>("GET", `/v1/incidents/${id}/timeline`);
  triageRuns = (id: string) => this.req<TriageRun[]>("GET", `/v1/incidents/${id}/triage-runs`);
  triage = (id: string) => this.req("POST", `/v1/incidents/${id}/triage`);
  correct = (id: string, body: Record<string, unknown>) => this.req("POST", `/v1/incidents/${id}/corrections`, body);
  approve = (id: string, expected_revision: number, plan_digest: string) =>
    this.req("POST", `/v1/incidents/${id}/approve`, { expected_revision, plan_digest });
  reject = (id: string, expected_revision: number, reason: string) =>
    this.req("POST", `/v1/incidents/${id}/reject`, { expected_revision, reason });
  reconcile = (actionId: string) => this.req<{ status: string; finding: string }>("POST", `/v1/actions/${actionId}/reconcile`);
  resolve = (actionId: string, decision: string, note: string, provider_reference?: string) =>
    this.req("POST", `/v1/actions/${actionId}/resolve`, { decision, note, provider_reference: provider_reference || null });

  replayScenarios = () => this.req<{ scenarios: Record<string, string>; modes: string[] }>("GET", "/v1/replays/scenarios");
  replays = (incidentId: string) => this.req<ReplayRun[]>("GET", `/v1/replays?incident_id=${incidentId}`);
  runReplay = (incident_id: string, scenario: string, mode: string) =>
    this.req<ReplayRun>("POST", "/v1/replays", { incident_id, scenario, mode });

  /** Server-sent events read through fetch so the token stays in a header. Reconnects with Last-Event-ID. */
  stream(onEvent: (e: AuditEvent) => void, onState: (live: boolean) => void): () => void {
    let stopped = false;
    let lastId: string | null = null;
    let controller: AbortController | null = null;
    const loop = async () => {
      while (!stopped) {
        controller = new AbortController();
        try {
          const headers: Record<string, string> = { Authorization: `Bearer ${this.token}` };
          if (lastId) headers["Last-Event-ID"] = lastId;
          const r = await fetch("/v1/events", { headers, signal: controller.signal });
          if (!r.ok || !r.body) throw new Error(`HTTP ${r.status}`);
          onState(true);
          const reader = r.body.pipeThrough(new TextDecoderStream()).getReader();
          let buf = "";
          for (;;) {
            const { value, done } = await reader.read();
            if (done) break;
            buf += value;
            let idx;
            while ((idx = buf.indexOf("\n\n")) >= 0) {
              const block = buf.slice(0, idx);
              buf = buf.slice(idx + 2);
              let data = "";
              for (const line of block.split("\n")) {
                if (line.startsWith("id: ")) lastId = line.slice(4);
                if (line.startsWith("data: ")) data += line.slice(6);
              }
              if (data) onEvent(JSON.parse(data));
            }
          }
        } catch {
          /* network error or abort: fall through to retry */
        }
        onState(false);
        if (!stopped) await new Promise((r) => setTimeout(r, 2000));
      }
    };
    loop();
    return () => { stopped = true; controller?.abort(); };
  }
}
