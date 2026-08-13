import { useCallback, useEffect, useState } from "react";
import { Database, Loader2, RefreshCw } from "lucide-react";
import {
  apiErrorDetail,
  postMaintenanceChunkDeduplicate,
  postMaintenanceRunCompaction,
  postMaintenanceStorageReport,
  postMaintenanceStorageTierPromoteStale,
  type StorageMaintenanceReport,
} from "../lib/api";

type SyncJobRun = {
  run_id: string;
  source_key: string;
  status: string;
  started_at: number;
  finished_at: number | null;
  items_count: number;
  error: string | null;
};

function formatTime(ts: number): string {
  return new Date(ts * 1000).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function StorageLifecyclePanel() {
  const [report, setReport] = useState<(StorageMaintenanceReport & { sync_job_runs?: SyncJobRun[] }) | null>(
    null,
  );
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  const loadReport = useCallback(async () => {
    setLoading(true);
    try {
      const data = await postMaintenanceStorageReport();
      setReport(data);
    } catch (e) {
      setMessage(apiErrorDetail(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void loadReport();
  }, [loadReport]);

  async function runAction(label: string, fn: () => Promise<unknown>) {
    setBusy(label);
    setMessage(null);
    try {
      const result = await fn();
      const summary = JSON.stringify(result);
      setMessage(`${label}: ${summary.slice(0, 240)}${summary.length > 240 ? "…" : ""}`);
      await loadReport();
    } catch (e) {
      setMessage(apiErrorDetail(e));
    } finally {
      setBusy(null);
    }
  }

  const tiers = report?.chunk_storage_tiers ?? {};
  const lastRun = report?.sync_job_runs?.[0];

  return (
    <section className="rounded-2xl border border-border bg-card p-4">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h2 className="inline-flex items-center gap-1.5 font-medium">
          <Database className="size-4 text-primary" /> Storage lifecycle
        </h2>
        <button
          type="button"
          onClick={() => void loadReport()}
          disabled={loading}
          className="rounded-lg border border-border px-2 py-1 text-xs hover:bg-accent disabled:opacity-50"
        >
          {loading ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
        </button>
      </div>

      <p className="text-xs text-muted-foreground">
        Hot/warm/cold tiers track corpus freshness. Dedupe and compaction are safe to preview first.
      </p>

      {loading && !report ? (
        <div className="mt-3 flex items-center gap-2 text-sm text-muted-foreground">
          <Loader2 className="size-4 animate-spin" /> Loading storage report…
        </div>
      ) : (
        <div className="mt-3 space-y-3 text-sm">
          <div className="grid grid-cols-3 gap-2">
            {(["hot", "warm", "cold"] as const).map((tier) => (
              <div key={tier} className="rounded-lg bg-muted/40 px-3 py-2 text-center">
                <p className="text-xs uppercase tracking-wide text-muted-foreground">{tier}</p>
                <p className="text-lg font-medium tabular-nums">{tiers[tier] ?? 0}</p>
              </div>
            ))}
          </div>

          {report?.ambient_event_count != null && (
            <p className="text-xs text-muted-foreground">
              Ambient events: {report.ambient_event_count.toLocaleString()}
              {report.sqlite?.freelist_ratio != null && (
                <> · SQLite freelist ~{(report.sqlite.freelist_ratio * 100).toFixed(1)}%</>
              )}
            </p>
          )}

          {lastRun && (
            <p className="text-xs text-muted-foreground">
              Last sync job: {lastRun.source_key} · {lastRun.status} · {formatTime(lastRun.started_at)}
            </p>
          )}

          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              disabled={Boolean(busy)}
              onClick={() =>
                void runAction("Dedupe", () =>
                  postMaintenanceChunkDeduplicate({ dry_run: false, min_chunk_age_days: 7 }),
                )
              }
              className="rounded-lg border border-border px-3 py-1.5 text-xs hover:bg-accent disabled:opacity-50"
            >
              Run dedupe
            </button>
            <button
              type="button"
              disabled={Boolean(busy)}
              onClick={() =>
                void runAction("Promote stale preview", () =>
                  postMaintenanceStorageTierPromoteStale({
                    dry_run: true,
                    min_source_age_days: 120,
                    from_tier: "hot",
                    to_tier: "warm",
                  }),
                )
              }
              className="rounded-lg border border-border px-3 py-1.5 text-xs hover:bg-accent disabled:opacity-50"
            >
              Preview promote stale
            </button>
            <button
              type="button"
              disabled={Boolean(busy)}
              onClick={() => void runAction("Run compaction", () => postMaintenanceRunCompaction())}
              className="rounded-lg border border-border px-3 py-1.5 text-xs hover:bg-accent disabled:opacity-50"
            >
              Run compaction
            </button>
          </div>

          <p className="text-xs text-muted-foreground">
            See{" "}
            <a
              href="https://github.com/The-Good-Project-Team/Minion/blob/main/docs/ROADMAP.md"
              className="text-primary hover:underline"
              target="_blank"
              rel="noreferrer"
            >
              storage tiers in ROADMAP.md
            </a>
            .
          </p>
        </div>
      )}

      {message && <p className="mt-2 text-xs text-muted-foreground break-words">{message}</p>}
    </section>
  );
}
