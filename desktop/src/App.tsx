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
} from "lucide-react";
import { open } from "@tauri-apps/plugin-dialog";

import {
  apiErrorDetail,
  connectClaudeDesktop,
  fetchClaudeDesktopStatus,
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
  type Active,
  type ClaudeDesktopStatus,
  type ConnState,
  type EventMsg,
  type GraphStats,
  type SidecarStatus,
  type Source,
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

export function App() {
  const [name, setName] = useState(storedName());
  const [nameDraft, setNameDraft] = useState("");
  const [sources, setSources] = useState<Source[]>([]);
  const [counts, setCounts] = useState({ sources: 0, chunks: 0 });
  const [active, setActive] = useState<Active>(EMPTY_ACTIVE);
  const [feed, setFeed] = useState<FeedLine[]>([]);
  const [conn, setConn] = useState<ConnState>("connecting");
  const [sidecar, setSidecar] = useState<SidecarStatus | null>(null);
  const [dragging, setDragging] = useState(false);
  const [busy, setBusy] = useState(false);
  const [graphMsg, setGraphMsg] = useState("");
  const [graphBusy, setGraphBusy] = useState(false);
  const [graphStats, setGraphStats] = useState<GraphStats | null>(null);
  const [reindexMsg, setReindexMsg] = useState("");
  const [claudeStatus, setClaudeStatus] = useState<ClaudeDesktopStatus | null>(null);
  const [claudeMsg, setClaudeMsg] = useState("");
  const dropRef = useRef<(files: FileList | null) => void>(() => {});

  const load = useCallback(async () => {
    try {
      const [st, srcs, gs, claude] = await Promise.all([
        fetchStatus().catch(() => null),
        fetchSources({ limit: 500 }).then((r) => r.sources).catch(() => [] as Source[]),
        fetchGraphStats().catch(() => null),
        fetchClaudeDesktopStatus().catch(() => null),
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
    } catch {
      /* ignore */
    }
  }, []);

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

  function pushFeed(path: string, stage: string, state: FeedLine["state"]) {
    setFeed((prev) => {
      const next = prev.filter((l) => l.path !== path);
      next.unshift({ path, stage, state });
      return next.slice(0, 12);
    });
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
          <button
            onClick={runReindex}
            title="Re-embed corpus under the current model"
            className="inline-flex items-center gap-1.5 rounded-lg border border-border px-3 py-1.5 text-sm hover:bg-accent"
          >
            <RefreshCw className="size-4" /> Reindex
          </button>
        </header>

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
                          : "truncate text-muted-foreground"
                    }`}
                  >
                    {item.detail}
                  </p>
                </div>
                {item.action && (
                  <button
                    onClick={() => void item.action!.run()}
                    disabled={item.id === "claude" && Boolean(claudeStatus?.connected)}
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
        {sources.length > 0 && (
          <section className="mt-6">
            <h2 className="mb-2 text-sm font-medium text-muted-foreground">Recent sources</h2>
            <ul className="divide-y divide-border overflow-hidden rounded-2xl border border-border bg-card">
              {sources.slice(0, 12).map((s) => (
                <li key={s.source_id} className="flex items-center gap-3 px-4 py-2 text-sm">
                  <span className="truncate">{baseName(s.path)}</span>
                  <span className="ml-auto shrink-0 text-xs text-muted-foreground">{s.kind}</span>
                  <button
                    onClick={() => void revealInFinder(s.path)}
                    className="shrink-0 text-xs text-primary hover:underline"
                  >
                    reveal
                  </button>
                </li>
              ))}
            </ul>
          </section>
        )}
      </div>
    </div>
  );
}

export default App;
