import { test, expect } from "@playwright/test";

const API_PORT = process.env.E2E_API_PORT ?? "9876";

test.describe("Minion desktop UI (browser + sidecar)", () => {
  test("shell defaults to live Minion agent", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Ask your life." })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole("link", { name: "Sources" })).toBeVisible();
    await expect(page.getByPlaceholder(/Ask about a person/)).toBeVisible();
  });

  test("sidecar capabilities JSON", async ({ request }) => {
    const r = await request.get(`http://127.0.0.1:${API_PORT}/capabilities`);
    expect(r.ok()).toBeTruthy();
    const j = await r.json();
    expect(j.service).toBe("minion-api");
  });

  test("feed endpoint", async ({ request }) => {
    const r = await request.get(`http://127.0.0.1:${API_PORT}/feed`);
    expect(r.ok()).toBeTruthy();
    const j = await r.json();
    expect(j.items).toBeDefined();
    expect(j.graph).toBeDefined();
  });
});
