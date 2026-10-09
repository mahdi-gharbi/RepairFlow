const TONE: Record<string, string> = {
  needs_review: "warn", blocked: "warn", uncertain: "warn", retry_wait: "warn", offered: "info", executing: "info",
  dispatching: "info", approved: "info", completed: "ok", succeeded: "ok", dead: "bad", rejected: "bad",
  cancelled: "mute", pending: "mute", received: "mute", enriching: "mute",
};

export function Pill({ value }: { value: string }) {
  return <span className={`pill ${TONE[value] ?? "mute"}`}>{value}</span>;
}

export function ago(iso: string): string {
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 60) return `${Math.round(s)}s ago`;
  if (s < 3600) return `${Math.round(s / 60)}m ago`;
  if (s < 86400) return `${Math.round(s / 3600)}h ago`;
  return `${Math.round(s / 86400)}d ago`;
}

export function secs(v: number | null): string {
  if (v === null || v === undefined) return "–";
  return v < 120 ? `${v.toFixed(1)} s` : `${(v / 60).toFixed(1)} min`;
}

export function time(iso: string | null): string {
  return iso ? new Date(iso).toISOString().replace("T", " ").slice(0, 19) + "Z" : "–";
}
