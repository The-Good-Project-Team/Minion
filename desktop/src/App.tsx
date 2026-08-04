import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import {
  Brain,
  CheckCircle2,
  Circle,
  Database,
  FileText,
  FolderOpen,
  GitFork,
  Layers,
  Loader2,
  Network,
  Plug,
  RefreshCw,
  Upload,
  Filter,
  Clock,
  Settings,
} from "lucide-react";
import { open } from "@tauri-apps/plugin-dialog";
import { GraphVisualization } from "./components/GraphVisualization";
import { IdentityMirror } from "./components/IdentityMirror";

import {
  apiErrorDetail,
  connectClaudeDesktop,
  connectCursor,
  fetchAuditLog,
  fetchClaudeDesktopStatus,
  fetchCursorStatus,
  fetchConsentPolicy,
  fetchFeed,
  fetchGraphStats,
  fetchSources,
  fetchStatus,
  graphBuild,
  ingestPath,
  ingestText,
  onSidecarStatus,
  openEvents,
  reindexEmbeddings,
  revealInFinder,
  resolveRevealPath,
  rollbackAuditLog,
  testWorkflow,
  updateConsentPolicy,
  type Active,
  type ActivityFeedBundle,
  type AuditLogEntry,
  type AuditLogResponse,
  type ClaudeDesktopStatus,
  type ConnState,
  type ConsentPolicy,
  type CouncilFeedItem,
  type CursorStatus,
  type EventMsg,
  type FeedItem,
  type FeedRow,
  type GraphStats,
  type SidecarStatus,
  type Source,
  type TestWorkflowResult,
} from "./lib/api";

const NAME_KEY = "minion:name";
const FLAG_PREFIX = "minion:connect:"; // best-effort local marker for click-through connectors

function storedName(): string {
  try {
    return localStorage.getItem(NAME_KEY)?.trim() ?? "";
  } catch {
    return "";
  }
}
function localFlag(id: string): boolean {
  try {
    return localStorage.getItem(FLAG_PREFIX + id) === "1";
  } catch {
    return false;
  }
}
function setLocalFlag(id: string) {
  try {
    localStorage.setItem(FLAG_PREFIX + id, "1");
  } catch {
    /* ignore */
  }
}
function clearLocalFlag(id: string) {
  try {
    localStorage.removeItem(FLAG_PREFIX + id);
  } catch {
    /* ignore */
  }
}

const EMPTY_ACTIVE: Active = { root: null, total: 0, done: 0, added: 0, skipped: 0 };

/** One rolling line per file path so progress rewrites in place (v1 terminal feel). */
type FeedLine = { path: string; stage: string; state: "running" | "added" | "skipped" | "failed" };

/**
 * Map raw backend stages to plain language a user understands, with a calm
 * progression of colour: violet (setting up) → blue (reading) → indigo
 * (understanding) → purple (learning) → green (stored). Skips are neutral
 * grey; only real failures are red.
 */
function stageLabel(stage: string, state: FeedLine["state"]): { text: string; cls: string } {
  if (state === "added") return { text: "Stored", cls: "text-green-600" };
  if (state === "failed") return { text: "Couldn't read", cls: "text-red-600" };
  if (state === "skipped") return { text: "Already known", cls: "text-slate-400" };
  switch (stage) {
    case "queued":
      return { text: "Setting up", cls: "text-violet-500" };
    case "unpack_start":
    case "unpack_done":
      return { text: "Unpacking", cls: "text-sky-500" };
    case "parse_start":
      return { text: "Reading", cls: "text-blue-500" };
    case "parsed":
      return { text: "Understanding", cls: "text-indigo-500" };
    case "embed":
    case "embedding":
      return { text: "Learning", cls: "text-purple-500" };
    case "indexed":
      return { text: "Stored", cls: "text-green-600" };
    default:
      return { text: "Working", cls: "text-blue-500" };
  }
}

function baseName(p: string): string {
  const parts = p.split(/[\\/]/);
  return parts[parts.length - 1] || p;
}

/** "BAAI/bge-base-en-v1.5" -> "bge-base-en-v1.5" (drop the org prefix). */
function shortModel(name: string): string {
  return name.split("/").pop() || name;
}

function StatTile({
  icon,
  label,
  value,
  sub,
  tint = "text-primary",
}: {
  icon: ReactNode;
  label: string;
  value: ReactNode;
  sub?: string;
  tint?: string;
}) {
  return (
    <div className="rounded-2xl border border-border bg-card p-4">
      <div className={`inline-flex items-center gap-1.5 text-xs font-medium ${tint}`}>
        {icon}
        {label}
      </div>
      <div className="mt-1 truncate text-xl font-semibold text-foreground">{value}</div>
      {sub && <div className="truncate text-xs text-muted-foreground">{sub}</div>}
    </div>
  );
}

function looksLikeExport(s: Source): boolean {
  const hay = `${s.path} ${String(s.meta?.title ?? "")} ${String(s.meta?.source ?? "")}`.toLowerCase();
  return /chatgpt|openai|claude|anthropic|perplexity|gemini|cursor|copilot|grok/.test(hay) || s.kind === "chatgpt-export";
}

type ChecklistItem = {
  id: string;
  label: string;
  detail: string;
  done: boolean;
  action?: { label: string; run: () => void | Promise<void> };
};

