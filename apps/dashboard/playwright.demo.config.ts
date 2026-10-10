import { defineConfig } from "@playwright/test";

// Records the captioned demo tour (video in .pytest_tmp/demo-video). Run against the compose stack:
//   npx playwright test -c playwright.demo.config.ts
export default defineConfig({
  testDir: "./e2e-demo",
  timeout: 240_000,
  expect: { timeout: 30_000 },
  retries: 0,
  workers: 1,
  outputDir: "../../.pytest_tmp/playwright-demo",
  reporter: [["list"]],
  use: { launchOptions: { slowMo: 120 } },
});
