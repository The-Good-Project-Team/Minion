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
  active_profile_id?: string;
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
  | { type: "db_error"; message: string }
  | { type: "chat_updated"; thread_id?: string | null; open_count: number };

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

/** macOS: snapshot Contacts into life_evidence/ (does not request Calendar). */
export async function snapshotLifeEvidence(): Promise<{
  contacts: number;
  events: number;
  indexed_contacts?: number;
  indexed_calendar?: number;
  skipped?: string;
}> {
  const cfg = await getConfig();
  const snap = (await invoke("snapshot_contacts_evidence", { dataDir: cfg.data_dir })) as {
    contacts: number;
    events: number;
    skipped?: string;
  };
  try {
    const indexed = await apiFetch<{ indexed_contacts: number; indexed_calendar: number }>(
      "/life-evidence/ingest",
      { method: "POST" }
    );
    return { ...snap, ...indexed };
  } catch {
    return snap;
  }
}

/** macOS: open a Privacy & Security permission pane for guided onboarding. */
export async function openMacosPrivacySettings(
  pane: "contacts" | "calendar" | "accessibility" | "screen-recording",
): Promise<void> {
  await invoke("open_macos_privacy_settings", { pane });
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
    source_type?: "file" | "chat_export" | "external" | "ambient";
    time_range?: "last_hour" | "last_day" | "last_week" | "all";
  } = {},
  init?: RequestInit,
): Promise<{ sources: Source[]; counts: { sources: number; chunks: number } }> {
  const q = new URLSearchParams();
  if (params.kind) q.set("kind", params.kind);
  if (params.path_glob) q.set("path_glob", params.path_glob);
  if (params.since) q.set("since", String(params.since));
  if (params.limit) q.set("limit", String(params.limit));
  if (params.source_type) q.set("source_type", params.source_type);
  if (params.time_range) q.set("time_range", params.time_range);
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
  superseded_at?: number | null;
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
export async function fetchIdentityMirror(params: { limit_history?: number; include_evidence?: boolean } = {}): Promise<IdentityMirrorResponse> {
  const q = new URLSearchParams();
  if (params.limit_history != null) q.set("limit_history", String(params.limit_history));
  if (params.include_evidence != null) q.set("include_evidence", String(params.include_evidence));
  const qs = q.toString();
  return apiFetch(`/identity/mirror${qs ? `?${qs}` : ""}`);
}

export type IdentityHistoryResponse = {
  history: IdentityClaim[];
  count: number;
};

/** Revision history for identity claims with supersession tracking. */
export async function fetchIdentityHistory(params: {
  claim_id?: string;
  status?: string;
  limit?: number;
} = {}): Promise<IdentityHistoryResponse> {
  const q = new URLSearchParams();
  if (params.claim_id) q.set("claim_id", params.claim_id);
  if (params.status) q.set("status", params.status);
  if (params.limit != null) q.set("limit", String(params.limit));
  const qs = q.toString();
  return apiFetch(`/identity/history${qs ? `?${qs}` : ""}`);
}

export type IdentityRevertResponse = {
  ok: boolean;
  reverted_claim: IdentityClaim;
  superseded_claim: IdentityClaim;
};

/** Revert to a previous identity claim version. */
export async function revertIdentityClaim(body: { claim_id: string }): Promise<IdentityRevertResponse> {
  return apiFetch("/identity/revert", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export type IdentityCompanionPillar = {
  label: string;
  count: number;
  prompt: string;
  status: string;
};

export type IdentityCompanionResponse = {
  tagline: string;
  readiness: number;
  source_count: number;
  graph_count: number;
  active_claim_count: number;
  proposed_claim_count: number;
  pillars: IdentityCompanionPillar[];
  next_steps: string[];
  starter_prompts: string[];
};

export async function fetchIdentityCompanion(): Promise<IdentityCompanionResponse> {
  return apiFetch("/identity/companion");
}

export async function startIdentityCompanion(): Promise<{ thread: ChatThread | null; created: boolean }> {
  return apiFetch("/identity/companion/start", { method: "POST", body: "{}" });
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

/** Kick a corpus-agnostic graph build over everything ingested ("Build graph now").
 * Runs in the background; no-op without an LLM key (status: "skipped"/"disabled"). */
export async function graphBuild(): Promise<{ status: string; delay?: number; reason?: string }> {
  return apiFetch("/graph/build", { method: "POST", body: "{}" });
}

/** Cheap live counts for the dashboard. */
export type GraphStats = {
  nodes: number;
  edges: number;
  communities: number;
  building: boolean;
  embed_dim?: number;
  embed_model?: string;
};
export async function fetchGraphStats(): Promise<GraphStats> {
  return apiFetch("/graph/stats");
}

/** Re-embed the corpus under the current default model (background; restart suggested after). */
export async function reindexEmbeddings(
  body: { model?: string } = {},
): Promise<{ started: boolean; model: string; note?: string }> {
  return apiFetch("/admin/reindex", { method: "POST", body: JSON.stringify(body) });
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

export async function ingestPath(path: string, move = false, temporary = false): Promise<{ queued: string }> {
  return apiFetch("/ingest", {
    method: "POST",
    body: JSON.stringify({ path, move, temporary }),
  });
}

export async function ingestText(body: { title?: string; text: string }): Promise<{ queued: string; kind: string }> {
  return apiFetch("/ingest/text", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export type ClaudeDesktopStatus = {
  installed: boolean;
  configured: boolean;
  connected: boolean;
  config_path: string | null;
};

export async function fetchClaudeDesktopStatus(): Promise<ClaudeDesktopStatus> {
  return apiFetch("/connect/claude-desktop/status");
}

export async function connectClaudeDesktop(body: { server_name?: string; config_path?: string } = {}): Promise<{
  config_path: string;
  backup_path: string | null;
  server_name: string;
  restart_required: boolean;
  installed: boolean;
  configured: boolean;
  message: string;
}> {
  return apiFetch("/connect/claude-desktop", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export type CursorStatus = {
  installed: boolean;
  configured: boolean;
  connected: boolean;
  config_path: string | null;
};

export async function fetchCursorStatus(): Promise<CursorStatus> {
  return apiFetch("/connect/cursor/status");
}

export async function connectCursor(body: { server_name?: string; config_path?: string } = {}): Promise<{
  config_path: string;
  backup_path: string | null;
  server_name: string;
  restart_required: boolean;
  installed: boolean;
  configured: boolean;
  message: string;
}> {
  return apiFetch("/connect/cursor", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

/** Pull a human-readable message out of apiFetch's `status text: {"detail":...}` errors. */
export function apiErrorDetail(e: unknown): string {
  if (!(e instanceof Error)) return String(e);
  const jsonTail = e.message.match(/:\s*(\{[\s\S]*\})\s*$/);
  if (jsonTail) {
    try {
      const parsed = JSON.parse(jsonTail[1]) as { detail?: unknown };
      if (typeof parsed.detail === "string" && parsed.detail.trim()) return parsed.detail.trim();
    } catch {
      /* ignore */
    }
  }
  return e.message;
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
  dom_snapshot?: boolean;
  clipboard_event?: boolean;
  mouse_event?: boolean;
  keyboard_event?: boolean;
  rolling_video_clip?: boolean;
  listening?: boolean;
  full_listening?: boolean;
  screenshot_fallback?: boolean;
  screen_reader?: boolean;
};

export type Settings = {
  disabled_kinds: string[];
  /** When true, do not POST anonymized telemetry to the configured collector. */
  telemetry_opt_out?: boolean;
  /** Opt-in (default off): forward error/crash diagnostics to the collector. */
  remote_monitoring?: boolean;
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

export async function saveGeminiApiKey(apiKey: string): Promise<{ ok: boolean; configured: boolean }> {
  return apiFetch("/settings/gemini-key", {
    method: "PUT",
    body: JSON.stringify({ api_key: apiKey }),
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

export type RevealPathResponse = {
  reveal_path: string;
  resolved_via: "direct" | "file_tracking" | "none";
  exists?: boolean;
  error?: string;
};

export async function resolveRevealPath(path: string): Promise<RevealPathResponse> {
  const q = new URLSearchParams({ path });
  return apiFetch(`/sources/reveal-path?${q}`);
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

/**
 * Best-effort: report a UI error/crash to the sidecar, which forwards it to the
 * remote collector only when the user has opted into monitoring. Swallows all
 * failures — a reporter must never throw into an error handler or boundary.
 * The sidecar may not be reachable (this is often called when things are broken),
 * so a short timeout keeps it from hanging.
 */
export async function reportClientError(
  message: string,
  detail?: string,
  context?: Record<string, unknown>,
): Promise<void> {
  try {
    const cfg = await getConfig();
    const ctrl = new AbortController();
    const t = setTimeout(() => ctrl.abort(), 3000);
    await fetch(`${cfg.api_base}/diagnostics/client-log`, {
      method: "POST",
      headers: authHeaders(cfg),
      signal: ctrl.signal,
      body: JSON.stringify({
        source: "desktop",
        message: String(message).slice(0, 600),
        detail: detail ? String(detail).slice(0, 4000) : undefined,
        context,
      }),
    }).catch(() => {});
    clearTimeout(t);
  } catch {
    /* ignore — reporting is best-effort */
  }
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
  readers: Record<string, {
    allowed_strata?: string[];
    max_release_level?: number;
    deny_chunk_source_kinds?: string[];
    deny_path_substrings?: string[];
    allow_screen_context_tools?: boolean;
    release_without_ok_level?: number;
    release_notice_threshold?: number;
    releasable_chunk_kinds?: string[];
  }>;
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

export type GraphMember = {
  node_id: string;
  node_kind: string;
  title: string;
  summary_snippet?: string;
};

export type GraphHighlight = {
  node_id: string;
  node_kind: string;
  title: string;
  bucket?: string;
  summary_snippet?: string;
};

export type GraphCandidate = {
  candidate_id: string;
  candidate_type: string;
  status: string;
  title: string;
  body_md: string;
  payload: Record<string, unknown>;
  evidence_refs: string[];
  confidence: number;
  source: string;
  created_at: number;
  updated_at: number;
  resolved_at?: number | null;
};

export type GraphScaffoldNode = {
  node_id: string;
  node_kind: string;
  title: string;
  status: string;
  summary?: string;
  filled_count?: number;
  parent_node_id?: string | null;
  members?: GraphMember[];
  children?: GraphScaffoldNode[];
  depth?: number;
};

export type GraphScaffoldResponse = {
  root: GraphScaffoldNode | null;
  tree: GraphScaffoldNode[];
  kinds: GraphKindScaffold[];
  node_types: string[];
  relation_types: string[];
  totals?: Record<string, number>;
  highlights?: GraphHighlight[];
  user_node_count?: number;
  has_fill_gap?: boolean;
  agent?: {
    active_thread_id?: string | null;
    needs_question?: boolean;
    question_preview?: string;
    has_gap?: boolean;
  } | null;
  spine?: {
    active_nodes?: Array<{
      node_id: string;
      node_kind: string;
      title: string;
      attention_hint?: string;
      ambient_refs?: number;
    }>;
    spine_md?: string;
  };
};

export type GraphContextResponse = {
  graph: {
    user_node_count: number;
    totals: Record<string, number>;
    highlights: GraphHighlight[];
    has_fill_gap: boolean;
    next_gap?: Record<string, unknown> | null;
  };
  open_candidates: GraphCandidate[];
  focus?: Record<string, unknown> | null;
  recent_ambient: Array<Record<string, unknown>>;
  recent_ambient_hints?: Array<Record<string, unknown>>;
  related_memory: Array<Record<string, unknown>>;
  generated_at: number;
};

export type ScreenMemorySummary = {
  minutes: number;
  event_count: number;
  top_apps: Array<{ app: string; events: number }>;
  recent_windows: Array<Record<string, unknown>>;
  semantic_events?: ScreenMemoryEvent[];
  summary: string;
};

export type ScreenMemoryEvent = {
  event_id: string;
  occurred_at: number;
  time?: string;
  app: string;
  window: string;
  url?: string | null;
  scene: string;
  visible_elements: Array<Record<string, unknown>>;
  events: Array<Record<string, unknown>>;
  source_refs: string[];
  confidence: number;
  trust_tier: string;
  time_range?: string;
  clip_path?: string;
  raw?: Record<string, unknown>;
};

export type ScreenMemorySearchHit = {
  chunk_id: string;
  score: number;
  path: string;
  kind: string;
  text: string;
  screen_event_id?: string | null;
  app?: string | null;
  window?: string | null;
  trust_tier?: string | null;
  time_range?: string | null;
  clip_path?: string | null;
};

export type ScreenMemoryVideoRange = {
  screen_event_id?: string | null;
  score?: number | null;
  path?: string | null;
  kind?: string | null;
  app?: string | null;
  window?: string | null;
  trust_tier?: string | null;
  time_range?: string | null;
  clip_path?: string | null;
  text?: string;
};

export type ScreenMemorySearchResponse = {
  query: string;
  filters?: {
    app?: string | null;
    after?: number | null;
    before?: number | null;
    time_window?: string | null;
  };
  hits: ScreenMemorySearchHit[];
  video_ranges: ScreenMemoryVideoRange[];
};

export type ScreenMemoryGuidance = {
  mode: string;
  do_this: string;
  candidate?: GraphCandidate;
  gap?: Record<string, unknown>;
  recent?: ScreenMemorySummary;
};

export type MenuStatusResponse = {
  pending_questions: number;
  open_candidates: number;
  next_question?: {
    kind: string;
    title: string;
    body: string;
    action: string;
    candidate_id?: string;
    candidate_type?: string;
    gap?: Record<string, unknown>;
  } | null;
  should_notify?: boolean;
  capture_health: string;
  issues: SystemIssue[];
  graph: GraphContextResponse["graph"];
  focus?: Record<string, unknown> | null;
  generated_at: number;
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

export type AgentSuggestion = { name: string; source?: string };

export type AgentStreamState = {
  active_thread_id: string | null;
  needs_question: boolean;
  open_count: number;
  suggestions?: AgentSuggestion[];
  question_body_md?: string;
  has_gap?: boolean;
};

export type SessionOpenResponse = {
  ok: boolean;
  briefing_md: string;
  request_md: string;
  request_kind?: string;
  thread_id?: string | null;
  created_thread?: boolean;
  delta_summary?: Record<string, unknown>;
  last_open_at?: number;
  opened_at?: number;
};

export type ActivityFeedBundle = {
  now: FeedItem | null;
  items: FeedRow[];
  council_items?: CouncilFeedItem[];
  memory_prefetch: FeedItem[];
  graph: GraphScaffoldResponse;
  agent?: AgentStreamState;
  session?: {
    briefing_summary?: string;
    request_kind?: string;
    request_preview?: string;
    thread_id?: string | null;
    opened_at?: number;
  } | null;
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

export async function agentNext(): Promise<{ thread: ChatThread | null; created: boolean }> {
  return apiFetch("/chat/agent/next", { method: "POST", body: "{}" });
}

export type OnboardingTurn = { role: "assistant" | "user"; content: string };

export async function agentOnboardingReply(body: {
  step: string;
  display_name?: string;
  transcript?: OnboardingTurn[];
  permission_status?: Record<string, string>;
}): Promise<{ message: string; llm: boolean }> {
  return apiFetch("/chat/agent/onboarding", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function saveOnboardingProfile(displayName: string): Promise<{ ok: boolean }> {
  return apiFetch("/onboarding/profile", {
    method: "POST",
    body: JSON.stringify({ display_name: displayName }),
  });
}

export async function openSession(body: { display_name?: string } = {}): Promise<SessionOpenResponse> {
  return apiFetch("/session/open", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export type ResourcePollQuestion = {
  resource_id: string;
  label: string;
  question: string;
};

export async function fetchResourcePollNext(): Promise<{
  question: ResourcePollQuestion | null;
  state: { answers: Record<string, unknown> };
}> {
  return apiFetch("/onboarding/resource-poll/next");
}

export async function answerResourcePoll(body: {
  resource_id: string;
  uses: boolean;
  note?: string;
}): Promise<{
  ok: boolean;
  poll_complete: boolean;
  next_question?: ResourcePollQuestion;
  candidate_id?: string;
  task_id?: string;
}> {
  return apiFetch("/onboarding/resource-poll", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export async function recordConnectorIntent(body: {
  source_text?: string;
  resource_id?: string;
}): Promise<{ ok: boolean; candidate_id?: string; task_id?: string }> {
  return apiFetch("/onboarding/connector-intent", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export type ContextPlatformMeta = {
  schema_version: number;
  layers: string[];
  doc: string;
  privacy_matrix: Record<string, unknown>;
};

export async function fetchContextPlatform(): Promise<ContextPlatformMeta> {
  return apiFetch("/context/platform");
}

export async function agentReply(
  message: string,
  threadId?: string,
  action?: string,
): Promise<{ ok: boolean; thread?: ChatThread; llm?: boolean }> {
  return apiFetch("/chat/agent/reply", {
    method: "POST",
    body: JSON.stringify({ message, thread_id: threadId ?? null, action: action ?? null }),
  });
}

/** Stream Minion agent reply over SSE. */
export function openAgentReplyStream(
  message: string,
  threadId?: string,
  action?: string,
  handlers: {
    onUser?: (msg: { message_id: string; body_md: string }) => void;
    onDelta?: (delta: string) => void;
    onDone?: (thread: ChatThread | null) => void | Promise<void>;
    onError?: (msg: string) => void;
  } = {},
): { cancel: () => void; finished: Promise<void> } {
  let cancelled = false;
  const finished = (async () => {
    const cfg = await getConfig();
    const res = await fetch(`${cfg.api_base}/chat/agent/reply/stream`, {
      method: "POST",
      headers: {
        "content-type": "application/json",
        ...(cfg.api_token ? { authorization: `Bearer ${cfg.api_token}` } : {}),
      },
      body: JSON.stringify({
        message,
        thread_id: threadId ?? null,
        action: action ?? null,
      }),
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
        let ev = "";
        let data = "";
        for (const ln of block.split("\n")) {
          if (ln.startsWith("event:")) ev = ln.slice(6).trim();
          if (ln.startsWith("data:")) data = ln.slice(5).trim();
        }
        if (!ev || !data) continue;
        try {
          const payload = JSON.parse(data) as Record<string, unknown>;
          if (ev === "message.user") {
            handlers.onUser?.({
              message_id: String(payload.message_id ?? ""),
              body_md: String(payload.body_md ?? ""),
            });
          } else if (ev === "message.assistant.delta") {
            handlers.onDelta?.(String(payload.delta ?? ""));
          } else if (ev === "message.assistant.done") {
            const t = payload.thread as ChatThread | undefined;
            await handlers.onDone?.(t ?? null);
          } else if (ev === "done") {
            return;
          } else if (ev === "error") {
            handlers.onError?.(String(payload.message ?? payload.code ?? "stream error"));
            return;
          }
        } catch {
          /* ignore parse errors */
        }
      }
    }
  })();
  return {
    cancel: () => {
      cancelled = true;
    },
    finished,
  };
}

export async function agentDismiss(threadId: string): Promise<{ ok: boolean; thread_id: string }> {
  return apiFetch("/chat/agent/dismiss", {
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
  const raw = await apiFetch<ActivityFeedBundle & { librarian?: AgentStreamState }>(`/feed${qs ? `?${qs}` : ""}`);
  return { ...raw, agent: raw.agent ?? raw.librarian };
}

export async function fetchGraphScaffold(): Promise<GraphScaffoldResponse> {
  return apiFetch("/graph/scaffold");
}

export async function fetchGraphContext(subject = ""): Promise<GraphContextResponse> {
  const qs = subject ? `?subject=${encodeURIComponent(subject)}` : "";
  return apiFetch(`/graph/context${qs}`);
}

export async function fetchGraphCandidates(status = "open"): Promise<{ candidates: GraphCandidate[]; count: number }> {
  return apiFetch(`/graph/candidates?status=${encodeURIComponent(status)}`);
}

export async function resolveGraphCandidate(
  candidateId: string,
  status: "approved" | "rejected" | "dismissed" | "merged",
  payload?: Record<string, unknown>,
): Promise<{ candidate: GraphCandidate; result?: Record<string, unknown> }> {
  return apiFetch(`/graph/candidates/${encodeURIComponent(candidateId)}/resolve`, {
    method: "POST",
    body: JSON.stringify({ status, payload }),
  });
}

export type AuditLogEntry = {
  id: number;
  ts: number;
  entity_type: "identity" | "graph";
  entity_id: string;
  action: string;
  detail: Record<string, unknown>;
};

export type AuditLogResponse = {
  logs: AuditLogEntry[];
  count: number;
};

export async function fetchAuditLog(params: { entity_type?: "identity" | "graph"; limit?: number } = {}): Promise<AuditLogResponse> {
  const q = new URLSearchParams();
  if (params.entity_type) q.set("entity_type", params.entity_type);
  if (params.limit != null) q.set("limit", String(params.limit));
  const qs = q.toString();
  return apiFetch(`/audit${qs ? `?${qs}` : ""}`);
}

export async function rollbackAuditLog(auditId: number): Promise<{ ok: boolean; error?: string }> {
  return apiFetch(`/audit/${auditId}/rollback`, { method: "POST" });
}

export async function fetchMenuStatus(): Promise<MenuStatusResponse> {
  return apiFetch("/menu/status");
}

export async function rememberScreen(params: {
  max_lines?: number;
  ingest_screenshots?: boolean;
  run_adapters?: boolean;
} = {}): Promise<Record<string, unknown>> {
  return apiFetch("/screen-memory/remember", {
    method: "POST",
    body: JSON.stringify(params),
  });
}

export async function searchScreenMemory(
  query: string,
  topK = 8,
  filters: { app?: string; after?: number; before?: number } = {},
): Promise<ScreenMemorySearchResponse> {
  const q = new URLSearchParams({ q: query, top_k: String(topK) });
  if (filters.app) q.set("app", filters.app);
  if (filters.after != null) q.set("after", String(filters.after));
  if (filters.before != null) q.set("before", String(filters.before));
  return apiFetch(`/screen-memory/search?${q.toString()}`);
}

export async function summarizeLastScreen(minutes = 30): Promise<ScreenMemorySummary> {
  return apiFetch(`/screen-memory/summarize-last?minutes=${encodeURIComponent(String(minutes))}`);
}

export async function whatWasIDoing(minutes = 20): Promise<ScreenMemorySummary & { question: string }> {
  return apiFetch(`/screen-memory/what-was-i-doing?minutes=${encodeURIComponent(String(minutes))}`);
}

export async function fetchScreenGuidance(minutes = 30): Promise<ScreenMemoryGuidance> {
  return apiFetch(`/screen-memory/guidance?minutes=${encodeURIComponent(String(minutes))}`);
}

export async function fetchScreenMemoryStatus(minutes = 60, probe = false): Promise<Record<string, unknown>> {
  const q = new URLSearchParams({ minutes: String(minutes), probe: String(probe) });
  return apiFetch(`/screen-memory/status?${q.toString()}`);
}

export async function fetchScreenEvents(minutes = 30, limit = 80): Promise<{ events: ScreenMemoryEvent[]; count: number }> {
  const q = new URLSearchParams({ minutes: String(minutes), limit: String(limit) });
  return apiFetch(`/screen-memory/events?${q.toString()}`);
}

export async function createTaskFromScreen(params: {
  minutes?: number;
  title?: string;
} = {}): Promise<{ created: boolean; task_id?: string; task?: WorkItem; reason?: string; summary?: ScreenMemorySummary }> {
  return apiFetch("/screen-memory/create-task", {
    method: "POST",
    body: JSON.stringify(params),
  });
}

// Profile management types and functions

export type Profile = {
  profile_id: string;
  name: string;
  kind: string;
  is_default: boolean;
  created_at: number;
  updated_at: number;
};

export type ProfileListResponse = {
  profiles: Profile[];
};

export type ProfileCreateRequest = {
  profile_id: string;
  name: string;
  kind?: string;
  is_default?: boolean;
};

export type ProfileUpdateRequest = {
  name?: string;
  is_default?: boolean;
};

export type ProfileSetActiveRequest = {
  profile_id: string;
};

export async function fetchProfiles(): Promise<ProfileListResponse> {
  return apiFetch("/profiles");
}

export async function fetchProfile(profileId: string): Promise<Profile> {
  return apiFetch(`/profiles/${encodeURIComponent(profileId)}`);
}

export async function fetchActiveProfile(): Promise<Profile> {
  return apiFetch("/profiles/active");
}

export async function createProfile(request: ProfileCreateRequest): Promise<Profile> {
  return apiFetch("/profiles", {
    method: "POST",
    body: JSON.stringify(request),
  });
}

export async function updateProfile(profileId: string, request: ProfileUpdateRequest): Promise<Profile> {
  return apiFetch(`/profiles/${encodeURIComponent(profileId)}`, {
    method: "PUT",
    body: JSON.stringify(request),
  });
}

export async function deleteProfile(profileId: string): Promise<{ ok: boolean }> {
  return apiFetch(`/profiles/${encodeURIComponent(profileId)}`, {
    method: "DELETE",
  });
}

export async function setActiveProfile(request: ProfileSetActiveRequest): Promise<{ ok: boolean; profile_id: string }> {
  return apiFetch("/profiles/active", {
    method: "PUT",
    body: JSON.stringify(request),
  });
}

// Export scheduler types and functions

export type ExportSchedulerStatus = {
  enabled: boolean;
  watch_path: string;
  interval_sec: number;
  last_check_at: number | null;
  last_ingested_count: number;
  total_ingested: number;
};

export type ExportSchedulerConfig = {
  export_watch_path?: string;
  export_interval_sec?: number;
  enabled?: boolean;
};

export type ExportSchedulerConfigResponse = {
  ok: boolean;
  settings: {
    export_watch_path?: string;
    export_interval_sec?: number;
  };
};

export async function fetchExportSchedulerStatus(): Promise<ExportSchedulerStatus> {
  return apiFetch("/exports/status");
}

export async function triggerExportExport(params?: { path?: string }): Promise<{ ok: boolean; ingested: number; message: string }> {
  const q = new URLSearchParams();
  if (params?.path) q.set("path", params.path);
  return apiFetch(`/exports/trigger${q ? `?${q.toString()}` : ""}`, {
    method: "POST",
  });
}

export async function updateExportSchedulerConfig(config: ExportSchedulerConfig): Promise<ExportSchedulerConfigResponse> {
  return apiFetch("/exports/config", {
    method: "POST",
    body: JSON.stringify(config),
  });
}

export type TestWorkflowResult = {
  ok: boolean;
  test_file: string;
  source_id: string;
  ingestion_time: number;
  total_time: number;
  semantic_search_found: boolean;
  keyword_search_found: boolean;
  semantic_hits: number;
  keyword_hits: number;
  query: string;
  status: "passed" | "failed";
};

export async function testWorkflow(query = "test"): Promise<TestWorkflowResult> {
  return apiFetch("/test/workflow", {
    method: "POST",
    body: JSON.stringify({ query }),
  });
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

export async function fetchConsentPolicy(profileId?: string): Promise<ConsentPolicy> {
  const q = profileId ? `?profile_id=${encodeURIComponent(profileId)}` : "";
  return apiFetch(`/settings/consent${q}`);
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
