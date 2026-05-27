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
    await expect(page.getByRole("heading", { name: "Ask your life." })).toBeVisible();
    await expect(page.getByPlaceholder(/Ask about a person/)).toBeVisible();
  });

  test("Seeded wiki person appears in feed", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByText("E2E Test Person")).toBeVisible({ timeout: 15_000 });
  });

  test("chat composer pinned at bottom", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByPlaceholder(/Ask about a person/)).toBeVisible();
  });

  test("Top navigation links are present", async ({ page }) => {
    await page.goto("/");
    await expect(page.getByRole("link", { name: "Sources" })).toBeVisible();
    await expect(page.getByRole("link", { name: "Settings" })).toBeVisible();
  });

  test("legacy /activity serves agent shell", async ({ page }) => {
    await page.goto("/activity");
    await expect(page.getByRole("heading", { name: "Ask your life." })).toBeVisible({ timeout: 15_000 });
  });

  test("legacy /chat serves agent shell", async ({ page }) => {
    await page.goto("/chat");
    await expect(page.getByRole("heading", { name: "Ask your life." })).toBeVisible();
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
