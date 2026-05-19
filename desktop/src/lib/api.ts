// Tiny typed client for the Python sidecar. The base URL comes from Rust
// (app_config command) so we stay in sync with whatever port the sidecar
// actually bound to.

import { invoke, listen } from "./tauri-bridge";

export type SidecarStatus = {
  state: "starting" | "bootstrapping" | "installing" | "ready" | "error";
  message?: string;
};

/// Subscribe to `sidecar://status` events emitted by Rust during first-launch
/// bootstrap (locating sidecar, creating venv, pip-installing). Returns an
/// unsubscribe fn. Used to render a "Setting up Minion…" overlay.
export async function onSidecarStatus(fn: (s: SidecarStatus) => void): Promise<() => void> {
  const unlisten = await listen<SidecarStatus>("sidecar://status", (ev) => fn(ev.payload));
  return unlisten;
}

export type AppConfig = {
  data_dir: string;
  inbox: string;
  api_port: number;
  api_base: string;
  /** Empty when sidecar runs without MINION_API_TOKEN. */
  api_token: string;
  sidecar_bootstrapped: boolean;
  sidecar_running: boolean;
  /** From Rust env MINION_AUTO_INSTALL_UPDATES — fleet installs skip the update dialog. */
  auto_install_updates?: boolean;
};

export type Source = {
  source_id: string;
  path: string;
  kind: string;
  sha256: string;
  mtime: number;
  bytes: number;
  parser: string;
  updated_at: number;
  chunk_count?: number;
  meta?: Record<string, unknown>;
};

export type SearchHit = {
  score: number;
  chunk_id: string;
  role?: string | null;
  source_id: string;
  path: string;
  kind: string;
  mtime: number;
  text: string;
  meta?: Record<string, unknown>;
  /** Chunk storage tier (hot / warm / cold); retrieval ordering bias only today. */
  storage_tier?: string;
};

export type Active = {
  root: string | null;
  total: number;
  done: number;
  added: number;
  skipped: number;
};

export type DatabaseStatus = {
  ok: boolean;
  error: string | null;
  journal_mode: string | null;
};

export type Status = {
  /** Sidecar semver (GET /status); present on recent builds. */
  version?: string;
  data_dir: string;
  inbox: string;
  db_path: string;
  supported_extensions: string[];
  counts: { sources: number; chunks: number };
  active: Active;
  /** Present on newer sidecars; when ok is false, ingest/search are blocked. */
  database?: DatabaseStatus;
  watcher: { running: boolean; mode?: string };
};

export type EventMsg =
  | { type: "snapshot"; counts: { sources: number; chunks: number }; active?: Active }
  | { type: "ready"; counts: { sources: number; chunks: number }; active?: Active }
  | { type: "heartbeat"; counts: { sources: number; chunks: number }; active?: Active }
  | { type: "ingest_started"; path?: string; source?: string; count?: number; active?: Active }
  | { type: "ingest_progress"; path: string; index: number; total: number }
  | { type: "file_progress"; path: string; index: number; total: number; stage: string; [k: string]: any }
  | { type: "ingest_skipped"; result: Record<string, unknown>; active?: Active }
  | { type: "ingest_failed"; path: string; active?: Active }
  | { type: "source_updated"; result: Record<string, unknown>; counts: any; active?: Active }
  | { type: "source_removed"; key: string; counts: any }
  | { type: "tree_done"; root: string; added: number; skipped: number; counts: any }
  | { type: "db_error"; message: string };

/** Always ask the Rust shell — never cache. Stale `api_base` after a port
 * change or sidecar restart caused POST /nuke to hit the wrong listener (404). */
export async function getConfig(): Promise<AppConfig> {
  return (await invoke("app_config")) as AppConfig;
}

export type MacosWatchEnv = {
  watchers_env_disabled: boolean;
  /** True when MINION_SCREEN_CAPTURE opts in (still needs macOS Screen Recording). */
  pixel_capture_requested: boolean;
  /** False when MINION_AX_CAPTURE turns off Accessibility sampling. */
  ax_text_sample_enabled: boolean;
  poll_interval_sec: number;
};

export type AmbientCollectorsStatus = Record<string, boolean>;

export type ScreenContextStatus = {
  platform: string;
  /** macOS-only background watcher; false on Windows/Linux. */
  watcher_supported: boolean;
  stream_path: string;
  legacy_stream_path?: string;
  /** Latest JSON line from stream.jsonl, or null if missing/empty. */
  last_event: unknown;
  /** macOS env Effective for watcher; null on other platforms. */
  macos_watch: MacosWatchEnv | null;
  ambient_collectors?: AmbientCollectorsStatus;
  listening_active?: boolean;
  full_listening_active?: boolean;
};

/** Focused-window logger status + last snapshot path (macOS). */
export async function screenContextStatus(): Promise<ScreenContextStatus> {
  return (await invoke("screen_context_status")) as ScreenContextStatus;
}

