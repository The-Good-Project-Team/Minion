import type { APIRequestContext, Page } from "@playwright/test";

const API_PORT = process.env.E2E_API_PORT ?? "9876";
export const API_BASE = `http://127.0.0.1:${API_PORT}`;

export async function waitForSidecar(request: APIRequestContext, timeoutMs = 60_000): Promise<void> {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    try {
      const r = await request.get(`${API_BASE}/status`);
      if (r.ok()) return;
    } catch {
      /* sidecar still starting */
    }
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error(`sidecar not ready at ${API_BASE}/status`);
}

/** Fresh browser profile for onboarding journeys. */
export async function freshOnboardingPage(page: Page): Promise<void> {
  await page.addInitScript(() => {
    localStorage.removeItem("minion:onboarding_done");
    localStorage.removeItem("minion:onboarding_name");
  });
}

/** Skip onboarding UI; graph-fill mode only. */
export async function skipOnboarding(page: Page, displayName = "E2E User"): Promise<void> {
  await page.addInitScript(
    ({ name }) => {
      localStorage.setItem("minion:onboarding_done", "true");
      localStorage.setItem("minion:onboarding_name", name);
      localStorage.setItem("minion:name", name);
    },
    displayName,
  );
}

export async function saveDisplayName(request: APIRequestContext, displayName: string): Promise<void> {
  const r = await request.post(`${API_BASE}/onboarding/profile`, { data: { display_name: displayName } });
  if (!r.ok()) throw new Error(`save profile failed: ${r.status()} ${await r.text()}`);
}

export async function seedGraphQuestionGap(
  request: APIRequestContext,
  name = `E2E Journey Person ${Date.now()}`,
): Promise<string> {
  const r = await request.post(`${API_BASE}/dev/e2e/seed-graph-gap`, { data: { name } });
  if (!r.ok()) throw new Error(`seed graph gap failed: ${r.status()} ${await r.text()}`);
  const j = await r.json();
  const tid = j.thread_id as string | undefined;
  if (!tid) throw new Error(`expected agent thread after seeding gap: ${JSON.stringify(j)}`);
  return tid;
}

export async function openConnectorViaPoll(request: APIRequestContext, resourceId = "gmail"): Promise<{
  candidate_id: string;
  task_id: string;
}> {
  const r = await request.post(`${API_BASE}/onboarding/resource-poll`, {
    data: { resource_id: resourceId, uses: true, note: "E2E journey" },
  });
  if (!r.ok()) throw new Error(`resource poll failed: ${r.status()}`);
  const j = await r.json();
  if (!j.candidate_id || !j.task_id) throw new Error("expected connector candidate and task");
  return { candidate_id: j.candidate_id, task_id: j.task_id };
}

export async function countOpenConnectorIntents(request: APIRequestContext): Promise<number> {
  const r = await request.get(`${API_BASE}/graph/candidates?status=open&limit=50`);
  if (!r.ok()) return 0;
  const j = await r.json();
  const items = (j.candidates ?? []) as { candidate_type?: string }[];
  return items.filter((c) => c.candidate_type === "connector_intent").length;
}
