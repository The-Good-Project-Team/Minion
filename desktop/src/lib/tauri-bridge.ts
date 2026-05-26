/**
 * Wrapper over @tauri-apps/api so Vite-in-browser dev (no Rust shell) works:
 * - `VITE_BROWSER_DEV=true` — local sidecar + real vault (scripts/dev-browser.sh)
 * - `VITE_E2E=true` — Playwright stack (scripts/run-e2e-stack.sh)
 *
 * Set `VITE_E2E_API_PORT` / `MINION_API_PORT` to the sidecar port (default 8765 browser, 9876 e2e).
 */
import { invoke as tauriInvoke } from "@tauri-apps/api/core";
import { listen as tauriListen } from "@tauri-apps/api/event";

import { apiPortFromBase } from "./net_util";

const BROWSER_DEV = import.meta.env.VITE_BROWSER_DEV === "true";
const E2E = import.meta.env.VITE_E2E === "true";
const WITHOUT_TAURI = BROWSER_DEV || E2E;

export function isWithoutTauri(): boolean {
  return WITHOUT_TAURI;
}

function sidecarApiBase(): string {
  const port =
    (import.meta.env.VITE_E2E_API_PORT as string | undefined)?.trim() ||
    (import.meta.env.VITE_API_PORT as string | undefined)?.trim() ||
    (E2E ? "9876" : "8765");
  return `http://127.0.0.1:${port}`;
}

async function appConfigFromSidecar(): Promise<Record<string, unknown>> {
  const api_base = sidecarApiBase();
  const res = await fetch(`${api_base}/status`);
  if (!res.ok) {
    throw new Error(`Sidecar not reachable at ${api_base} (${res.status}). Start it or run npm run dev:browser.`);
  }
  const st = (await res.json()) as { data_dir?: string; inbox?: string };
  return {
    data_dir: st.data_dir ?? "",
    inbox: st.inbox ?? "",
    api_port: apiPortFromBase(api_base),
    api_base,
    api_token: "",
    sidecar_bootstrapped: true,
    sidecar_running: true,
  };
}

const LISTENING_OFF = { active: false, full_listening: false };

const SCREEN_BROWSER = {
  platform: "browser",
  watcher_supported: false,
  stream_path: "",
  last_event: null,
  macos_watch: null,
  ambient_collectors: {},
  listening_active: false,
  full_listening_active: false,
};

export async function invoke(cmd: string, args?: Record<string, unknown>): Promise<unknown> {
  if (!WITHOUT_TAURI) {
    return tauriInvoke(cmd, args);
  }

  switch (cmd) {
    case "app_config":
      return appConfigFromSidecar();
    case "restart_sidecar":
      return { pid: 0, api_port: apiPortFromBase(sidecarApiBase()) };
    case "vision_status":
      return { state: "off", model: "", installed: false, server_up: false };
    case "ensure_vision_model":
      return { state: "off", model: String(args?.model ?? "") };
    case "copy_into_inbox":
      return { drops: [], inbox: "" };
    case "reveal_in_finder":
      return undefined;
    case "screen_context_status":
      return SCREEN_BROWSER;
    case "snapshot_life_evidence": {
      const api_base = sidecarApiBase();
      const res = await fetch(`${api_base}/life-evidence/refresh?force=true`, { method: "POST" });
      if (!res.ok) {
        return { contacts: 0, events: 0, skipped: `sidecar ${res.status}` };
      }
      return (await res.json()) as Record<string, unknown>;
    }
    case "listening_start":
    case "listening_stop":
    case "listening_status":
    case "full_listening_start":
    case "full_listening_stop":
    case "full_listening_status":
    case "full_listening_sync":
      return LISTENING_OFF;
    case "keychain_search":
      return [];
    case "keychain_add":
      return { ok: false, error: "browser dev" };
    case "council_bridge_open":
      return undefined;
    default:
      throw new Error(
        `[browser dev] unhandled invoke("${cmd}") — use the desktop app or add a stub in tauri-bridge.ts`,
      );
  }
}

export async function listen<T>(
  event: string,
  handler: (event: { payload: T }) => void,
): Promise<() => void> {
  if (!WITHOUT_TAURI) {
    return tauriListen(event, handler);
  }

  if (event === "sidecar://status") {
    handler({ payload: { state: "ready", message: "Browser dev (sidecar on loopback)" } as T });
    return () => {};
  }
  if (
    event === "tauri://drag-enter" ||
    event === "tauri://drag-leave" ||
    event === "screen-context://update" ||
    event === "menu://status" ||
    event === "listening://wake"
  ) {
    return () => {};
  }
  return () => {};
}
