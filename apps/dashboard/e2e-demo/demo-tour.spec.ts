// Scripted, captioned demo tour recorded as a video (playwright.demo.config.ts). Everything shown is the real
// local stack: n8n form and webhooks, API, worker, ticket-service fault injection, Mailpit, Replay Lab.
// Captions are injected into the page for the viewer; the values they quote come from real responses.
import { expect, test, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const ENV: Record<string, string> = Object.fromEntries(
  readFileSync(resolve(process.cwd(), "../../.env"), "utf-8").split(/\r?\n/)
    .filter((l) => l.includes("=") && !l.startsWith("#"))
    .map((l) => [l.slice(0, l.indexOf("=")), l.slice(l.indexOf("=") + 1)]),
);
const API = "http://127.0.0.1:18000";
const REVIEWER = { Authorization: `Bearer ${ENV.RF_REVIEWER_TOKEN}` };
const PACE = 1.45; // caption reading time multiplier (target video length 90–120 s)
const DESCRIPTION = "Le robot s'arrête après trois minutes ; la batterie est pourtant pleine.";

async function caption(page: Page, text: string, ms = 2600) {
  await page.evaluate((t) => {
    let el = document.getElementById("rf-caption");
    if (!el) {
      el = document.createElement("div");
      el.id = "rf-caption";
      Object.assign(el.style, {
        position: "fixed", left: "50%", bottom: "22px", transform: "translateX(-50%)", zIndex: "2147483647",
        background: "rgba(16,19,23,.92)", color: "#fff", padding: "10px 18px", borderRadius: "8px",
        font: "600 17px/1.35 system-ui, sans-serif", maxWidth: "88%", textAlign: "center", boxShadow: "0 4px 18px rgba(0,0,0,.35)",
      });
      document.body.appendChild(el);
    }
    el.textContent = t;
  }, text);
  await page.waitForTimeout(ms * PACE);
}

test("RepairFlow demo tour", async ({ browser, request }) => {
  test.setTimeout(240_000);
  const context = await browser.newContext({
    viewport: { width: 1280, height: 720 },
    httpCredentials: { username: ENV.RF_FORM_USER, password: ENV.RF_FORM_PASSWORD },
    recordVideo: { dir: "../../.pytest_tmp/demo-video", size: { width: 1280, height: 720 } },
  });
  const page = await context.newPage();

  // 1. Operator form (n8n Form Trigger) -> W1 intake.
  await page.goto("http://127.0.0.1:15678/form/rf-report");
  await caption(page, "1 · An operator reports a problem through an n8n form (no AI decides anything yet)");
  await page.selectOption("#field-0", { label: "robot-07 (Picker Golf, Warehouse A)" });
  await page.selectOption("#field-1", "fr");
  await page.fill("#field-2", DESCRIPTION);
  await page.fill("#field-3", "operator-demo-1");
  await page.waitForTimeout(600);
  await page.locator("button[type=submit]").click();
  await caption(page, "n8n validates through the API, stores the report, fetches telemetry and manuals, then triages", 2400);

  // Find the incident created by this submission.
  let incident: any = null;
  for (let i = 0; i < 40 && !incident; i++) {
    const list = await (await request.get(`${API}/v1/incidents?limit=5`, { headers: REVIEWER })).json();
    for (const it of list) {
      const d = await (await request.get(`${API}/v1/incidents/${it.id}`, { headers: REVIEWER })).json();
      if (d.revision_data.description === DESCRIPTION && d.evidence[0].source_id.includes(":form-") && d.status === "needs_review"
          && Date.now() - Date.parse(d.created_at) < 120_000) { incident = d; break; }
    }
    if (!incident) await page.waitForTimeout(1000);
  }
  expect(incident).not.toBeNull();
  const id = incident.id;

  // 2. Redelivery of the very same event -> idempotent replay.
  const eventId = incident.evidence[0].source_id.split(":")[1];
  const same = { event_id: eventId, equipment_id: "robot-07", reported_at: incident.revision_data.reported_at.replace("+00:00", "Z"),
                 language: "fr", description: DESCRIPTION, operator_reference: "operator-demo-1" };
  const again = await request.post("http://127.0.0.1:15678/webhook/rf-intake", { headers: { "X-Intake-Key": ENV.RF_INTAKE_KEY }, data: same });
  const againBody = await again.json();
  await caption(page, `2 · The same event delivered again → HTTP ${again.status()}, replayed=${againBody.replayed}, same incident: ${againBody.incident_id === id}`, 3200);

  // 3. Reviewer dashboard: contradiction with cited evidence.
  await page.goto(`${API}/app/#${id}`);
  await page.getByLabel("Access token").fill(ENV.RF_REVIEWER_TOKEN);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByTestId("contradiction").first()).toBeVisible({ timeout: 30_000 });
  await caption(page, "3 · The proposal flags a contradiction instead of inventing a cause: operator says full, telemetry says 12 %", 3200);
  const ref = page.getByTestId("contradiction").first().locator(".refs a").nth(1);
  await ref.click();
  await caption(page, "Every claim cites a dated evidence snapshot (message, telemetry, manual section)", 2800);
  await page.evaluate(() => window.scrollTo({ top: 0, behavior: "smooth" }));

  // 4. Another reviewer edits the incident -> this page is stale, approval disabled.
  await page.getByText("Plan to approve").scrollIntoViewIfNeeded();
  await caption(page, "4 · The reviewer approves an exact plan: revision + digest of actions, evidence versions and policy", 2800);
  const r = await request.post(`${API}/v1/incidents/${id}/corrections`, { headers: REVIEWER,
    data: { expected_revision: incident.revision, note: "Charge LED unreliable on this unit; trust telemetry.",
            ticket_title: "[robot-07] battery: shutdown after 3 min, telemetry 12 % vs reported full" } });
  expect(r.status()).toBe(200);
  await expect(page.getByTestId("stale-banner")).toBeVisible({ timeout: 20_000 });
  await page.getByTestId("stale-banner").scrollIntoViewIfNeeded();
  await caption(page, "Meanwhile a colleague corrected the ticket title: this page is stale, approving it is impossible", 3200);
  await page.getByRole("button", { name: /Load revision/ }).click();

  // 5. Outage: the ticket service refuses twice; approve; watch retries and recovery.
  await request.post("http://127.0.0.1:18081/_faults", { headers: { "X-Faults-Key": ENV.SIM_FAULTS_KEY },
                                                          data: { mode: "fail_before", status: 503, count: 2 } });
  await page.getByTestId("approve").scrollIntoViewIfNeeded();
  await caption(page, "5 · We break the ticket service (503 twice), then approve the current revision", 2600);
  await page.getByTestId("approve").click();
  await page.getByRole("tab", { name: /actions/ }).click();
  await caption(page, "The worker leases the command; n8n begins, calls the ticket API, acknowledges — retries come from the API policy", 3000);
  await expect(page.locator(".title-row .pill").first()).toHaveText("completed", { timeout: 60_000 });
  await caption(page, "Two 503s, backoff, then one ticket — then the team email. Each action is idempotent or explicitly 'uncertain'", 3200);
  await page.getByRole("tab", { name: "timeline" }).click();
  await caption(page, "The audit timeline records every transition, attempt and actor", 3000);

  // 6. The email in Mailpit (local SMTP sink).
  await page.goto("http://127.0.0.1:18025/");
  await caption(page, "6 · The notification reached the local mail sink — nothing leaves the machine", 3000);

  // 7. Replay Lab: what if the SMTP response had been lost?
  await page.goto(`${API}/app/#${id}`);
  await page.getByRole("tab", { name: "replay" }).click();
  await page.selectOption("select[aria-label=scenario]", "email_accepted_response_lost");
  await caption(page, "7 · Replay Lab: re-run this incident in a sandbox — what if the SMTP response had been lost?", 2600);
  await page.getByTestId("run-replay").click();
  await expect(page.getByTestId("replay-result")).toBeVisible();
  await page.getByTestId("replay-result").scrollIntoViewIfNeeded();
  await caption(page, "Ticket created once; the email becomes 'uncertain' and waits for a human — never blindly re-sent", 3600);
  await caption(page, "RepairFlow · n8n + FastAPI + PostgreSQL · versioned approval, outbox, reconciliation", 3000);

  await context.close();
});
