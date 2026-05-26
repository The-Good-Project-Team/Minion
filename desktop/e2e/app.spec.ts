import { test, expect } from "@playwright/test";

const API_PORT = process.env.E2E_API_PORT ?? "9876";

test.describe("Minion desktop UI (browser + sidecar)", () => {
  test("shell defaults to Minion stream", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("heading", { name: "Minion" })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole("link", { name: "Sources" })).toBeVisible();
  });

  test("navigate sources and settings", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("link", { name: "Sources" }).click();
    await expect(page.getByRole("heading", { name: "Sources" })).toBeVisible();
    await page.getByRole("link", { name: "Settings" }).click();
    await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
  });

  test("sources search POST completes against sidecar", async ({ page }) => {
    await page.goto("/sources");
    const resp = page.waitForResponse(
      (r) => r.url().includes("/search") && r.request().method() === "POST" && r.ok(),
      { timeout: 30_000 },
    );
    await page.getByPlaceholder("Semantic search…").fill("cursor automated qa");
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await resp;
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