/** macOS: snapshot Contacts + Calendar into life_evidence/ (needs Automation access). */
export async function snapshotLifeEvidence(): Promise<{
  contacts: number;
  events: number;
  skipped?: string;
}> {
  const cfg = await getConfig();
  return (await invoke("snapshot_life_evidence", { dataDir: cfg.data_dir })) as {
    contacts: number;
    events: number;
    skipped?: string;
  };
}

async function assertSidecarHasNukeRoute(apiBase: string): Promise<void> {
  const maxAttempts = 18;
  const delayMs = 300;
  let lastNet: Error | undefined;
  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    let res: Response;
    try {
      res = await fetch(`${apiBase}/openapi.json`, { headers: { accept: "application/json" } });
    } catch (e) {
      lastNet = e instanceof Error ? e : new Error(String(e));
      if (attempt < maxAttempts - 1) {
        await new Promise((r) => setTimeout(r, delayMs));
        continue;
      }
      throw new Error(
        `Cannot reach sidecar at ${apiBase}: ${lastNet.message}. Try Settings → Restart.`,
      );
    }
    if (!res.ok) {
      if (attempt < maxAttempts - 1 && (res.status === 502 || res.status === 503 || res.status === 404)) {
        await new Promise((r) => setTimeout(r, delayMs));
        continue;
      }
      throw new Error(`Sidecar at ${apiBase} returned ${res.status}. Try Settings → Restart or update Minion.`);
    }
    const text = await res.text();
    if (!text.includes('"/nuke"')) {
      throw new Error(
        `The server at ${apiBase} is not this Minion build (missing /nuke — often another user or app on the same port). Click Restart in Settings.`,
      );
    }
    return;
  }
}

function authHeaders(cfg: AppConfig, extra?: HeadersInit): Record<string, string> {
  const h: Record<string, string> = { "content-type": "application/json" };
  if (extra) {
    if (extra instanceof Headers) {
      extra.forEach((v, k) => {
        h[k] = v;
      });
    } else if (Array.isArray(extra)) {
      for (const [k, v] of extra) h[k] = v;
    } else {
      Object.assign(h, extra as Record<string, string>);
    }
  }
  if (cfg.api_token) {
    h["authorization"] = `Bearer ${cfg.api_token}`;
  }
  return h;
}

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const cfg = await getConfig();
  const res = await fetch(`${cfg.api_base}${path}`, {
    ...init,
    headers: authHeaders(cfg, init?.headers as HeadersInit | undefined),
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}: ${body}`);
  }
  return (await res.json()) as T;
}

export function isNotFoundError(e: unknown): boolean {
  const msg = (e as any)?.message ? String((e as any).message) : String(e);
  return msg.includes("404") || msg.includes("Not Found");
}

export async function fetchStatus(init?: RequestInit): Promise<Status> {
  const cfg = await getConfig();
  const res = await fetch(`${cfg.api_base}/status`, {
    ...init,
    headers: authHeaders(cfg, init?.headers as HeadersInit | undefined),
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}: ${body}`);
  }
  return (await res.json()) as Status;
}

/** Poll GET /status until it succeeds (e.g. after sidecar restart the port is bound before accept()). */
export async function waitForHealthySidecar(maxMs = 20_000, init?: RequestInit): Promise<Status> {
  const deadline = Date.now() + maxMs;
  let last: unknown;
  while (Date.now() < deadline) {
    try {
      return await fetchStatus(init);
    } catch (e) {
      last = e;
      await new Promise((r) => setTimeout(r, 350));
    }
  }
  throw last instanceof Error ? last : new Error(String(last));
}

export async function fetchSources(
  params: {
    kind?: string;
    path_glob?: string;
    since?: number;
    limit?: number;
  } = {},
  init?: RequestInit,
): Promise<{ sources: Source[]; counts: { sources: number; chunks: number } }> {
  const q = new URLSearchParams();
  if (params.kind) q.set("kind", params.kind);
  if (params.path_glob) q.set("path_glob", params.path_glob);
  if (params.since) q.set("since", String(params.since));
  if (params.limit) q.set("limit", String(params.limit));
  const qs = q.toString();
  const cfg = await getConfig();
  const res = await fetch(`${cfg.api_base}/sources${qs ? `?${qs}` : ""}`, {
    ...init,
    headers: authHeaders(cfg, init?.headers as HeadersInit | undefined),
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`${res.status} ${res.statusText}: ${body}`);
  }
  return (await res.json()) as { sources: Source[]; counts: { sources: number; chunks: number } };
}