function SettingsView({
  consentPolicy,
  setConsentPolicy,
  consentError,
  auditLog,
  auditFilter,
  setAuditFilter,
  loadAuditLog,
  formatFeedTime,
}: {
  consentPolicy: ConsentPolicy | null;
  setConsentPolicy: (policy: ConsentPolicy) => void;
  consentError: string | null;
  auditLog: AuditLogEntry[];
  auditFilter: "all" | "identity" | "graph";
  setAuditFilter: (filter: "all" | "identity" | "graph") => void;
  loadAuditLog: () => void;
  formatFeedTime: (ts: number) => string;
}) {
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState("");
  const [rollingBack, setRollingBack] = useState<number | null>(null);

  const strata = [
    { id: "raw_evidence", label: "Raw Evidence", desc: "Full ambient/screen chunk text" },
    { id: "summaries", label: "Summaries", desc: "Rolled-up ambient summaries" },
    { id: "graph_facts", label: "Graph Facts", desc: "Durable graph nodes and edges" },
    { id: "work_context", label: "Work Context", desc: "Releasable current-work summaries" },
    { id: "preferences", label: "Preferences", desc: "Identity preference claims" },
    { id: "projections", label: "Projections", desc: "Composed context bundles" },
  ];

  const readers = ["local_ui", "mcp", "connector_builder", "export_bundle"];

  const getReaderSummary = (readerId: string) => {
    if (!consentPolicy) return "";
    const reader = consentPolicy.readers[readerId];
    if (!reader) return "No policy set";
    const allowedStrataIds = reader.allowed_strata || [];
    const maxLevel = reader.max_release_level ?? 3;
    const strataLabels = allowedStrataIds
      .map(stratumId => strata.find(st => st.id === stratumId)?.label || stratumId)
      .join(", ");
    return `Access: ${strataLabels || "None"} · Max Level: ${maxLevel}/5`;
  };

  const toggleStratum = async (readerId: string, stratumId: string) => {
    if (!consentPolicy) return;
    const updated = { ...consentPolicy };
    const currentStrata = updated.readers[readerId]?.allowed_strata || [];
    const newStrata = currentStrata.includes(stratumId)
      ? currentStrata.filter((s) => s !== stratumId)
      : [...currentStrata, stratumId];
    updated.readers[readerId] = {
      ...updated.readers[readerId],
      allowed_strata: newStrata,
    };
    setConsentPolicy(updated);
    setSaving(true);
    setSaveMsg("");
    try {
      await updateConsentPolicy(updated);
      setSaveMsg("Saved");
    } catch (e) {
      setSaveMsg(e instanceof Error ? e.message : "Failed to save");
    } finally {
      setSaving(false);
    }
  };

  const updateReleaseLevel = async (readerId: string, level: number) => {
    if (!consentPolicy) return;
    const updated = { ...consentPolicy };
    updated.readers[readerId] = {
      ...updated.readers[readerId],
      max_release_level: level,
    };
    setConsentPolicy(updated);
    setSaving(true);
    setSaveMsg("");
    try {
      await updateConsentPolicy(updated);
      setSaveMsg("Saved");
    } catch (e) {
      setSaveMsg(e instanceof Error ? e.message : "Failed to save");
    } finally {
      setSaving(false);
    }
  };

  const handleRollback = async (auditId: number) => {
    setRollingBack(auditId);
    try {
      const result = await rollbackAuditLog(auditId);
      if (result.ok) {
        loadAuditLog();
      } else {
        console.error("Rollback failed:", result.error);
      }
    } catch (e) {
      console.error("Rollback error:", e);
    } finally {
      setRollingBack(null);
    }
  };

  if (!consentPolicy) {
    return (
      <div className="mt-6">
        <p className="text-sm text-muted-foreground">Loading consent policy…</p>
        {consentError && <p className="mt-2 text-sm text-red-600">{consentError}</p>}
      </div>
    );
  }

  return (
    <div className="mt-6 space-y-6">
      <div className="flex items-center justify-between">
        <h2 className="text-lg font-medium">Consent Policy</h2>
        {saveMsg && (
          <span className={`text-sm ${saveMsg === "Saved" ? "text-green-600" : "text-red-600"}`}>
            {saveMsg}
          </span>
        )}
      </div>

      <p className="text-sm text-muted-foreground">
        Control what each reader can access. Local UI sees everything by default. MCP tools are restricted to summaries and graph facts.
      </p>

      {/* Real-time preview */}
      <section className="rounded-2xl border border-border bg-card p-4">
        <h3 className="mb-3 font-medium">Real-time Preview</h3>
        <div className="space-y-2">
          {readers.map((readerId) => (
            <div key={readerId} className="rounded-lg bg-muted/50 p-3">
              <p className="text-sm font-medium capitalize">{readerId.replace("_", " ")}</p>
              <p className="text-xs text-muted-foreground">{getReaderSummary(readerId)}</p>
            </div>
          ))}
        </div>
      </section>

      {readers.map((readerId) => (
        <section key={readerId} className="rounded-2xl border border-border bg-card p-4">
          <h3 className="mb-3 font-medium capitalize">{readerId.replace("_", " ")}</h3>
          <div className="mb-4">
            <label className="text-xs text-muted-foreground">Max Release Level (0-5)</label>
            <div className="mt-1 flex gap-2">
              {[0, 1, 2, 3, 4, 5].map((level) => (
                <button
                  key={level}
                  onClick={() => void updateReleaseLevel(readerId, level)}
                  className={`rounded px-2 py-1 text-xs ${
                    (consentPolicy.readers[readerId]?.max_release_level ?? 3) === level
                      ? "bg-primary text-primary-foreground"
                      : "bg-muted hover:bg-muted/80"
                  }`}
                >
                  {level}
                </button>
              ))}
            </div>
          </div>
          <div className="space-y-2">
            {strata.map((stratum) => {
              const allowed = consentPolicy.readers[readerId]?.allowed_strata?.includes(stratum.id) ?? false;
              return (
                <div key={stratum.id} className="flex items-center justify-between rounded-lg p-2 hover:bg-accent/40">
                  <div>
                    <p className="text-sm font-medium">{stratum.label}</p>
                    <p className="text-xs text-muted-foreground">{stratum.desc}</p>
                  </div>
                  <button
                    onClick={() => void toggleStratum(readerId, stratum.id)}
                    disabled={saving}
                    className={`shrink-0 rounded px-3 py-1.5 text-xs font-medium transition-colors ${
                      allowed
                        ? "bg-primary text-primary-foreground hover:bg-primary/90"
                        : "bg-muted text-muted-foreground hover:bg-muted/80"
                    }`}
                  >
                    {allowed ? "Allowed" : "Blocked"}
                  </button>
                </div>
              );
            })}
          </div>
        </section>
      ))}

      {/* Audit Log */}
      <section className="rounded-2xl border border-border bg-card p-4">
        <div className="flex items-center justify-between mb-4">
          <h2 className="inline-flex items-center gap-1.5 font-medium">
            <Clock className="size-4 text-primary" /> Audit Log
          </h2>
          <select
            value={auditFilter}
            onChange={(e) => {
              setAuditFilter(e.target.value as any);
              loadAuditLog();
            }}
            className="rounded-lg border border-border bg-background px-2 py-1 text-xs hover:bg-accent"
          >
            <option value="all">All Changes</option>
            <option value="identity">Identity</option>
            <option value="graph">Graph</option>
          </select>
        </div>

        <div className="space-y-2 max-h-96 overflow-y-auto">
          {auditLog.length === 0 ? (
            <p className="text-sm text-muted-foreground text-center py-4">No audit log entries</p>
          ) : (
            auditLog.map((entry) => (
              <div key={entry.id} className="rounded-lg bg-muted/30 p-3 text-xs">
                <div className="flex items-center justify-between mb-1">
                  <span className={`font-medium ${
                    entry.entity_type === "identity" ? "text-blue-600" : "text-purple-600"
                  }`}>
                    {entry.entity_type}
                  </span>
                  <span className="text-muted-foreground">
                    {formatFeedTime(entry.ts)}
                  </span>
                </div>
                <div className="font-medium">{entry.action}</div>
                {entry.entity_id && (
                  <div className="text-muted-foreground truncate">ID: {entry.entity_id}</div>
                )}
                {Object.keys(entry.detail).length > 0 && (
                  <details className="mt-1">
                    <summary className="cursor-pointer text-muted-foreground hover:text-foreground">
                      Details
                    </summary>
                    <pre className="mt-1 text-xs bg-background p-2 rounded overflow-x-auto">
                      {JSON.stringify(entry.detail, null, 2)}
                    </pre>
                  </details>
                )}
                {entry.entity_type === "identity" && entry.entity_id && (
                  <button
                    onClick={() => handleRollback(entry.id)}
                    disabled={rollingBack === entry.id}
                    className="mt-2 w-full rounded-lg border border-border px-2 py-1 text-xs hover:bg-accent disabled:opacity-50"
                  >
                    {rollingBack === entry.id ? "Rolling back..." : "Rollback"}
                  </button>
                )}
              </div>
            ))
          )}
        </div>
      </section>
    </div>
  );
}

