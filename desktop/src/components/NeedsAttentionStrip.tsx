import { useCallback, useEffect, useMemo, useState } from "react";
import { AlertTriangle, Loader2, Settings, X } from "lucide-react";
import {
  fetchHealth,
  fetchStatus,
  resolveHealthIssue,
  type SystemIssue,
} from "../lib/api";

const SNOOZE_KEY = "minion:health_snooze_until";
const DISMISS_KEY = "minion:health_dismissed";

type NeedsAttentionStripProps = {
  sidecarReady?: boolean;
  onOpenSettings?: () => void;
};

function loadDismissed(): Set<string> {
  try {
    const raw = localStorage.getItem(DISMISS_KEY);
    if (!raw) return new Set();
    const parsed = JSON.parse(raw) as unknown;
    return new Set(Array.isArray(parsed) ? parsed.filter((x) => typeof x === "string") : []);
  } catch {
    return new Set();
  }
}

function saveDismissed(ids: Set<string>): void {
  try {
    localStorage.setItem(DISMISS_KEY, JSON.stringify([...ids]));
  } catch {
    /* ignore */
  }
}

function snoozedUntil(): number {
  try {
    const raw = localStorage.getItem(SNOOZE_KEY);
    return raw ? Number(raw) : 0;
  } catch {
    return 0;
  }
}

export function NeedsAttentionStrip({ sidecarReady = false, onOpenSettings }: NeedsAttentionStripProps) {
  const [issues, setIssues] = useState<SystemIssue[]>([]);
  const [dbOk, setDbOk] = useState(true);
  const [watcherRunning, setWatcherRunning] = useState(true);
  const [loading, setLoading] = useState(false);
  const [resolvingId, setResolvingId] = useState<string | null>(null);
  const [dismissed, setDismissed] = useState<Set<string>>(() => loadDismissed());
  const [hidden, setHidden] = useState(() => Date.now() < snoozedUntil());

  const loadHealth = useCallback(async () => {
    if (!sidecarReady) return;
    setLoading(true);
    try {
      const [health, status] = await Promise.all([fetchHealth(), fetchStatus()]);
      setIssues(health.open_issues ?? []);
      setDbOk(Boolean(status.database?.ok ?? health.database?.ok));
      setWatcherRunning(Boolean(status.watcher?.running ?? health.watcher?.running));
    } catch (e) {
      console.error("Failed to load health:", e);
    } finally {
      setLoading(false);
    }
  }, [sidecarReady]);

  useEffect(() => {
    void loadHealth();
    const t = window.setInterval(() => void loadHealth(), 60_000);
    return () => window.clearInterval(t);
  }, [loadHealth]);

  const visibleIssues = useMemo(
    () => issues.filter((issue) => !dismissed.has(issue.issue_id)),
    [issues, dismissed],
  );

  const showWatcherWarning = sidecarReady && !watcherRunning;
  const showDbWarning = sidecarReady && !dbOk;
  const hasBanner = !hidden && (showWatcherWarning || showDbWarning || visibleIssues.length > 0);

  if (!hasBanner) return null;

  async function handleResolve(issue: SystemIssue) {
    setResolvingId(issue.issue_id);
    try {
      await resolveHealthIssue(issue.issue_id);
      setIssues((prev) => prev.filter((i) => i.issue_id !== issue.issue_id));
    } catch (e) {
      console.error("Failed to resolve issue:", e);
    } finally {
      setResolvingId(null);
    }
  }

  function dismissIssue(issueId: string) {
    const next = new Set(dismissed);
    next.add(issueId);
    setDismissed(next);
    saveDismissed(next);
  }

  function snoozeBanner(hours = 4) {
    const until = Date.now() + hours * 3600_000;
    localStorage.setItem(SNOOZE_KEY, String(until));
    setHidden(true);
  }

  return (
    <section className="rounded-xl border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-start gap-2">
          {loading ? (
            <Loader2 className="mt-0.5 size-4 shrink-0 animate-spin text-amber-700" />
          ) : (
            <AlertTriangle className="mt-0.5 size-4 shrink-0 text-amber-700 dark:text-amber-400" />
          )}
          <div className="min-w-0 space-y-2">
            <p className="font-medium text-amber-900 dark:text-amber-200">Needs attention</p>
            {showDbWarning && (
              <p className="text-xs text-amber-800 dark:text-amber-300">
                Database check failed — indexing or search may be unreliable.
              </p>
            )}
            {showWatcherWarning && (
              <p className="text-xs text-amber-800 dark:text-amber-300">
                Inbox watcher is not running — new files may not ingest automatically.
              </p>
            )}
            {visibleIssues.map((issue) => (
              <div key={issue.issue_id} className="rounded-lg bg-background/60 px-2 py-1.5 text-xs">
                <p className="text-foreground">{issue.body_md}</p>
                <div className="mt-1 flex flex-wrap gap-2">
                  <button
                    type="button"
                    onClick={() => void handleResolve(issue)}
                    disabled={resolvingId === issue.issue_id}
                    className="text-primary hover:underline disabled:opacity-50"
                  >
                    {resolvingId === issue.issue_id ? "Resolving…" : "Resolve"}
                  </button>
                  <button
                    type="button"
                    onClick={() => dismissIssue(issue.issue_id)}
                    className="text-muted-foreground hover:underline"
                  >
                    Dismiss
                  </button>
                </div>
              </div>
            ))}
          </div>
        </div>
        <div className="flex shrink-0 items-center gap-1">
          {onOpenSettings && (
            <button
              type="button"
              onClick={onOpenSettings}
              className="rounded-lg p-1 hover:bg-amber-500/20"
              title="Open Settings"
            >
              <Settings className="size-4" />
            </button>
          )}
          <button
            type="button"
            onClick={() => snoozeBanner()}
            className="rounded-lg px-2 py-1 text-xs text-muted-foreground hover:bg-amber-500/20"
          >
            Snooze 4h
          </button>
          <button
            type="button"
            onClick={() => setHidden(true)}
            className="rounded-lg p-1 hover:bg-amber-500/20"
            title="Hide until next refresh"
          >
            <X className="size-4" />
          </button>
        </div>
      </div>
    </section>
  );
}
