import { useCallback, useEffect, useState } from "react";
import { GitFork, Loader2 } from "lucide-react";
import {
  apiErrorDetail,
  fetchGraphCandidates,
  resolveGraphCandidate,
  type GraphCandidate,
} from "../lib/api";
import { formatFeedTime } from "../lib/feedUtils";

type GraphCandidateInboxProps = {
  compact?: boolean;
  onNavigateGraph?: () => void;
  onResolved?: () => void;
};

function candidateSummary(candidate: GraphCandidate): string {
  const body = candidate.body_md?.trim();
  if (body) return body.slice(0, 200);
  const payload = candidate.payload ?? {};
  const bits = [
    payload.email,
    payload.handle,
    payload.app,
    payload.window,
    payload.resource_id,
  ]
    .filter(Boolean)
    .map(String);
  return bits.join(" · ").slice(0, 200);
}

export function GraphCandidateInbox({
  compact = false,
  onNavigateGraph,
  onResolved,
}: GraphCandidateInboxProps) {
  const [candidates, setCandidates] = useState<GraphCandidate[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetchGraphCandidates("open");
      setCandidates(res.candidates);
    } catch (e) {
      setError(apiErrorDetail(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function resolve(
    candidateId: string,
    status: "approved" | "rejected" | "dismissed" | "merged",
  ) {
    setBusyId(candidateId);
    try {
      await resolveGraphCandidate(candidateId, status);
      setCandidates((prev) => prev.filter((c) => c.candidate_id !== candidateId));
      onResolved?.();
    } catch (e) {
      setError(apiErrorDetail(e));
    } finally {
      setBusyId(null);
    }
  }

  if (loading && candidates.length === 0) {
    if (compact) return null;
    return (
      <div className="flex items-center justify-center py-6">
        <Loader2 className="size-5 animate-spin text-muted-foreground" />
      </div>
    );
  }

  if (candidates.length === 0) {
    if (compact) return null;
    return (
      <section className="rounded-2xl border border-border bg-card p-4">
        <h3 className="inline-flex items-center gap-1.5 font-medium">
          <GitFork className="size-4 text-violet-500" /> Graph questions
        </h3>
        <p className="mt-2 text-sm text-muted-foreground">No open merge or fill questions right now.</p>
      </section>
    );
  }

  if (compact) {
    return (
      <div className="rounded-lg border border-violet-500/30 bg-violet-50/60 p-3 text-sm dark:bg-violet-950/20">
        <p className="font-medium">
          {candidates.length} question{candidates.length === 1 ? "" : "s"} about your graph
        </p>
        <p className="mt-1 text-xs text-muted-foreground">
          Review merge candidates and screen-memory suggestions.
        </p>
        {onNavigateGraph && (
          <button
            type="button"
            onClick={onNavigateGraph}
            className="mt-2 text-xs font-medium text-primary hover:underline"
          >
            Review in Graph →
          </button>
        )}
      </div>
    );
  }

  return (
    <section className="rounded-2xl border border-border bg-card p-4">
      <div className="mb-3 flex items-center justify-between gap-2">
        <h3 className="inline-flex items-center gap-1.5 font-medium">
          <GitFork className="size-4 text-violet-500" /> Graph questions ({candidates.length})
        </h3>
        <button
          type="button"
          onClick={() => void load()}
          className="text-xs text-muted-foreground hover:text-foreground"
        >
          Refresh
        </button>
      </div>
      {error && <p className="mb-2 text-sm text-red-600">{error}</p>}
      <ul className="space-y-3">
        {candidates.map((candidate) => {
          const busy = busyId === candidate.candidate_id;
          return (
            <li
              key={candidate.candidate_id}
              className="rounded-lg border border-border bg-muted/30 p-3"
            >
              <div className="flex flex-wrap items-start justify-between gap-2">
                <div className="min-w-0 flex-1">
                  <p className="font-medium">{candidate.title || "Graph candidate"}</p>
                  <p className="mt-0.5 text-xs text-muted-foreground">
                    {candidate.candidate_type.replace(/_/g, " ")} · {formatFeedTime(candidate.updated_at)}
                    {candidate.source ? ` · ${candidate.source}` : ""}
                  </p>
                  {candidateSummary(candidate) && (
                    <p className="mt-2 text-sm text-muted-foreground">{candidateSummary(candidate)}</p>
                  )}
                </div>
                <div className="flex shrink-0 flex-wrap gap-1.5">
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void resolve(candidate.candidate_id, "merged")}
                    className="rounded-lg bg-primary px-2.5 py-1 text-xs font-medium text-primary-foreground disabled:opacity-50"
                  >
                    {busy ? <Loader2 className="size-3 animate-spin" /> : "Accept"}
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void resolve(candidate.candidate_id, "dismissed")}
                    className="rounded-lg border border-border px-2.5 py-1 text-xs hover:bg-accent disabled:opacity-50"
                  >
                    Defer
                  </button>
                  <button
                    type="button"
                    disabled={busy}
                    onClick={() => void resolve(candidate.candidate_id, "rejected")}
                    className="rounded-lg border border-border px-2.5 py-1 text-xs text-muted-foreground hover:bg-accent disabled:opacity-50"
                  >
                    Reject
                  </button>
                </div>
              </div>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
