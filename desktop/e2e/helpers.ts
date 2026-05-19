import type { APIRequestContext } from "@playwright/test";

const API_PORT = process.env.E2E_API_PORT ?? "9876";
export const API_BASE = `http://127.0.0.1:${API_PORT}`;

/** Seed minimal second-brain data for UI assertions. */
export async function seedButlerFixtures(request: APIRequestContext): Promise<void> {
  await request.post(`${API_BASE}/wiki/pages`, {
    data: {
      page_type: "person",
      title: "E2E Test Person",
      body_md: "Fixture wiki page for Playwright.",
      status: "active",
    },
  });
  await request.post(`${API_BASE}/tasks/infer`, {
    data: {
      title: "Review E2E fixture task",
      body_md: "Agent-inferred work item for UI test.",
      origin: "agent",
    },
  });
}

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