export async function search(body: {
  query: string;
  top_k?: number;
  kind?: string;
  path_glob?: string;
}): Promise<{ results: SearchHit[] }> {
  return apiFetch("/search", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export type IdentityClaim = {
  claim_id: string;
  kind: string;
  text: string;
  status: string;
  confidence?: number | null;
  source_agent?: string | null;
  created_at: number;
  updated_at: number;
  superseded_by?: string | null;
  meta?: Record<string, unknown>;
};

export type IdentityEdge = {
  edge_id: string;
  claim_id: string;
  chunk_id: string | null;
  source_id: string | null;
  rationale: string | null;
  created_at: number;
};

export async function fetchIdentityClaims(params: {
  status?: string;
  kind?: string;
  limit?: number;
} = {}): Promise<{ claims: IdentityClaim[]; count: number }> {
  const q = new URLSearchParams();
  if (params.status) q.set("status", params.status);
  if (params.kind) q.set("kind", params.kind);
  if (params.limit != null) q.set("limit", String(params.limit));
  const qs = q.toString();
  return apiFetch(`/identity/claims${qs ? `?${qs}` : ""}`);
}

export async function patchIdentityClaim(
  claimId: string,
  body: {
    status?: string;
    superseded_by?: string;
    text?: string;
    meta?: Record<string, unknown>;
    revision_source?: string;
  },
): Promise<{ claim: IdentityClaim | null }> {
  return apiFetch(`/identity/claims/${encodeURIComponent(claimId)}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export async function fetchIdentityClaimEdges(
  claimId: string,
): Promise<{ edges: IdentityEdge[]; count: number }> {
  return apiFetch(`/identity/claims/${encodeURIComponent(claimId)}/edges`);
}

export async function fetchChunk(
  chunkId: string,
  max_chars?: number,
): Promise<{
  chunk_id: string;
  source_id: string;
  role: string | null;
  path: string;
  kind: string;
  mtime: number;
  text: string;
  meta: Record<string, unknown>;
}> {
  const q = max_chars != null ? `?max_chars=${max_chars}` : "";
  return apiFetch(`/chunks/${encodeURIComponent(chunkId)}${q}`);
}

export async function exportIdentityBundle(body: {
  out_path?: string;
  include_chunk_index?: boolean;
} = {}): Promise<{ path: string; manifest: Record<string, unknown> }> {
  return apiFetch("/identity/export", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function rebuildPreferenceClusters(body: {
  sample_limit?: number;
  k?: number;
  use_llm?: boolean;
} = {}): Promise<Record<string, unknown>> {
  return apiFetch("/identity/clusters/rebuild", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export type IdentityMirrorResponse = {
  markdown: string;
  history: IdentityClaim[];
  history_count: number;
};

/** Time-aware mirror digest + superseded/rejected claims (desktop Identity → Mirror). */
export async function fetchIdentityMirror(params: { limit_history?: number } = {}): Promise<IdentityMirrorResponse> {
  const q = new URLSearchParams();
  if (params.limit_history != null) q.set("limit_history", String(params.limit_history));
  const qs = q.toString();
  return apiFetch(`/identity/mirror${qs ? `?${qs}` : ""}`);
}

/** PRAGMA snapshot from POST /maintenance/storage-report (optional on very old sidecars). */
export type SqliteStorageFootprint = {
  page_count: number;
  freelist_pages: number;
  page_size: number;
  logical_bytes: number;
  freelist_bytes_approx: number;
  freelist_ratio: number;
  db_file_bytes: number | null;
  db_path: string | null;
};

export type StorageMaintenanceReport = {
  chunk_storage_tiers: Record<string, number>;
  ambient_event_count: number;
  note: string;
  sqlite?: SqliteStorageFootprint;
};

/** Chunk tier counts + ambient row count (compaction is metadata-first today). */
export async function postMaintenanceStorageReport(): Promise<StorageMaintenanceReport> {
  return apiFetch("/maintenance/storage-report", { method: "POST", body: "{}" });
}

export type StorageTierPromoteStaleResult = {
  dry_run: boolean;
  candidates: number;
  promoted: number;
  source_updated_before_unix: number;
  min_source_age_days: number;
  source_kinds?: string[] | null;
  from_tier: string;
  to_tier: string;
  chunk_storage_tiers?: Record<string, number>;
};

/** Preview or apply stale-source tier hops (default hot→warm; e.g. warm→cold). */
export async function postMaintenanceStorageTierPromoteStale(body: {
  min_source_age_days?: number;
  source_kinds?: string[] | null;
  dry_run?: boolean;
  from_tier?: string;
  to_tier?: string;
}): Promise<StorageTierPromoteStaleResult> {
  return apiFetch("/maintenance/storage-tier-promote-stale", {
    method: "POST",
    body: JSON.stringify({
      min_source_age_days: body.min_source_age_days ?? 120,
      source_kinds: body.source_kinds ?? null,
      dry_run: body.dry_run ?? true,
      from_tier: body.from_tier ?? "hot",
      to_tier: body.to_tier ?? "warm",
    }),
  });
}

/** Full inbox scan → DB. Use `force: true` to re-embed even when sha unchanged (slow). */
export async function reconcileInbox(body: { force?: boolean } = {}): Promise<{ started: boolean; force: boolean }> {
  return apiFetch("/reconcile", {
    method: "POST",
    body: JSON.stringify({ force: body.force ?? false }),
  });
}

/// Subscribe to GET /search/stream (SSE). Calls onHit for each result; onDone when finished.
export function openSearchStream(
  query: string,
  opts: { top_k?: number; kind?: string; path_glob?: string; role?: string; max_chars?: number } = {},
  handlers: {
    onMeta?: (n: number) => void;
    onHit: (hit: SearchHit) => void;
    onDone?: () => void;
    onError?: (msg: string) => void;
  },
): () => void {
  let cancelled = false;
  (async () => {
    const cfg = await getConfig();
    const q = new URLSearchParams({ query });
    if (opts.top_k != null) q.set("top_k", String(opts.top_k));
    if (opts.kind) q.set("kind", opts.kind);
    if (opts.path_glob) q.set("path_glob", opts.path_glob);
    if (opts.role) q.set("role", opts.role);
    if (opts.max_chars != null) q.set("max_chars", String(opts.max_chars));
    const url = `${cfg.api_base}/search/stream?${q}`;
    try {
      const res = await fetch(url, {
        headers: cfg.api_token ? { authorization: `Bearer ${cfg.api_token}` } : undefined,
      });
      if (!res.ok || !res.body) {
        handlers.onError?.(`${res.status} ${res.statusText}`);
        return;
      }
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      while (!cancelled) {
        const { value, done } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx: number;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const block = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const lines = block.split("\n");
          let ev = "";
          let data = "";
          for (const ln of lines) {
            if (ln.startsWith("event:")) ev = ln.slice(6).trim();
            if (ln.startsWith("data:")) data = ln.slice(5).trim();
          }
          if (ev === "meta") {
            try {
              const o = JSON.parse(data) as { count?: number };
              handlers.onMeta?.(o.count ?? 0);
            } catch {
              /* ignore */
            }
          } else if (ev === "hit") {
            try {
              handlers.onHit(JSON.parse(data) as SearchHit);
            } catch {
              /* ignore */
            }
          } else if (ev === "done") {
            handlers.onDone?.();
          } else if (ev === "error") {
            try {
              const o = JSON.parse(data) as { message?: string };
              handlers.onError?.(o.message ?? "stream error");
            } catch {
              handlers.onError?.("stream error");
            }
          }
        }
      }
    } catch (e) {
      if (!cancelled) handlers.onError?.((e as Error).message ?? String(e));
    }
  })();
  return () => {
    cancelled = true;
  };
}

export async function ingestPath(path: string, move = false): Promise<{ queued: string }> {
  return apiFetch("/ingest", {
    method: "POST",
    body: JSON.stringify({ path, move }),
  });
}

export async function connectClaudeDesktop(body: { server_name?: string; config_path?: string } = {}): Promise<{
  config_path: string;
  backup_path: string | null;
  server_name: string;
  restart_required: boolean;
}> {
  return apiFetch("/connect/claude-desktop", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function deleteSource(body: {
  path?: string;
  source_id?: string;
  kind?: string;
  confirm_bulk?: boolean;
}): Promise<{ removed_chunks: number; sources_removed?: number; kind?: string }> {
  return apiFetch("/sources", {
    method: "DELETE",
    body: JSON.stringify(body),
  });
}

export async function nukeDb(): Promise<{ removed: string[]; missing: string[]; db_path: string }> {
  const cfg = await getConfig();
  await assertSidecarHasNukeRoute(cfg.api_base);
  return apiFetch("/nuke", {
    method: "POST",
    body: JSON.stringify({ confirm: true }),
  });
}

export async function factoryReset(): Promise<{
  removed: string[];
  missing: string[];
  db_path: string;
  inbox: string;
  inbox_removed: string[];
  inbox_missing: string[];
}> {
  const cfg = await getConfig();
  await assertSidecarHasNukeRoute(cfg.api_base);
  return apiFetch("/factory-reset", {
    method: "POST",
    body: JSON.stringify({ confirm: true }),
  });
}

export type ConnState = "connecting" | "open" | "closed" | "unreachable";

/// Connect to the sidecar's `/events` WebSocket with bounded retries.
/// Backoff schedule: 1.5s, 3s, 6s, 12s, 20s (capped). After ~8 attempts
/// without a single successful open, flip to "unreachable" so the UI can
/// show an actionable error instead of silently reconnecting forever.
export async function openEvents(
  onMessage: (e: EventMsg) => void,
  onStatus?: (s: ConnState) => void,
): Promise<() => void> {
  let closed = false;
  let ws: WebSocket | null = null;
  let attempts = 0;
  let everOpened = false;
  const MAX_ATTEMPTS_BEFORE_UNREACHABLE = 8;
  const backoff = (n: number) => Math.min(1500 * Math.pow(1.6, n), 20000);

  const connect = async () => {
    if (closed) return;
    attempts += 1;
    onStatus?.("connecting");
    try {
      const cfg = await getConfig();
      ws = new WebSocket(`${cfg.api_base.replace("http", "ws")}/events`);
    } catch (err) {
      // Tauri invoke can reject early if backend is still spinning up.
      if (!everOpened && attempts >= MAX_ATTEMPTS_BEFORE_UNREACHABLE) {
        onStatus?.("unreachable");
      } else {
        onStatus?.("closed");
      }
      setTimeout(connect, backoff(attempts));
      return;
    }
    ws.onopen = () => {
      everOpened = true;
      attempts = 0;
      onStatus?.("open");
    };
    ws.onmessage = (ev) => {
      try {
        onMessage(JSON.parse(ev.data));
      } catch {
        // ignore malformed
      }
    };
    ws.onclose = () => {
      if (closed) return;
      if (!everOpened && attempts >= MAX_ATTEMPTS_BEFORE_UNREACHABLE) {
        onStatus?.("unreachable");
      } else {
        onStatus?.("closed");
      }
      setTimeout(connect, backoff(attempts));
    };
    ws.onerror = () => {
      if (!everOpened && attempts >= MAX_ATTEMPTS_BEFORE_UNREACHABLE) {
        onStatus?.("unreachable");
      } else {
        onStatus?.("closed");
      }
    };
  };
  await connect();

  return () => {
    closed = true;
    ws?.close();
  };
}

export async function restartSidecar(): Promise<{ pid: number; api_port: number }> {
  return (await invoke("restart_sidecar")) as { pid: number; api_port: number };
}

export type AmbientCollectorsSettings = {
  window_focus?: boolean;
  ax_content_changed?: boolean;
  process_snapshot?: boolean;
  app_launched?: boolean;
  browser_visit?: boolean;
  listening?: boolean;
  full_listening?: boolean;
  screenshot_fallback?: boolean;
  screen_reader?: boolean;
};

export type Settings = {
  disabled_kinds: string[];
  /** When true, do not POST anonymized telemetry to the configured collector. */
  telemetry_opt_out?: boolean;
  ambient_sensing_enabled?: boolean;
  /** Continuous mic + local transcripts; default off. */
  full_listening_enabled?: boolean;
  ambient_collectors?: AmbientCollectorsSettings;
  capture_on_empty_ax?: boolean;
  ambient_deny?: { app_names?: string[]; title_substrings?: string[] };
};

export type FullListeningStatus = {
  active: boolean;
  full_listening: boolean;
  session_id?: string;
  last_wake_ts?: number;
  last_wake_excerpt?: string;
};

export async function fetchAttentionSummary(hours = 24): Promise<Record<string, unknown>> {
  return apiFetch(`/attention/summary?hours=${hours}`);
}

export async function listeningStart(): Promise<{ session_id: string; active: boolean }> {
  const cfg = await getConfig();
  return (await invoke("listening_start", { dataDir: cfg.data_dir })) as {
    session_id: string;
    active: boolean;
  };
}

export async function listeningStop(): Promise<{ session_id: string; active: boolean }> {
  const cfg = await getConfig();
  return (await invoke("listening_stop", { dataDir: cfg.data_dir })) as {
    session_id: string;
    active: boolean;
  };
}

export async function listeningStatus(): Promise<FullListeningStatus> {
  const cfg = await getConfig();
  return (await invoke("listening_status", { dataDir: cfg.data_dir })) as FullListeningStatus;
}

export async function fullListeningStart(): Promise<FullListeningStatus> {
  const cfg = await getConfig();
  return (await invoke("full_listening_start", { dataDir: cfg.data_dir })) as FullListeningStatus;
}

export async function fullListeningStop(): Promise<FullListeningStatus> {
  const cfg = await getConfig();
  return (await invoke("full_listening_stop", { dataDir: cfg.data_dir })) as FullListeningStatus;
}

export async function fullListeningStatus(): Promise<FullListeningStatus> {
  const cfg = await getConfig();
  return (await invoke("full_listening_status", { dataDir: cfg.data_dir })) as FullListeningStatus;
}

export async function fullListeningSync(): Promise<FullListeningStatus> {
  const cfg = await getConfig();
  return (await invoke("full_listening_sync", { dataDir: cfg.data_dir })) as FullListeningStatus;
}

export type SettingsResponse = {
  settings: Settings;
  all_kinds: string[];
};

export type ExtensionsInfo = {
  manifest_path: string;
  user_extensions: { suffix: string; kind: string; module: string; function: string }[];
  supported_extensions: string[];
  parser_manifest_schema: { version: number; extensions: unknown[]; note?: string };
  ingest_webhook: Record<string, unknown>;
};

export async function fetchExtensions(): Promise<ExtensionsInfo> {
  return apiFetch<ExtensionsInfo>("/extensions");
}

export async function reloadParserExtensions(): Promise<{ reloaded: number; manifest_path: string }> {
  return apiFetch("/extensions/reload", { method: "POST" });
}

export async function fetchSettings(): Promise<SettingsResponse> {
  return apiFetch<SettingsResponse>("/settings");
}

export type CapabilitiesResponse = {
  service?: string;
  product?: string;
  version?: string;
  analytics?: {
    url_configured: boolean;
    telemetry_opt_out?: boolean;
    opt_out_setting?: string;
    note?: string;
  };
};

export async function fetchCapabilities(): Promise<CapabilitiesResponse> {
  return apiFetch<CapabilitiesResponse>("/capabilities");
}

export async function updateSettings(body: Partial<Settings>): Promise<SettingsResponse> {
  return apiFetch<SettingsResponse>("/settings", {
    method: "PUT",
    body: JSON.stringify(body),
  });
}

export type VisionState = "unavailable" | "off" | "pulling" | "ready";
export type VisionStatus = {
  state: VisionState;
  model: string;
  installed: boolean;
  server_up: boolean;
};

export async function visionStatus(): Promise<VisionStatus> {
  return (await invoke("vision_status")) as VisionStatus;
}

export async function ensureVisionModel(model?: string): Promise<{ state: VisionState; model: string }> {
  return (await invoke("ensure_vision_model", { model })) as { state: VisionState; model: string };
}

export type CopyDrop = {
  source: string;
  kind: "file" | "directory" | "missing" | "unsupported" | "duplicate";
  dest?: string;
  copied: number;
  bytes: number;
  skipped_dirs?: number;
  skipped_dotfiles?: number;
  errors?: string[];
  paths?: string[];
};

export type CopyResult = {
  drops: CopyDrop[];
  inbox: string;
};

export async function copyIntoInbox(paths: string[]): Promise<CopyResult> {
  return (await invoke("copy_into_inbox", { paths })) as CopyResult;
}

export async function revealInFinder(path: string): Promise<void> {
  await invoke("reveal_in_finder", { path });
}

/** Loopback Minion sidecar discovered via GET /capabilities. */
export type DiagnosticsInstance = {
  port: number;
  version?: string;
  product?: string;
  self?: boolean;
};

export type DiagnosticsPeersResponse = {
  instances: DiagnosticsInstance[];
  scan: { port_lo: number; port_hi: number };
};

export type DiagnosticsAbout = {
  name: string;
  tagline: string;
  license: string;
  homepage: string;
  privacy: string;
};

export type DiagnosticsLogBody = {
  log_file_hint: string | null;
  lines: string[];
  count: number;
};

/** Public diagnostics GETs are intentionally unauthenticated (loopback-only). */
async function diagFetchJson<T>(apiBase: string, path: string): Promise<T> {
  const base = apiBase.replace(/\/$/, "");
  const res = await fetch(`${base}${path}`, { headers: { accept: "application/json" } });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`${res.status}: ${body}`);
  }
  return (await res.json()) as T;
}

export async function fetchDiagnosticsAbout(apiBase?: string): Promise<DiagnosticsAbout> {
  const cfg = await getConfig();
  const base = apiBase ?? cfg.api_base;
  return diagFetchJson<DiagnosticsAbout>(base, "/diagnostics/about");
}

export async function fetchDiagnosticsPeers(apiBase?: string): Promise<DiagnosticsPeersResponse> {
  const cfg = await getConfig();
  const base = apiBase ?? cfg.api_base;
  return diagFetchJson<DiagnosticsPeersResponse>(base, "/diagnostics/peers");
}

export async function fetchDiagnosticsLogAtBase(apiBase: string, lines = 300): Promise<DiagnosticsLogBody> {
  return diagFetchJson<DiagnosticsLogBody>(apiBase, `/diagnostics/log?lines=${encodeURIComponent(String(lines))}`);
}

export async function fetchDiagnosticsLogTextAtBase(apiBase: string, lines = 400): Promise<string> {
  const base = apiBase.replace(/\/$/, "");
  const res = await fetch(`${base}/diagnostics/log/text?lines=${encodeURIComponent(String(lines))}`, {
    headers: { accept: "text/plain" },
  });
  if (!res.ok) {
    const body = await res.text().catch(() => "");
    throw new Error(`${res.status}: ${body}`);
  }
  return await res.text();
}

export function loopbackApiBaseForPort(port: number): string {
  return `http://127.0.0.1:${port}`;
}

// --- Second brain / butler API ---

export type TodayBundle = {
  working_context: Record<string, unknown>;
  attention_24h: { top_apps: string; recent: Array<Record<string, unknown>> };
  work_items: {
    open: WorkItem[];
    review: WorkItem[];
    inferred_pending: WorkItem[];
  };
  needs_attention: SystemIssue[];
  identity_excerpt_md: string;
};

export type WorkItem = {
  task_id: string;
  title: string;
  status: string;
  origin: string;
  priority?: string | null;
  body_md: string;
  context_refs: unknown[];
  wiki_refs: string[];
  output_id?: string | null;
  created_at: number;
  updated_at: number;
};

export type WikiPage = {
  page_id: string;
  page_type: string;
  title: string;
  body_md: string;
  status: string;
  last_updated: number;
  meta: Record<string, unknown>;
};

export type SystemIssue = {
  issue_id: string;
  severity: string;
  source_key?: string | null;
  body_md: string;
  status: string;
  created_at: number;
};

export type ConsentPolicy = {
  schema_version: number;
  readers: {
    mcp: {
      deny_chunk_source_kinds: string[];
      deny_path_substrings: string[];
      allow_screen_context_tools: boolean;
    };
  };
};

export async function fetchToday(): Promise<TodayBundle> {
  return apiFetch("/today");
}

export type GraphKindScaffold = {
  kind: string;
  label: string;
  hint: string;
  stub_node_id: string;
  filled_count: number;
};

export type GraphScaffoldNode = {
  node_id: string;
  node_kind: string;
  title: string;
  status: string;
  summary?: string;
  filled_count?: number;
  parent_node_id?: string | null;
  children?: GraphScaffoldNode[];
  depth?: number;
};

export type GraphScaffoldResponse = {
  root: GraphScaffoldNode | null;
  tree: GraphScaffoldNode[];
  kinds: GraphKindScaffold[];
  node_types: string[];
  relation_types: string[];
};

export type FeedParse = {
  status: string;
  reason?: string;
};

export type FeedAction = {
  id: string;
  label: string;
};

export type FeedItem = {
  item_kind?: "river";
  feed_id: string;
  ts: number;
  lane: "now" | "observed" | "parsed" | "suggestion" | string;
  kind: string;
  title: string;
  body: string;
  parse?: FeedParse | null;
  actions: FeedAction[];
  refs: Record<string, string>;
  graph_kinds: string[];
};

export type CouncilFeedItem = {
  item_kind: "council";
  ts: number;
  event: {
    event_type: string;
    subject_id: string;
    domain: string;
    evidence_refs: string[];
    pattern_id?: string;
  };
  proposal: {
    proposal_id: string;
    proposal_type: string;
    title: string;
    summary: string;
    payload: Record<string, unknown>;
    intensity: "standard" | "elevated" | string;
  };
  required_skill: string;
  required_info: Record<string, { status: string; ref?: string; label?: string }>;
  approval: { options: FeedAction[] };
};

export type FeedRow = FeedItem | CouncilFeedItem;

export type FortyTwoStreamState = {
  active_thread_id: string | null;
  needs_question: boolean;
  open_count: number;
};

export type ActivityFeedBundle = {
  now: FeedItem | null;
  items: FeedRow[];
  council_items?: CouncilFeedItem[];
  memory_prefetch: FeedItem[];
  graph: GraphScaffoldResponse;
  forty_two?: FortyTwoStreamState;
  composed_at: number;
  since_ts: number;
};

export type ChatThread = {
  thread_id: string;
  subject_id?: string | null;
  status: string;
  topic: string;
  meta: Record<string, unknown>;
  created_at: number;
  updated_at: number;
  messages?: ChatMessage[];
};

export type ChatMessage = {
  message_id: string;
  thread_id: string;
  role: "assistant" | "user" | string;
  body_md: string;
  meta: Record<string, unknown>;
  created_at: number;
};

export async function fetchChatThreads(status = "open"): Promise<{ threads: ChatThread[]; open_count: number }> {
  return apiFetch(`/chat/threads?status=${encodeURIComponent(status)}`);
}

export async function fetchChatBadge(): Promise<{ open_count: number }> {
  return apiFetch("/chat/badge");
}

export async function fetchChatThread(threadId: string): Promise<ChatThread> {
  return apiFetch(`/chat/threads/${encodeURIComponent(threadId)}`);
}

export async function chatNextThread(): Promise<{ thread: ChatThread | null; created: boolean; message?: string }> {
  return apiFetch("/chat/threads/next", { method: "POST", body: "{}" });
}

export async function fortyTwoNext(): Promise<{ thread: ChatThread | null; created: boolean }> {
  return apiFetch("/chat/42/next", { method: "POST", body: "{}" });
}

export async function fortyTwoReply(
  message: string,
  threadId?: string,
  action?: string,
): Promise<{ ok: boolean; thread?: ChatThread; llm?: boolean }> {
  return apiFetch("/chat/42/reply", {
    method: "POST",
    body: JSON.stringify({ message, thread_id: threadId ?? null, action: action ?? null }),
  });
}

export async function fortyTwoDismiss(threadId: string): Promise<{ ok: boolean; thread_id: string }> {
  return apiFetch("/chat/42/dismiss", {
    method: "POST",
    body: JSON.stringify({ thread_id: threadId, message: "" }),
  });
}

export async function chatReply(threadId: string, body: string, action?: string): Promise<{ ok: boolean; thread?: ChatThread }> {
  return apiFetch(`/chat/threads/${encodeURIComponent(threadId)}/reply`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ body, action }),
  });
}

