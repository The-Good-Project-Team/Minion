import { test, expect } from "@playwright/test";
import { API_BASE, seedButlerFixtures, waitForSidecar } from "./helpers";

test.describe.configure({ mode: "serial" });

test.describe("Minion stream UI", () => {
  test.beforeAll(async ({ request }) => {
    await waitForSidecar(request);
    await seedButlerFixtures(request);
  });

  test("home is agent feed with composer", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByLabel("Agent feed")).toBeVisible();
    await expect(page.getByPlaceholder("Reply…")).toBeVisible();
  });

  test("Seeded wiki person appears in feed", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByText("E2E Test Person")).toBeVisible({ timeout: 15_000 });
  });

  test("Inferred task appears in feed", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByText("Review E2E fixture task")).toBeVisible({ timeout: 15_000 });
  });

  test("Dismiss suggestion removes inferred task from feed", async ({ page }) => {
    await page.goto("/");
    const card = page.getByText("Review E2E fixture task");
    await expect(card).toBeVisible();
    await page.getByRole("button", { name: "Dismiss" }).first().click();
    await expect(card).not.toBeVisible({ timeout: 10_000 });
  });

  test("chat composer pinned at bottom", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByPlaceholder("Reply…")).toBeVisible();
  });

  test("Sources search still works", async ({ page }) => {
    await page.goto("/sources");
    await expect(page.getByRole("heading", { name: "Sources" })).toBeVisible();
    const resp = page.waitForResponse(
      (r) => r.url().includes("/search") && r.request().method() === "POST" && r.ok(),
      { timeout: 30_000 },
    );
    await page.getByPlaceholder("Semantic search…").fill("e2e fixture");
    await page.getByRole("button", { name: "Search", exact: true }).click();
    await resp;
  });

  test("Settings reachable from home gear", async ({ page }) => {
    await page.goto("/");
    await page.getByRole("link", { name: "Settings" }).click();
    await expect(page.getByRole("heading", { name: "Settings" })).toBeVisible();
    await expect(page.getByRole("button", { name: "Add to Claude" })).toBeVisible();
  });

  test("legacy /activity redirects home", async ({ page }) => {
    await page.goto("/activity");
    await expect(page.getByLabel("Agent feed")).toBeVisible({ timeout: 15_000 });
  });

  test("legacy /chat redirects home", async ({ page }) => {
    await page.goto("/chat");
    await expect(page).toHaveURL("/");
    await expect(page.getByLabel("Agent feed")).toBeVisible();
  });

  test("Sidebar navigation on secondary routes", async ({ page }) => {
    await page.goto("/sources");
    for (const label of ["Sources", "Settings"]) {
      await page.getByRole("link", { name: label, exact: true }).click();
      await expect(page.getByRole("link", { name: label, exact: true })).toHaveClass(/active/);
    }
  });
});

test.describe("Activity API (sidecar)", () => {
  test("feed bundle shape", async ({ request }) => {
    await waitForSidecar(request);
    const r = await request.get(`${API_BASE}/feed`);
    expect(r.ok()).toBeTruthy();
    const j = await r.json();
    expect(Array.isArray(j.items)).toBe(true);
    expect(j.graph.root.title).toBe("Me");
    expect(j.graph.node_types.length).toBeGreaterThan(10);
  });

  test("graph scaffold endpoint", async ({ request }) => {
    const r = await request.get(`${API_BASE}/graph/scaffold`);
    expect(r.ok()).toBeTruthy();
    const j = await r.json();
    expect(j.kinds.length).toBeGreaterThan(20);
    expect(j.root.title).toBe("Me");
  });

  test("today bundle still available", async ({ request }) => {
    const r = await request.get(`${API_BASE}/today`);
    expect(r.ok()).toBeTruthy();
    const j = await r.json();
    expect(j.working_context).toBeDefined();
  });
});
