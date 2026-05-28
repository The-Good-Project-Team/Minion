import { defineConfig, devices } from "@playwright/test";

/** Must match default in scripts/run-e2e-stack.sh */
const E2E_API_PORT = process.env.E2E_API_PORT ?? "9876";
process.env.E2E_API_PORT = E2E_API_PORT;

/**
 * Browser UI QA: Vite + real Python sidecar (see scripts/run-e2e-stack.sh).
 * Exercises React + HTTP; not a substitute for full native Tauri integration tests.
 */
export default defineConfig({
  testDir: "e2e",
  /** Shared temp MINION_DATA_DIR on one sidecar — avoid cross-test DB races. */
  fullyParallel: false,
  workers: 1,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  reporter: process.env.CI ? "github" : "list",
  timeout: 90_000,
  expect: { timeout: 15_000 },
  use: {
    ...devices["Desktop Chrome"],
    baseURL: "http://127.0.0.1:1420",
    trace: "on-first-retry",
  },
  webServer: {
    command: `bash scripts/run-e2e-stack.sh`,
    env: { ...process.env, E2E_API_PORT },
    url: "http://127.0.0.1:1420",
    reuseExistingServer: false,
    timeout: 180_000,
    stdout: "pipe",
    stderr: "pipe",
  },
});