export function App() {
  const [name, setName] = useState(storedName());
  const [nameDraft, setNameDraft] = useState("");
  const [sources, setSources] = useState<Source[]>([]);
  const [counts, setCounts] = useState({ sources: 0, chunks: 0 });
  const [active, setActive] = useState<Active>(EMPTY_ACTIVE);
  const [feed, setFeed] = useState<FeedLine[]>([]);
  const [conn, setConn] = useState<ConnState>("connecting");
  const [sourceTypeFilter, setSourceTypeFilter] = useState<"file" | "chat_export" | "external" | "ambient" | "all">("all");
  const [timeRangeFilter, setTimeRangeFilter] = useState<"last_hour" | "last_day" | "last_week" | "all">("all");
  const [sidecar, setSidecar] = useState<SidecarStatus | null>(null);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [graphMsg, setGraphMsg] = useState("");
  const [graphBusy, setGraphBusy] = useState(false);
  const [graphStats, setGraphStats] = useState<GraphStats | null>(null);
  const [reindexMsg, setReindexMsg] = useState("");
  const [claudeStatus, setClaudeStatus] = useState<ClaudeDesktopStatus | null>(null);
  const [claudeMsg, setClaudeMsg] = useState("");
  const [cursorStatus, setCursorStatus] = useState<CursorStatus | null>(null);
  const [cursorMsg, setCursorMsg] = useState("");
  const [revealError, setRevealError] = useState<string | null>(null);
  const [currentTab, setCurrentTab] = useState<"home" | "graph" | "settings">("home");
  const [consentPolicy, setConsentPolicy] = useState<ConsentPolicy | null>(null);
  const [consentError, setConsentError] = useState<string | null>(null);
  const [activityFeed, setActivityFeed] = useState<ActivityFeedBundle | null>(null);
  const [activityFeedLoading, setActivityFeedLoading] = useState(true);
  const [activityFeedError, setActivityFeedError] = useState<string | null>(null);
  const [activityFeedFilter, setActivityFeedFilter] = useState<"all" | "ingest" | "ambient" | "graph" | "errors">("all");
  const [activityTimeRange, setActivityTimeRange] = useState<"last_hour" | "last_day" | "last_week" | "all">("last_day");
  const [collapsedSections, setCollapsedSections] = useState<Record<string, boolean>>({});
  const [auditLog, setAuditLog] = useState<AuditLogEntry[]>([]);
  const [auditFilter, setAuditFilter] = useState<"all" | "identity" | "graph">("all");
  const [testRunning, setTestRunning] = useState(false);
  const [testResult, setTestResult] = useState<TestWorkflowResult | null>(null);
  const dropRef = useRef<(files: FileList | null) => void>(() => {});

  const loadActivityFeed = useCallback(async (retryCount = 0) => {
    // Only show loading state if we don't have data yet
    if (!activityFeed) {
      setActivityFeedLoading(true);
    }
    setActivityFeedError(null);
    try {
      const sinceHours = activityTimeRange === "all" ? 168 : activityTimeRange === "last_week" ? 168 : activityTimeRange === "last_day" ? 24 : 1;
      const feed = await fetchFeed({ limit: 100, since_hours: sinceHours });
      setActivityFeed(feed);
      setActivityFeedLoading(false);
    } catch (e) {
      console.error("Failed to load activity feed:", e);
      if (retryCount < 2) {
        // Retry automatically after a delay
        setTimeout(() => loadActivityFeed(retryCount + 1), 1000 * (retryCount + 1));
      } else {
        // Only show error if we don't have any data
        if (!activityFeed) {
          setActivityFeedError("Failed to load activity feed");
        }
        setActivityFeedLoading(false);
      }
    }
  }, [activityTimeRange, activityFeed]);

  const loadAuditLog = useCallback(async () => {
    try {
      const response = await fetchAuditLog({ 
        entity_type: auditFilter === "all" ? undefined : auditFilter, 
        limit: 100 
      });
      setAuditLog(response.logs);
    } catch (e) {
      console.error("Failed to load audit log:", e);
    }
  }, [auditFilter]);

  const load = useCallback(async () => {
    setConn("connecting");
    try {
      const consent = await fetchConsentPolicy().catch(() => null);
      if (consent) {
        setConsentPolicy(consent);
        setConsentError(null);
      } else {
        setConsentError("Failed to load consent policy");
      }
    } catch (e) {
      setConsentError(e instanceof Error ? e.message : "Failed to load consent policy");
    }

    try {
      const [st, srcs, gs, claude, cursor] = await Promise.all([
        fetchStatus().catch(() => null),
        fetchSources({
          limit: 500,
          source_type: sourceTypeFilter === "all" ? undefined : sourceTypeFilter,
          time_range: timeRangeFilter === "all" ? undefined : timeRangeFilter,
        }).then((r) => r.sources).catch(() => [] as Source[]),
        fetchGraphStats().catch(() => null),
        fetchClaudeDesktopStatus().catch(() => null),
        fetchCursorStatus().catch(() => null),
      ]);
      if (st) {
        setCounts(st.counts);
        setActive(st.active ?? EMPTY_ACTIVE);
      }
      setSources(srcs);
      if (gs) setGraphStats(gs);
      if (claude) {
        setClaudeStatus(claude);
        if (!claude.connected) clearLocalFlag("claude");
      }
      if (cursor) {
        setCursorStatus(cursor);
        if (cursor.connected) {
          setLocalFlag("cursor");
        } else {
          clearLocalFlag("cursor");
        }
      }
    } catch (e) {
      console.error("Failed to load dashboard data:", e);
    }
  }, [sourceTypeFilter, timeRangeFilter]);

  // Coalesce the per-file refreshes the ingest stream triggers. The backend
  // emits one `source_updated` per file; on a big corpus (100k+ chunks) calling
  // the full `load()` (3 fetches + a 500-row setState) on every event thrashed
  // the WebView heap until macOS killed the renderer → blank white window.
  // Debounce to a single trailing refresh; live counts still tick via e.counts.
  const loadTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const scheduleLoad = useCallback(() => {
    if (loadTimer.current) clearTimeout(loadTimer.current);
    loadTimer.current = setTimeout(() => {
      loadTimer.current = null;
      void load();
    }, 1500);
  }, [load]);
  useEffect(() => () => {
    if (loadTimer.current) clearTimeout(loadTimer.current);
  }, []);

  // Poll graph stats while a build is in progress so the dashboard ticks live.
  useEffect(() => {
    if (!graphStats?.building) return;
    const id = setInterval(() => {
      void fetchGraphStats().then(setGraphStats).catch(() => {});
    }, 3000);
    return () => clearInterval(id);
  }, [graphStats?.building]);

  // Live ingest stream → progress + rolling per-file feed.
  useEffect(() => {
    let cleanup: (() => void) | undefined;
    void openEvents(
      (e: EventMsg) => {
        if ("active" in e && e.active) {
          const a = e.active;
          setActive(a.total > 0 ? a : EMPTY_ACTIVE);
        }
        switch (e.type) {
          case "snapshot":
          case "ready":
          case "heartbeat":
            if (e.counts) setCounts(e.counts);
            break;
          case "ingest_started":
            if (e.path) pushFeed(e.path, "queued", "running");
            break;
          case "ingest_progress":
          case "file_progress":
            pushFeed(e.path, "stage" in e ? String(e.stage) : "embedding", "running");
            break;
          case "ingest_failed":
            pushFeed(e.path, "failed", "failed");
            break;
          case "source_updated": {
            const path = String((e.result as any)?.path ?? "");
            if (path) pushFeed(path, "indexed", "added");
            if (e.counts) setCounts(e.counts);
            scheduleLoad();
            break;
          }
          case "tree_done":
            setFeed([]);
            setActive(EMPTY_ACTIVE);
            void load();
            break;
        }
      },
      (s) => setConn(s),
    ).then((c) => (cleanup = c));
    return () => cleanup?.();
  }, [load, scheduleLoad]);

  useEffect(() => {
    // A clean mount means we're past any crash loop; reset the reload guard.
    try {
      sessionStorage.removeItem("minion:reload-tries");
    } catch {
      /* ignore */
    }
    void load();
    void onSidecarStatus((s) => setSidecar(s));
  }, [load]);

  useEffect(() => {
    // Only load activity feed when sidecar is ready
    if (sidecar?.state === "ready") {
      void loadActivityFeed();
    }
  }, [sidecar?.state, loadActivityFeed]);

  useEffect(() => {
    if (currentTab === "settings") {
      void loadAuditLog();
    }
  }, [currentTab, loadAuditLog]);

  function pushFeed(path: string, stage: string, state: FeedLine["state"]) {
    setFeed((prev) => {
      const next = prev.filter((l) => l.path !== path);
      next.unshift({ path, stage, state });
      return next.slice(0, 12);
    });
  }

  function toggleSection(sectionId: string) {
    setCollapsedSections(prev => ({ ...prev, [sectionId]: !prev[sectionId] }));
  }

  function formatFeedTime(ts: number): string {
    const now = Date.now() / 1000;
    const diff = now - ts;
    if (diff < 60) return "just now";
    if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
    if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
    return `${Math.floor(diff / 86400)}d ago`;
  }

  function isFeedItem(item: FeedRow): item is FeedItem {
    return item.item_kind !== "council";
  }

  function isCouncilFeedItem(item: FeedRow): item is CouncilFeedItem {
    return item.item_kind === "council";
  }

  function getFeedItemType(item: FeedRow): "ingest" | "ambient" | "graph" | "errors" | "other" {
    if (item.item_kind === "council") return "graph";
    const kind = item.kind?.toLowerCase() || "";
    if (kind.includes("ingest") || kind.includes("source") || kind.includes("indexed")) return "ingest";
    if (kind.includes("ambient") || kind.includes("screen") || kind.includes("focus")) return "ambient";
    if (kind.includes("graph") || kind.includes("node") || kind.includes("edge")) return "graph";
    if (kind.includes("error") || kind.includes("failed") || kind.includes("issue")) return "errors";
    return "other";
  }

  // --- ingest actions (reuse the v1 path-based ingest) ---
  async function addPaths(paths: string[]) {
    if (!paths.length || busy) return;
    setBusy(true);
    try {
      await Promise.all(paths.map((p) => ingestPath(p, false, true)));
      await load();
    } finally {
      setBusy(false);
    }
  }
  async function addFiles() {
    const sel = await open({ multiple: true, directory: false, title: "Add files to Minion" });
    await addPaths(Array.isArray(sel) ? sel : sel ? [sel] : []);
  }
  async function addFolder() {
    const sel = await open({ multiple: false, directory: true, title: "Add a folder to Minion" });
    await addPaths(Array.isArray(sel) ? sel : sel ? [sel] : []);
  }
  async function handleDropped(files: FileList | null) {
    if (!files || !files.length) return;
    const arr = Array.from(files);
    const nativePaths = arr
      .map((f) => (f as File & { path?: string }).path)
      .filter((p): p is string => typeof p === "string" && p.length > 0);
    if (nativePaths.length) {
      await addPaths(nativePaths);
      return;
    }
    // Browser fallback: ingest readable text directly.
    setBusy(true);
    try {
      for (const f of arr) {
        const text = await f.text().catch(() => "");
        if (text.trim()) await ingestText({ title: f.name || "Dropped file", text });
      }
      await load();
    } finally {
      setBusy(false);
    }
  }
  dropRef.current = handleDropped;

  // window-level drag/drop (Tauri delivers native f.path on drop)
  useEffect(() => {
    let depth = 0;
    const hasFiles = (e: DragEvent) => Array.from(e.dataTransfer?.types ?? []).includes("Files");
    const onEnter = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      depth += 1;
      setDragging(true);
    };
    const onOver = (e: DragEvent) => hasFiles(e) && e.preventDefault();
    const onLeave = () => {
      depth = Math.max(0, depth - 1);
      if (depth === 0) setDragging(false);
    };
    const onDrop = (e: DragEvent) => {
      e.preventDefault();
      depth = 0;
      setDragging(false);
      dropRef.current(e.dataTransfer?.files ?? null);
    };
    window.addEventListener("dragenter", onEnter);
    window.addEventListener("dragover", onOver);
    window.addEventListener("dragleave", onLeave);
    window.addEventListener("drop", onDrop);
    return () => {
      window.removeEventListener("dragenter", onEnter);
      window.removeEventListener("dragover", onOver);
      window.removeEventListener("dragleave", onLeave);
      window.removeEventListener("drop", onDrop);
    };
  }, []);

  async function runGraphBuild() {
    setGraphBusy(true);
    setGraphMsg("");
    try {
      const r = await graphBuild();
      setGraphMsg(
        r.status === "scheduled"
          ? "Building your knowledge graph in the background…"
          : r.status === "skipped" && r.reason === "no_llm"
            ? "Add a Gemini API key in Settings to build the graph."
            : `Graph build: ${r.status}`,
      );
    } catch (e) {
      setGraphMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setGraphBusy(false);
    }
  }

  async function runReindex() {
    setReindexMsg("Re-embedding the corpus… this runs in the background; restart when it finishes.");
    try {
      const r = await reindexEmbeddings();
      setReindexMsg(`Re-embedding under ${r.model}. ${r.note ?? ""}`);
    } catch (e) {
      setReindexMsg(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleReveal(sourcePath: string) {
    setRevealError(null);
    try {
      const resolved = await resolveRevealPath(sourcePath);
      if (resolved.error) {
        setRevealError(resolved.error);
        return;
      }
      await revealInFinder(resolved.reveal_path);
    } catch (e) {
      setRevealError(e instanceof Error ? e.message : String(e));
    }
  }

  async function runTestWorkflow() {
    setTestRunning(true);
    setTestResult(null);
    try {
      const result = await testWorkflow("workflow verification");
      setTestResult(result);
    } catch (e) {
      console.error("Test workflow failed:", e);
      setTestResult({
        ok: false,
        test_file: "",
        source_id: "",
        ingestion_time: 0,
        total_time: 0,
        semantic_search_found: false,
        keyword_search_found: false,
        semantic_hits: 0,
        keyword_hits: 0,
        query: "test",
        status: "failed",
      });
    } finally {
      setTestRunning(false);
    }
  }

  const checklist: ChecklistItem[] = useMemo(() => {
    const exportCount = sources.filter(looksLikeExport).length;
    return [
      {
        id: "name",
        label: "Name set",
        detail: name ? `Hi, ${name}` : "Tell Minion what to call you",
        done: Boolean(name),
      },
      {
        id: "files",
        label: "Add your files & folders",
        detail: counts.sources ? `${counts.sources} source${counts.sources === 1 ? "" : "s"} indexed` : "Drop documents, notes, exports",
        done: counts.sources > 0,
        action: { label: "Add files", run: addFiles },
      },
      {
        id: "ai_exports",
        label: "AI chat exports",
        detail: exportCount ? `${exportCount} export${exportCount === 1 ? "" : "s"} added` : "ChatGPT, Claude, Cursor, etc.",
        done: exportCount > 0,
        action: { label: "Add export", run: addFiles },
      },
      {
        id: "claude",
        label: "Connect Claude Desktop (optional)",
        detail:
          claudeMsg ||
          (claudeStatus?.connected
            ? "Claude can read your memory"
            : claudeStatus?.configured && !claudeStatus?.installed
              ? "Config saved — install Claude Desktop to use it"
              : "Optional — lets Claude query your memory via MCP"),
        done: Boolean(claudeStatus?.connected),
        action: {
          label: claudeStatus?.connected ? "Connected" : "Connect",
          run: async () => {
            setClaudeMsg("");
            try {
              const r = await connectClaudeDesktop({});
              setLocalFlag("claude");
              setClaudeStatus({
                installed: r.installed,
                configured: r.configured,
                connected: r.installed && r.configured,
                config_path: r.config_path,
              });
              setClaudeMsg(r.message);
            } catch (e) {
              clearLocalFlag("claude");
              setClaudeMsg(apiErrorDetail(e));
            }
          },
        },
      },
      {
        id: "cursor",
        label: "Connect Cursor (optional)",
        detail:
          cursorMsg ||
          (cursorStatus?.connected
            ? "Cursor can read your memory"
            : cursorStatus?.configured && !cursorStatus?.installed
              ? "Config saved — install Cursor to use it"
              : "Optional — lets Cursor query your memory via MCP"),
        done: Boolean(cursorStatus?.connected),
        action: {
          label: cursorStatus?.connected ? "Connected" : "Connect",
          run: async () => {
            setCursorMsg("");
            try {
              const r = await connectCursor({});
              setLocalFlag("cursor");
              setCursorStatus({
                installed: r.installed,
                configured: r.configured,
                connected: r.installed && r.configured,
                config_path: r.config_path,
              });
              setCursorMsg(r.message);
            } catch (e) {
              clearLocalFlag("cursor");
              setCursorMsg(apiErrorDetail(e));
            }
          },
        },
      },
      {
        id: "graph",
        label: "Knowledge graph",
        detail:
          graphMsg ||
          (counts.sources > 0
            ? "Builds automatically as you add files — people, projects & topics"
            : "Add files and Minion maps people, projects & topics for you"),
        done: counts.sources > 0,
        action: counts.sources > 0 ? { label: graphBusy ? "Rebuilding…" : "Rebuild", run: runGraphBuild } : undefined,
      },
    ];
  }, [name, counts.sources, sources, graphMsg, graphBusy, claudeStatus, claudeMsg]);

  const doneCount = checklist.filter((c) => c.done).length;
  const pct = active.total > 0 ? Math.round((active.done / active.total) * 100) : 0;
  const ingesting = active.total > 0 && active.done < active.total;

  if (!name) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-background p-8 text-foreground">
        <div className="w-full max-w-md rounded-2xl border border-border bg-card p-8 shadow-sm">
          <img src="/logo.png" alt="Minion" className="mb-4 size-16 rounded-2xl" />
          <h1 className="font-serif text-3xl">Welcome to Minion</h1>
          <p className="mt-2 text-sm text-muted-foreground">
            Your data stays on this Mac. Drop files in, connect your tools, and Minion builds a private, searchable memory.
          </p>
          <label className="mt-6 block text-sm font-medium">What should I call you?</label>
          <input
            autoFocus
            value={nameDraft}
            onChange={(e) => setNameDraft(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && nameDraft.trim()) {
                localStorage.setItem(NAME_KEY, nameDraft.trim());
                setName(nameDraft.trim());
              }
            }}
            placeholder="Your name"
            className="mt-2 w-full rounded-lg border border-input bg-background px-3 py-2 outline-none focus:ring-2 focus:ring-ring"
          />
          <button
            disabled={!nameDraft.trim()}
            onClick={() => {
              localStorage.setItem(NAME_KEY, nameDraft.trim());
              setName(nameDraft.trim());
            }}
            className="mt-4 w-full rounded-lg bg-primary px-4 py-2 font-medium text-primary-foreground disabled:opacity-50"
          >
            Get started
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="min-h-screen bg-background text-foreground">
      {/* drag overlay */}
      {dragging && (
        <div className="pointer-events-none fixed inset-0 z-50 flex items-center justify-center bg-primary/10 backdrop-blur-sm">
          <div className="rounded-2xl border-2 border-dashed border-primary bg-card px-10 py-8 text-center shadow-lg">
            <Upload className="mx-auto size-8 text-primary" />
            <p className="mt-2 font-medium">Drop to add to your memory</p>
          </div>
        </div>
      )}

      <div className="mx-auto max-w-3xl px-6 py-8">
        <header className="flex items-center justify-between">
          <div className="flex items-center gap-3">
            <img src="/logo.png" alt="Minion" className="size-10 rounded-xl" />
            <div>
            <h1 className="font-serif text-2xl">Minion</h1>
            <p className="text-sm text-muted-foreground">
              {counts.sources} sources · {counts.chunks} chunks
              <span className={`ml-2 ${conn === "open" ? "text-primary" : "text-muted-foreground"}`}>
                ● {conn === "open" ? "connected" : conn}
              </span>
            </p>
            </div>
          </div>
          <div className="flex items-center gap-2">
            <button
              onClick={() => setCurrentTab("home")}
              className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm hover:bg-accent ${
                currentTab === "home" ? "bg-accent" : ""
              }`}
            >
              <Brain className="size-4" /> Home
            </button>
            <button
              onClick={() => setCurrentTab("graph")}
              className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm hover:bg-accent ${
                currentTab === "graph" ? "bg-accent" : ""
              }`}
            >
              <Network className="size-4" /> Graph
            </button>
            <button
              onClick={() => setCurrentTab("settings")}
              className={`inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm hover:bg-accent ${
                currentTab === "settings" ? "bg-accent" : ""
              }`}
            >
              <Settings className="size-4" /> Settings
            </button>
            <button
              onClick={runReindex}
              title="Re-embed corpus under the current model"
              className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm hover:bg-accent"
            >
              <RefreshCw className="size-4" /> Reindex
            </button>
            <button
              onClick={runTestWorkflow}
              title="Test workflow: drop sample file and verify retrieval"
              disabled={testRunning}
              className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm hover:bg-accent disabled:opacity-50"
            >
              {testRunning ? <Loader2 className="size-4 animate-spin" /> : <CheckCircle2 className="size-4" />} Test
            </button>
          </div>
        </header>

        {currentTab === "home" && (
          <>
            {/* live dashboard */}
            <section className="mt-6 grid grid-cols-2 gap-3 sm:grid-cols-4">
          <StatTile icon={<FileText className="size-4" />} label="Sources" value={counts.sources} tint="text-sky-500" />
          <StatTile icon={<Database className="size-4" />} label="Chunks" value={counts.chunks} tint="text-blue-500" />
          <StatTile
            icon={<GitFork className="size-4" />}
            label="Knowledge graph"
            tint="text-violet-500"
            value={
              graphStats?.building ? (
                <span className="inline-flex items-center gap-1.5 text-base text-violet-500">
                  <Loader2 className="size-4 animate-spin" /> Building…
                </span>
              ) : graphStats && (graphStats.nodes > 0 || graphStats.edges > 0) ? (
                `${graphStats.nodes} branches · ${graphStats.edges} edges`
              ) : (
                "—"
              )
            }
            sub={graphStats && graphStats.communities > 0 ? `${graphStats.communities} communities` : undefined}
          />
          <StatTile
            icon={<Brain className="size-4" />}
            label="Embedder"
            tint="text-emerald-500"
            value={graphStats?.embed_dim ? `${graphStats.embed_dim}-d` : "—"}
            sub={graphStats?.embed_model ? shortModel(graphStats.embed_model) : undefined}
          />
        </section>

        {sidecar && sidecar.state !== "ready" && (
          <div className="mt-4 rounded-lg border border-border bg-card p-3 text-sm text-muted-foreground">
            Starting up: {sidecar.state}…
          </div>
        )}

        {testResult && (
          <div className={`mt-4 rounded-lg border border-border bg-card p-4 text-sm ${
            testResult.status === "passed" ? "border-green-500/50" : "border-red-500/50"
          }`}>
            <div className="flex items-center justify-between mb-2">
              <span className={`font-medium ${testResult.status === "passed" ? "text-green-600" : "text-red-600"}`}>
                {testResult.status === "passed" ? "✓ Test passed" : "✗ Test failed"}
              </span>
              <button
                onClick={() => setTestResult(null)}
                className="text-muted-foreground hover:text-foreground"
              >
                ×
              </button>
            </div>
            <div className="space-y-1 text-xs text-muted-foreground">
              <div>Ingestion: {testResult.ingestion_time}s · Total: {testResult.total_time}s</div>
              <div>Semantic search: {testResult.semantic_search_found ? "✓" : "✗"} ({testResult.semantic_hits} hits)</div>
              <div>Keyword search: {testResult.keyword_search_found ? "✓" : "✗"} ({testResult.keyword_hits} hits)</div>
            </div>
          </div>
        )}

        {/* drop zone */}
        <section
          className={`mt-6 rounded-2xl border-2 border-dashed p-8 text-center transition-colors ${
            dragging ? "border-primary bg-primary/5" : "border-border bg-card"
          }`}
        >
          <Upload className="mx-auto size-8 text-muted-foreground" />
          <p className="mt-3 font-medium">Drop files or folders here</p>
          <p className="text-sm text-muted-foreground">Documents, notes, AI chat exports — indexed on-device.</p>
          <div className="mt-4 flex justify-center gap-2">
            <button
              onClick={addFiles}
              disabled={busy}
              className="inline-flex items-center gap-1.5 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground disabled:opacity-50"
            >
              {busy ? <Loader2 className="size-4 animate-spin" /> : <Upload className="size-4" />} Add files
            </button>
            <button
              onClick={addFolder}
              disabled={busy}
              className="inline-flex items-center gap-1.5 rounded-lg border border-border px-4 py-2 text-sm hover:bg-accent disabled:opacity-50"
            >
              <FolderOpen className="size-4" /> Add folder
            </button>
          </div>
        </section>

        {/* live ingest status */}
        {(ingesting || feed.length > 0) && (
          <section className="mt-6 rounded-2xl border border-border bg-card p-4">
            <div className="flex items-center justify-between text-sm">
              <span className="inline-flex items-center gap-1.5 font-medium">
                <Database className="size-4 text-violet-500" />
                {ingesting ? "Building your memory" : "Memory up to date"}
              </span>
              <span className="text-muted-foreground">
                {active.total > 0 ? (
                  <>
                    {active.done}/{active.total} learned
                    {active.skipped > 0 && <span className="ml-1 text-slate-400">· {active.skipped} skipped</span>}
                  </>
                ) : (
                  <>
                    {counts.sources} source{counts.sources === 1 ? "" : "s"} indexed
                    {active.skipped > 0 && <span className="ml-1 text-slate-400">· {active.skipped} skipped</span>}
                  </>
                )}
              </span>
            </div>
            <div className="mt-2 h-2 overflow-hidden rounded-full bg-muted">
              <div
                className="h-full rounded-full bg-gradient-to-r from-violet-500 via-blue-500 to-green-500 transition-all"
                style={{ width: `${pct}%` }}
              />
            </div>
            <ul className="mt-3 space-y-1 text-xs">
              {feed.map((l) => {
                const sl = stageLabel(l.stage, l.state);
                const dot =
                  l.state === "added"
                    ? "bg-green-500"
                    : l.state === "failed"
                      ? "bg-red-500"
                      : l.state === "skipped"
                        ? "bg-slate-300"
                        : "bg-blue-500 animate-pulse";
                return (
                  <li key={l.path} className="flex items-center gap-2 truncate">
                    <span className={`size-2 shrink-0 rounded-full ${dot}`} />
                    <span className="truncate text-foreground">{baseName(l.path)}</span>
                    <span className={`ml-auto shrink-0 font-medium ${sl.cls}`}>{sl.text}</span>
                  </li>
                );
              })}
            </ul>
          </section>
        )}

        {/* activity feed */}
        <section className="mt-6 rounded-2xl border border-border bg-card p-4">
          <div className="flex items-center justify-between mb-4">
            <h2 className="inline-flex items-center gap-1.5 font-medium">
              <Clock className="size-4 text-primary" /> Activity Feed
            </h2>
            <div className="flex items-center gap-2">
              <select
                value={activityFeedFilter}
                onChange={(e) => setActivityFeedFilter(e.target.value as any)}
                className="rounded-lg border border-border bg-background px-2 py-1 text-xs hover:bg-accent"
              >
                <option value="all">All Events</option>
                <option value="ingest">Ingest</option>
                <option value="ambient">Ambient</option>
                <option value="graph">Graph</option>
                <option value="errors">Errors</option>
              </select>
              <select
                value={activityTimeRange}
                onChange={(e) => setActivityTimeRange(e.target.value as any)}
                className="rounded-lg border border-border bg-background px-2 py-1 text-xs hover:bg-accent"
              >
                <option value="last_hour">Last Hour</option>
                <option value="last_day">Last Day</option>
                <option value="last_week">Last Week</option>
                <option value="all">All Time</option>
              </select>
            </div>
          </div>

          {activityFeedLoading ? (
            <div className="flex items-center justify-center py-8">
              <Loader2 className="size-6 animate-spin text-muted-foreground" />
            </div>
          ) : activityFeedError ? (
            <div className="text-center py-8">
              <p className="text-sm text-muted-foreground">{activityFeedError}</p>
              <button
                onClick={() => loadActivityFeed()}
                className="mt-2 text-sm text-primary hover:underline"
              >
                Retry
              </button>
            </div>
          ) : activityFeed ? (
            <>
              {activityFeed.now && (
              <div className="mb-4 rounded-lg bg-muted/50 p-3">
                <p className="text-xs font-medium text-muted-foreground mb-1">Now</p>
                <p className="text-sm">{activityFeed.now.title}</p>
              </div>
            )}

            <div className="space-y-3">
              {["ingest", "ambient", "graph", "errors"].map((sectionType) => {
                const sectionItems = activityFeed.items.filter(item => {
                  const itemType = getFeedItemType(item);
                  if (activityFeedFilter !== "all" && itemType !== activityFeedFilter) return false;
                  return itemType === sectionType;
                });

                if (sectionItems.length === 0) return null;

                const sectionId = `activity-${sectionType}`;
                const isCollapsed = collapsedSections[sectionId];

                return (
                  <div key={sectionId} className="rounded-lg border border-border bg-muted/30">
                    <button
                      onClick={() => toggleSection(sectionId)}
                      className="flex w-full items-center justify-between p-3 hover:bg-accent/40 transition-colors"
                    >
                      <span className="text-sm font-medium capitalize">{sectionType} ({sectionItems.length})</span>
                      <span className="text-muted-foreground">
                        {isCollapsed ? "▶" : "▼"}
                      </span>
                    </button>
                    {!isCollapsed && (
                      <div className="border-t border-border p-3 space-y-2">
                        {sectionItems.slice(0, 10).map((item, idx) => {
                          const itemId = isFeedItem(item) ? item.feed_id : `${item.proposal.proposal_id}-${idx}`;
                          const title = isFeedItem(item) ? item.title : item.proposal.title;
                          const body = isFeedItem(item) ? item.body : item.proposal.summary;
                          const refs = isFeedItem(item) ? item.refs : {};

                          return (
                            <div
                              key={`${itemId}-${idx}`}
                              className="flex items-start gap-2 text-xs hover:bg-accent/40 p-2 rounded cursor-pointer"
                              onClick={() => {
                                // Handle click to jump to source or graph node
                                if (refs?.source_id) {
                                  const source = sources.find(s => s.source_id === refs?.source_id);
                                  if (source?.path) {
                                    void handleReveal(source.path);
                                  }
                                } else if (refs?.node_id) {
                                  // Navigate to graph node
                                  setCurrentTab("graph");
                                } else if (refs?.path) {
                                  void handleReveal(refs.path);
                                }
                              }}
                            >
                              <span className="text-muted-foreground shrink-0">{formatFeedTime(item.ts)}</span>
                              <div className="min-w-0 flex-1">
                                <p className="font-medium truncate">{title}</p>
                                {body && <p className="text-muted-foreground truncate">{body}</p>}
                              </div>
                            </div>
                          );
                        })}
                        {sectionItems.length > 10 && (
                          <p className="text-xs text-muted-foreground text-center">
                            +{sectionItems.length - 10} more
                          </p>
                        )}
                      </div>
                    )}
                  </div>
                );
              })}

              {activityFeed.items.filter(item => {
                if (activityFeedFilter !== "all" && getFeedItemType(item) !== activityFeedFilter) return false;
                return getFeedItemType(item) === "other";
              }).length > 0 && (
                <div className="rounded-lg border border-border bg-muted/30">
                  <button
                    onClick={() => toggleSection("activity-other")}
                    className="flex w-full items-center justify-between p-3 hover:bg-accent/40 transition-colors"
                  >
                    <span className="text-sm font-medium">Other ({activityFeed.items.filter(item => getFeedItemType(item) === "other").length})</span>
                    <span className="text-muted-foreground">
                      {collapsedSections["activity-other"] ? "▶" : "▼"}
                    </span>
                  </button>
                  {!collapsedSections["activity-other"] && (
                    <div className="border-t border-border p-3 space-y-2">
                      {activityFeed.items.filter(item => getFeedItemType(item) === "other").slice(0, 10).map((item, idx) => {
                        const itemId = isFeedItem(item) ? item.feed_id : `${item.proposal.proposal_id}-${idx}`;
                        const title = isFeedItem(item) ? item.title : item.proposal.title;
                        const body = isFeedItem(item) ? item.body : item.proposal.summary;

                        return (
                          <div
                            key={`${itemId}-${idx}`}
                            className="flex items-start gap-2 text-xs hover:bg-accent/40 p-2 rounded"
                          >
                            <span className="text-muted-foreground shrink-0">{formatFeedTime(item.ts)}</span>
                            <div className="min-w-0 flex-1">
                              <p className="font-medium truncate">{title}</p>
                              {body && <p className="text-muted-foreground truncate">{body}</p>}
                            </div>
                          </div>
                        );
                      })}
                    </div>
                  )}
                </div>
              )}
            </div>

            {activityFeed.items.length === 0 && (
              <p className="text-sm text-muted-foreground text-center py-4">No recent activity</p>
            )}
            </>
          ) : null}
        </section>

        {/* identity mirror */}
        <IdentityMirror sidecarReady={sidecar?.state === "ready"} />

        {/* connector checklist */}
        <section className="mt-6 rounded-2xl border border-border bg-card p-4">
          <div className="flex items-center justify-between">
            <h2 className="inline-flex items-center gap-1.5 font-medium">
              <Plug className="size-4 text-primary" /> Set up Minion
            </h2>
            <span className="text-sm text-muted-foreground">
              {doneCount}/{checklist.length} done
            </span>
          </div>
          <ul className="mt-3 space-y-2">
            {checklist.map((item) => (
              <li key={item.id} className="flex items-center gap-3 rounded-lg p-2 hover:bg-accent/40">
                {item.done ? (
                  <CheckCircle2 className="size-5 shrink-0 text-primary" />
                ) : (
                  <Circle className="size-5 shrink-0 text-muted-foreground" />
                )}
                <div className="min-w-0 flex-1">
                  <p className="text-sm font-medium">{item.label}</p>
                  <p
                    className={`text-xs ${
                      item.id === "claude" && claudeMsg && !claudeStatus?.connected
                        ? "text-amber-700 dark:text-amber-400"
                        : item.id === "claude" && claudeStatus?.connected
                          ? "text-emerald-700 dark:text-emerald-400"
                          : item.id === "cursor" && cursorMsg && !cursorStatus?.connected
                            ? "text-amber-700 dark:text-amber-400"
                            : item.id === "cursor" && cursorStatus?.connected
                              ? "text-emerald-700 dark:text-emerald-400"
                              : "truncate text-muted-foreground"
                    }`}
                  >
                    {item.detail}
                  </p>
                </div>
                {item.action && (
                  <button
                    onClick={() => void item.action!.run()}
                    disabled={
                      (item.id === "claude" && Boolean(claudeStatus?.connected)) ||
                      (item.id === "cursor" && Boolean(cursorStatus?.connected))
                    }
                    className="shrink-0 rounded-lg border border-border px-3 py-1.5 text-xs hover:bg-accent disabled:cursor-default disabled:opacity-60"
                  >
                    {item.action.label}
                  </button>
                )}
              </li>
            ))}
          </ul>
          {graphMsg && (
            <p className="mt-2 inline-flex items-center gap-1.5 text-xs text-muted-foreground">
              <Network className="size-3.5" /> {graphMsg}
            </p>
          )}
          {reindexMsg && <p className="mt-1 text-xs text-muted-foreground">{reindexMsg}</p>}
        </section>

        {/* sources library (compact) */}
        <section className="mt-6">
          <div className="mb-2 flex items-center justify-between">
            <h2 className="text-sm font-medium text-muted-foreground">Recent sources</h2>
            <div className="flex gap-2">
              <select
                value={sourceTypeFilter}
                onChange={(e) => setSourceTypeFilter(e.target.value as any)}
                className="text-xs rounded border border-border bg-background px-2 py-1"
              >
                <option value="all">All types</option>
                <option value="file">Files</option>
                <option value="chat_export">Chat exports</option>
                <option value="external">External</option>
                <option value="ambient">Ambient</option>
              </select>
              <select
                value={timeRangeFilter}
                onChange={(e) => setTimeRangeFilter(e.target.value as any)}
                className="text-xs rounded border border-border bg-background px-2 py-1"
              >
                <option value="all">All time</option>
                <option value="last_hour">Last hour</option>
                <option value="last_day">Last day</option>
                <option value="last_week">Last week</option>
              </select>
            </div>
          </div>
          {revealError && (
            <p className="mb-2 text-xs text-amber-700 dark:text-amber-400">Reveal failed: {revealError}</p>
          )}
          {sources.length === 0 ? (
            <p className="text-sm text-muted-foreground">No sources match the current filters.</p>
          ) : (
            <ul className="divide-y divide-border overflow-hidden rounded-2xl border border-border bg-card">
              {sources.slice(0, 12).map((s) => (
                <li key={s.source_id} className="flex items-center gap-3 px-4 py-2 text-sm">
                  {looksLikeExport(s) && (
                    <span className="shrink-0 rounded-full bg-purple-100 px-2 py-0.5 text-xs font-medium text-purple-700 dark:bg-purple-900/30 dark:text-purple-300">
                      AI Export
                    </span>
                  )}
                  <div className="flex flex-1 flex-col">
                    <span className="truncate">{baseName(s.path)}</span>
                    {looksLikeExport(s) && s.meta && (() => {
                      const indexedConversations = s.meta.indexed_conversations as number | undefined;
                      const conversationIds = s.meta.conversation_ids as string[] | undefined;
                      return (
                        <span className="truncate text-xs text-muted-foreground">
                          {indexedConversations && `${indexedConversations} conversations`}
                          {indexedConversations && conversationIds && ' · '}
                          {conversationIds && Array.isArray(conversationIds) && `${conversationIds.length} unique IDs`}
                        </span>
                      );
                    })()}
                  </div>
                  <span className="ml-auto shrink-0 text-xs text-muted-foreground">{s.kind}</span>
                  {!s.kind.includes('ambient') && !s.path.startsWith('ambient/') && (
                    <button
                      onClick={() => void handleReveal(s.path)}
                      className="shrink-0 text-xs text-primary hover:underline"
                    >
                      reveal
                    </button>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>
          </>
        )}

        {currentTab === "graph" && (
          <section className="mt-6 w-full">
            <h2 className="text-lg font-medium mb-4">Knowledge Graph</h2>
            <GraphVisualization />
          </section>
        )}

        {currentTab === "settings" && (
          <SettingsView
            consentPolicy={consentPolicy}
            setConsentPolicy={setConsentPolicy}
            consentError={consentError}
            auditLog={auditLog}
            auditFilter={auditFilter}
            setAuditFilter={setAuditFilter}
            loadAuditLog={loadAuditLog}
            formatFeedTime={formatFeedTime}
          />
        )}
      </div>
    </div>
  );
}

export default App;
