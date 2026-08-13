import { test, expect } from "@playwright/test";
import {
  API_BASE,
  countOpenConnectorIntents,
  freshOnboardingPage,
  openConnectorViaPoll,
  saveDisplayName,
  seedGraphQuestionGap,
  skipOnboarding,
  waitForSidecar,
} from "./helpers";

test.describe.configure({ mode: "serial" });

test.describe("User journeys", () => {
  test.beforeAll(async ({ request }) => {
    await waitForSidecar(request);
  });

  test("graph gap yields an agent question and accepts an answer", async ({ page, request }) => {
    await skipOnboarding(page);
    await saveDisplayName(request, "Journey User");
    const threadId = await seedGraphQuestionGap(request, `E2E Person ${Date.now()}`);

    await expect
      .poll(async () => {
        const r = await request.get(`${API_BASE}/chat/threads/${encodeURIComponent(threadId)}`);
        if (!r.ok()) return "";
        const msgs = ((await r.json()).messages ?? []) as { role?: string; body_md?: string }[];
        const q = msgs.find((m) => m.role === "assistant");
        return String(q?.body_md ?? "").trim();
      })
      .not.toBe("");

    const replyRes = await request.post(`${API_BASE}/chat/agent/reply`, {
      data: { message: "Met through work on a shared project.", thread_id: threadId },
    });
    expect(replyRes.ok(), await replyRes.text()).toBeTruthy();

    await page.goto("/");
    await page.waitForResponse((r) => r.url().includes("/feed") && r.ok());
    await expect(page.getByText("Met through work on a shared project.").first()).toBeVisible({
      timeout: 30_000,
    });
  });

  test("first-run onboarding completes and reaches graph mode", async ({ page, request }) => {
    await freshOnboardingPage(page);
    await page.goto("/");

    await expect(page.getByText("What should I call you?")).toBeVisible({ timeout: 15_000 });
    await expect(page.getByPlaceholder("Type your name...")).toBeVisible({ timeout: 15_000 });
    await page.getByPlaceholder("Type your name...").fill("Journey User");
    await page.getByRole("button", { name: "Tell Minion" }).click();

    await expect(page.getByRole("button", { name: /Yes — sync Contacts|Open Accessibility/ })).toBeVisible({
      timeout: 15_000,
    });
    await page.getByRole("button", { name: "Not now" }).first().click();
    await page.getByRole("button", { name: "I enabled Accessibility" }).click();
    await page.getByRole("button", { name: "I enabled Screen Recording" }).click();

    await expect(page.getByRole("button", { name: "Yes — Gmail" })).toBeVisible({ timeout: 15_000 });
    await page.getByRole("button", { name: "Skip remaining" }).click();

    await expect(page.getByPlaceholder(/Type a source|Tell Minion a source/)).toBeVisible();
    await page.getByPlaceholder(/Type a source|Tell Minion a source/).fill("Slack exports folder");
    await page.getByRole("button", { name: "Send" }).click();

    await expect
      .poll(async () => page.evaluate(() => window.localStorage.getItem("minion:onboarding_done")))
      .toBe("true");

    const profile = await request.post(`${API_BASE}/onboarding/profile`, {
      data: { display_name: "Journey User" },
    });
    expect(profile.ok()).toBeTruthy();
  });

  test("resource poll yes creates durable connector work", async ({ request }) => {
    const { candidate_id, task_id } = await openConnectorViaPoll(request, "slack");
    expect(candidate_id).toBeTruthy();
    expect(task_id).toBeTruthy();
    const open = await countOpenConnectorIntents(request);
    expect(open).toBeGreaterThan(0);
  });

  test("adds pasted text to Minion context from the main screen", async ({ page }) => {
    await skipOnboarding(page);
    await page.goto("/");

    await expect(page.getByText("Add context")).toBeVisible({ timeout: 15_000 });
    await page.getByPlaceholder("Title").fill("Client note");
    await page.getByPlaceholder("Paste text you want Minion to remember...").fill("Foofie context should be available to the server.");
    await page.getByRole("button", { name: "Save text" }).click();

    await expect(page.getByText("Saved. Minion is indexing it now.")).toBeVisible({ timeout: 15_000 });
  });

  test("Settings loads consent policy and profile context after startup", async ({ page, request }) => {
    await skipOnboarding(page, "Settings User");
    await saveDisplayName(request, "Settings User");
    await page.goto("/");

    await expect(page.getByRole("button", { name: "Settings" })).toBeVisible({ timeout: 15_000 });
    await page.getByRole("button", { name: "Settings" }).click();
    await expect(page.getByRole("heading", { name: "Consent Policy" })).toBeVisible({
      timeout: 15_000,
    });
    await expect(page.getByText("Real-time Preview")).toBeVisible();
    await expect(page.getByText(/Profile:/)).toBeVisible();
    await expect(page.getByText("MCP assistants")).toBeVisible();
  });

  test("Activity home shows today briefing and memory search", async ({ page }) => {
    await skipOnboarding(page);
    await page.goto("/");

    await expect(page.getByRole("button", { name: "Activity" })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole("heading", { name: "Today" })).toBeVisible({ timeout: 15_000 });
    await expect(page.getByRole("heading", { name: "Search memory" })).toBeVisible();
    await expect(page.getByRole("heading", { name: "Activity" })).toBeVisible();
  });

  test("Work tab loads suggested task queue", async ({ page }) => {
    await skipOnboarding(page);
    await page.goto("/");

    await page.getByRole("button", { name: "Work" }).click();
    await expect(page.getByRole("heading", { name: "Work queue" })).toBeVisible({ timeout: 15_000 });
  });

  test("Graph tab shows candidate inbox section", async ({ page }) => {
    await skipOnboarding(page);
    await page.goto("/");

    await page.locator("header").getByRole("button", { name: "Graph" }).click();
    await expect(page.getByText("Graph questions")).toBeVisible({ timeout: 15_000 });
  });

});

test.describe("Return visit", () => {
  test.beforeAll(async ({ request }) => {
    await waitForSidecar(request);
  });

  test("session open shows briefing and a request", async ({ page, request }) => {
    await skipOnboarding(page);
    await saveDisplayName(request, "Return User");
    const open = await request.post(`${API_BASE}/session/open`, { data: { display_name: "Return User" } });
    expect(open.ok()).toBeTruthy();

    await page.goto("/");
    await page.waitForResponse((r) => r.url().includes("/feed") && r.ok());
    await expect(page.getByText(/Welcome back|Since you were here|Nothing major changed/i).first()).toBeVisible({
      timeout: 20_000,
    });
    await expect(page.getByPlaceholder(/Answer Minion|Reply when Minion/)).toBeVisible({ timeout: 15_000 });
  });
});