export type KeychainItemMeta = {
  service: string;
  account: string;
  label: string;
  vault_ref: string;
};

export async function keychainSearch(query: string): Promise<KeychainItemMeta[]> {
  const cfg = await getConfig();
  return (await invoke("keychain_search", { dataDir: cfg.data_dir, query: query || null })) as KeychainItemMeta[];
}

export async function keychainAdd(
  service: string,
  account: string,
  secret: string,
  label?: string,
): Promise<KeychainItemMeta> {
  const cfg = await getConfig();
  return (await invoke("keychain_add", {
    dataDir: cfg.data_dir,
    service,
    account,
    secret,
    label: label ?? null,
  })) as KeychainItemMeta;
}

export async function fetchKeyCapabilities(): Promise<{ items: CapabilityRef[] }> {
  return apiFetch("/keys/capabilities");
}

export async function linkKeyCapability(body: {
  cap_key: string;
  vault_ref: string;
  label: string;
  provider?: string;
}): Promise<{ ok: boolean }> {
  return apiFetch("/keys/link", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export type CapabilityRef = {
  ref_id: string;
  cap_key: string;
  provider: string;
  label: string;
  vault_ref: string;
  status: string;
};

export async function councilApprove(body: {
  proposal_id: string;
  action: string;
  edited_payload?: Record<string, unknown>;
  snooze_days?: number;
}): Promise<{ ok: boolean; error?: string; execute?: unknown }> {
  return apiFetch("/council/approve", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
}

export async function fetchFeed(params: { limit?: number; since_hours?: number } = {}): Promise<ActivityFeedBundle> {
  const q = new URLSearchParams();
  if (params.limit != null) q.set("limit", String(params.limit));
  if (params.since_hours != null) q.set("since_hours", String(params.since_hours));
  const qs = q.toString();
  return apiFetch(`/feed${qs ? `?${qs}` : ""}`);
}

export async function fetchGraphScaffold(): Promise<GraphScaffoldResponse> {
  return apiFetch("/graph/scaffold");
}

export async function fetchHealth(): Promise<{ sync_sources: unknown[]; open_issues: SystemIssue[] }> {
  return apiFetch("/health");
}

export async function fetchWikiPages(params: {
  page_type?: string;
  q?: string;
  status?: string;
  limit?: number;
} = {}): Promise<{ pages: WikiPage[]; count: number }> {
  const q = new URLSearchParams();
  if (params.page_type) q.set("page_type", params.page_type);
  if (params.q) q.set("q", params.q);
  if (params.status) q.set("status", params.status);
  if (params.limit != null) q.set("limit", String(params.limit));
  const qs = q.toString();
  return apiFetch(`/wiki/pages${qs ? `?${qs}` : ""}`);
}

export async function fetchWikiPage(pageId: string): Promise<{ page: WikiPage; links: unknown[] }> {
  return apiFetch(`/wiki/pages/${encodeURIComponent(pageId)}`);
}

export async function patchWikiPage(
  pageId: string,
  body: Partial<Pick<WikiPage, "title" | "body_md" | "status">> & { page_type?: string },
): Promise<{ page: WikiPage }> {
  return apiFetch(`/wiki/pages/${encodeURIComponent(pageId)}`, {
    method: "PATCH",
    body: JSON.stringify({
      page_type: body.page_type ?? "topic",
      title: body.title ?? "",
      body_md: body.body_md ?? "",
      status: body.status ?? "active",
      meta: {},
    }),
  });
}

export async function fetchTasks(params: {
  status?: string;
  origin?: string;
  limit?: number;
} = {}): Promise<{ tasks: WorkItem[]; count: number }> {
  const q = new URLSearchParams();
  if (params.status) q.set("status", params.status);
  if (params.origin) q.set("origin", params.origin);
  if (params.limit != null) q.set("limit", String(params.limit));
  const qs = q.toString();
  return apiFetch(`/tasks${qs ? `?${qs}` : ""}`);
}

export async function patchTask(
  taskId: string,
  body: { status?: string; title?: string; body_md?: string },
): Promise<{ task: WorkItem }> {
  return apiFetch(`/tasks/${encodeURIComponent(taskId)}`, {
    method: "PATCH",
    body: JSON.stringify(body),
  });
}

export async function fetchConsentPolicy(): Promise<ConsentPolicy> {
  return apiFetch("/settings/consent");
}

export async function updateConsentPolicy(policy: ConsentPolicy): Promise<ConsentPolicy> {
  return apiFetch("/settings/consent", { method: "PUT", body: JSON.stringify(policy) });
}

export async function resolveHealthIssue(issueId: string): Promise<{ status: string }> {
  return apiFetch(`/health/issues/${encodeURIComponent(issueId)}/resolve`, {
    method: "POST",
    body: "{}",
  });
}
