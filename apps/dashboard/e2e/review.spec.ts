// Essential reviewer journey through the real stack: n8n intake -> dashboard review -> approval ->
// worker + n8n dispatch -> completion pushed to the page by SSE. Plus the stale-revision guard and roles.
import { expect, test, type APIRequestContext, type Page } from "@playwright/test";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

const ENV: Record<string, string> = Object.fromEntries(
  readFileSync(resolve(process.cwd(), "../../.env"), "utf-8").split(/\r?\n/)
    .filter((l) => l.includes("=") && !l.startsWith("#"))
    .map((l) => [l.slice(0, l.indexOf("=")), l.slice(l.indexOf("=") + 1)]),
);
const REVIEWER = { Authorization: `Bearer ${ENV.RF_REVIEWER_TOKEN}` };

async function intake(request: APIRequestContext, equipment = "robot-07",
                      description = "Le robot s'arrête après trois minutes ; la batterie est pourtant pleine."): Promise<string> {
  const r = await request.post("http://127.0.0.1:15678/webhook/rf-intake", {
    headers: { "X-Intake-Key": ENV.RF_INTAKE_KEY },
    data: { event_id: `evt-e2e-${Date.now()}-${Math.random().toString(16).slice(2, 8)}`, equipment_id: equipment,
            reported_at: new Date().toISOString().slice(0, 19) + "Z", language: "fr", description },
  });
  expect(r.status()).toBe(201);
  return (await r.json()).incident_id;
}

async function signIn(page: Page, token: string, incidentId: string) {
  await page.goto(`/app/#${incidentId}`);
  await page.getByLabel("Access token").fill(token);
  await page.getByRole("button", { name: "Sign in" }).click();
  await expect(page.getByText("live", { exact: true })).toBeVisible();
}

test("reviewer approves a contradictory report and sees the dispatch complete live", async ({ page, request }) => {
  const id = await intake(request);
  await signIn(page, ENV.RF_REVIEWER_TOKEN, id);

  await expect(page.getByTestId("contradiction").first()).toBeVisible();
  // A citation jumps to the evidence row it refers to.
  const ref = page.getByTestId("contradiction").first().locator(".refs a").first();
  const evidenceId = await ref.textContent();
  await ref.click();
  await expect(page.locator(`tr#ev-${evidenceId}`)).toHaveClass(/hl/);

  await page.getByTestId("approve").click();
  await expect(page.getByTestId("flash")).toContainText("approved");
  // Completion arrives through SSE (worker -> n8n W3/W4 -> ack), without reloading the page.
  await expect(page.locator(".title-row .pill").first()).toHaveText("completed", { timeout: 60_000 });
  await page.getByRole("tab", { name: "actions" }).click();
  await expect(page.locator("td .pill", { hasText: "succeeded" })).toHaveCount(2);
  await page.getByRole("tab", { name: "timeline" }).click();
  await expect(page.getByTestId("timeline")).toContainText("incident_completed");
});

test("a revision created elsewhere disables approval of the stale page", async ({ page, request }) => {
  const id = await intake(request, "robot-05", "Tugger stops receiving missions although the wifi is fine on the tablet.");
  await signIn(page, ENV.RF_REVIEWER_TOKEN, id);
  await expect(page.getByTestId("approve")).toBeEnabled();

  // Another reviewer corrects the incident through the API while this page is open.
  const d = await (await request.get(`/v1/incidents/${id}`, { headers: REVIEWER })).json();
  const r = await request.post(`/v1/incidents/${id}/corrections`, {
    headers: REVIEWER, data: { expected_revision: d.revision, note: "second reviewer edit", category: "network" },
  });
  expect(r.status()).toBe(200);

  await expect(page.getByTestId("stale-banner")).toBeVisible();
  await expect(page.getByTestId("approve")).toHaveCount(0);
  await page.getByRole("button", { name: /Load revision/ }).click();
  await expect(page.getByTestId("stale-banner")).toHaveCount(0);
  await expect(page.getByTestId("approve")).toContainText(`Approve revision ${d.revision + 1}`);
});

test("an operator can read but not approve, and the server refuses it too", async ({ page, request }) => {
  const id = await intake(request, "robot-02", "Grincement fort de la roue avant droite.");
  await signIn(page, ENV.RF_OPERATOR_TOKEN, id);
  await expect(page.getByText("Read-only: approval needs the reviewer role.")).toBeVisible();
  await expect(page.getByTestId("approve")).toHaveCount(0);
  await expect(page.getByRole("tab", { name: "technical" })).toHaveCount(0);
  const r = await request.post(`/v1/incidents/${id}/approve`, {
    headers: { Authorization: `Bearer ${ENV.RF_OPERATOR_TOKEN}` }, data: { expected_revision: 1, plan_digest: "0".repeat(64) },
  });
  expect(r.status()).toBe(403);
});
