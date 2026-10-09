import { defineConfig } from "@playwright/test";

// Runs against the compose stack (API serving the built dashboard at /app). Start it first:
//   docker compose up -d --build && bash scripts/provision_n8n.sh
export default defineConfig({
  testDir: "./e2e",
  timeout: 90_000,
  expect: { timeout: 20_000 },
  retries: 0,
  workers: 1,
  outputDir: "../../.pytest_tmp/playwright",
  reporter: [["list"]],
  use: { baseURL: "http://127.0.0.1:18000", trace: "retain-on-failure", screenshot: "only-on-failure" },
});
