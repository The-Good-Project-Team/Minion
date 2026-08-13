import { useCallback, useEffect, useMemo, useState } from "react";
import { History, Loader2, RefreshCw, RotateCcw, Eye, EyeOff } from "lucide-react";
import {
  fetchChunk,
  fetchIdentityClaimEdges,
  fetchIdentityHistory,
  fetchIdentityMirror,
  revertIdentityClaim,
  type IdentityClaim,
  type IdentityEdge,
  type IdentityMirrorResponse,
} from "../lib/api";

interface IdentityMirrorProps {
  sidecarReady?: boolean;
  onReveal?: (path: string) => void | Promise<void>;
}

type HistoryFilter = "superseded" | "rejected" | "all";

function formatClaimTime(ts: number): string {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

export function IdentityMirror({ sidecarReady = false, onReveal }: IdentityMirrorProps) {
  const [mirror, setMirror] = useState<IdentityMirrorResponse | null>(null);
  const [history, setHistory] = useState<IdentityClaim[]>([]);
  const [historyFilter, setHistoryFilter] = useState<HistoryFilter>("superseded");
  const [loading, setLoading] = useState(true);
  const [historyLoading, setHistoryLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [showEvidence, setShowEvidence] = useState(true);
  const [expandedClaimId, setExpandedClaimId] = useState<string | null>(null);
  const [claimEdges, setClaimEdges] = useState<Record<string, IdentityEdge[]>>({});
  const [revertingId, setRevertingId] = useState<string | null>(null);
  const [actionMsg, setActionMsg] = useState<string | null>(null);

  const loadMirror = useCallback(async () => {
    if (!mirror) setLoading(true);
    setError(null);
    try {
      const data = await fetchIdentityMirror({ limit_history: 60, include_evidence: showEvidence });
      setMirror(data);
    } catch (e) {
      console.error("Failed to load identity mirror:", e);
      if (!mirror) setError("Failed to load identity mirror");
    } finally {
      setLoading(false);
    }
  }, [mirror, showEvidence]);

  const loadHistory = useCallback(async () => {
    setHistoryLoading(true);
    try {
      const params =
        historyFilter === "all"
          ? { limit: 80 }
          : { status: historyFilter, limit: 80 };
      const data = await fetchIdentityHistory(params);
      setHistory(data.history);
    } catch (e) {
      console.error("Failed to load identity history:", e);
    } finally {
      setHistoryLoading(false);
    }
  }, [historyFilter]);

  useEffect(() => {
    if (sidecarReady) void loadMirror();
  }, [sidecarReady, loadMirror]);

  useEffect(() => {
    if (sidecarReady) void loadHistory();
  }, [sidecarReady, loadHistory]);

  const revertableClaims = useMemo(
    () => history.filter((c) => c.status === "superseded" && c.superseded_by),
    [history],
  );

  async function loadEdgesForClaim(claimId: string) {
    if (claimEdges[claimId]) return;
    try {
      const data = await fetchIdentityClaimEdges(claimId);
      setClaimEdges((prev) => ({ ...prev, [claimId]: data.edges }));
    } catch (e) {
      console.error("Failed to load claim edges:", e);
    }
  }

  async function toggleClaimExpanded(claimId: string) {
    if (expandedClaimId === claimId) {
      setExpandedClaimId(null);
      return;
    }
    setExpandedClaimId(claimId);
    if (showEvidence) await loadEdgesForClaim(claimId);
  }

  async function openEvidence(edge: IdentityEdge) {
    if (!onReveal) return;
    try {
      if (edge.chunk_id) {
        const chunk = await fetchChunk(edge.chunk_id, 200);
        if (chunk.path) {
          await onReveal(chunk.path);
          return;
        }
      }
    } catch (e) {
      console.error("Failed to open evidence:", e);
    }
  }

  async function handleRevert(claim: IdentityClaim) {
    if (!claim.superseded_by || revertingId) return;
    setRevertingId(claim.claim_id);
    setActionMsg(null);
    try {
      await revertIdentityClaim({ claim_id: claim.claim_id });
      setActionMsg("Claim restored to active.");
      await Promise.all([loadMirror(), loadHistory()]);
    } catch (e) {
      console.error("Failed to revert claim:", e);
      setActionMsg("Could not revert this claim.");
    } finally {
      setRevertingId(null);
    }
  }

  const renderMarkdown = (markdown: string) => {
    const lines = markdown.split("\n");
    return lines.map((line, idx) => {
      if (line.startsWith("## ")) {
        return <h2 key={idx} className="text-xl font-bold mt-6 mb-3">{line.replace("## ", "")}</h2>;
      }
      if (line.startsWith("### ")) {
        return <h3 key={idx} className="text-lg font-semibold mt-4 mb-2">{line.replace("### ", "")}</h3>;
      }
      if (line.startsWith("#### ")) {
        return <h4 key={idx} className="text-base font-medium mt-3 mb-2">{line.replace("#### ", "")}</h4>;
      }
      if (line.startsWith("- ")) {
        return <li key={idx} className="ml-4 mb-1">{line.replace("- ", "")}</li>;
      }
      if (line.startsWith("  - ")) {
        return <li key={idx} className="ml-8 mb-1 text-sm text-muted-foreground">{line.replace("  - ", "")}</li>;
      }
      if (line.startsWith("**") && line.endsWith("**")) {
        return <p key={idx} className="font-semibold mb-2">{line.slice(2, -2)}</p>;
      }
      if (line.startsWith("_") && line.endsWith("_")) {
        return <p key={idx} className="italic text-muted-foreground mb-2">{line.slice(1, -1)}</p>;
      }
      if (line.trim() === "") {
        return <br key={idx} />;
      }
      return <p key={idx} className="mb-2">{line}</p>;
    });
  };

  return (
    <div className="rounded-2xl border border-border bg-card p-6">
      <div className="flex items-center justify-between mb-4">
        <h2 className="inline-flex items-center gap-1.5 font-medium">
          <RefreshCw className="size-4 text-primary" /> Identity Mirror
        </h2>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={() => setShowEvidence(!showEvidence)}
            className="rounded-lg border border-border px-2 py-1 text-xs hover:bg-accent"
            title={showEvidence ? "Hide evidence" : "Show evidence"}
          >
            {showEvidence ? <Eye className="size-4" /> : <EyeOff className="size-4" />}
          </button>
          <button
            type="button"
            onClick={() => void Promise.all([loadMirror(), loadHistory()])}
            disabled={loading}
            className="rounded-lg border border-border px-2 py-1 text-xs hover:bg-accent disabled:opacity-50"
          >
            {loading ? <Loader2 className="size-4 animate-spin" /> : <RefreshCw className="size-4" />}
          </button>
        </div>
      </div>

      {actionMsg && (
        <p className="mb-3 text-xs text-muted-foreground">{actionMsg}</p>
      )}

      {loading && !mirror ? (
        <div className="flex items-center justify-center py-8">
          <Loader2 className="size-6 animate-spin text-muted-foreground" />
        </div>
      ) : error ? (
        <div className="text-center py-8">
          <p className="text-sm text-muted-foreground">{error}</p>
          <button type="button" onClick={() => void loadMirror()} className="mt-2 text-sm text-primary hover:underline">
            Retry
          </button>
        </div>
      ) : mirror ? (
        <div className="space-y-4">
          <div className="prose prose-sm dark:prose-invert max-w-none">
            {renderMarkdown(mirror.markdown)}
          </div>

          <div className="mt-6 pt-4 border-t border-border">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <h3 className="inline-flex items-center gap-1.5 text-sm font-medium">
                <History className="size-4 text-primary" />
                Revision timeline
                {history.length > 0 && <span className="text-muted-foreground">({history.length})</span>}
              </h3>
              <select
                value={historyFilter}
                onChange={(e) => setHistoryFilter(e.target.value as HistoryFilter)}
                className="rounded-lg border border-border bg-background px-2 py-1 text-xs hover:bg-accent"
              >
                <option value="superseded">Superseded</option>
                <option value="rejected">Rejected</option>
                <option value="all">All revisions</option>
              </select>
            </div>

            {historyLoading && history.length === 0 ? (
              <div className="flex items-center gap-2 py-4 text-sm text-muted-foreground">
                <Loader2 className="size-4 animate-spin" /> Loading history…
              </div>
            ) : history.length === 0 ? (
              <p className="py-2 text-sm text-muted-foreground">No revision history for this filter.</p>
            ) : (
              <div className="space-y-2 max-h-80 overflow-y-auto">
                {history.map((claim) => {
                  const expanded = expandedClaimId === claim.claim_id;
                  const edges = claimEdges[claim.claim_id] ?? [];
                  const canRevert = claim.status === "superseded" && Boolean(claim.superseded_by);
                  return (
                    <div key={claim.claim_id} className="rounded-lg border border-border bg-muted/20 p-3 text-xs">
                      <div className="flex items-start justify-between gap-2">
                        <button
                          type="button"
                          onClick={() => void toggleClaimExpanded(claim.claim_id)}
                          className="min-w-0 flex-1 text-left"
                        >
                          <div className="flex flex-wrap items-center gap-2 mb-1">
                            <span className="font-medium capitalize">{claim.kind}</span>
                            <span className="rounded bg-muted px-1.5 py-0.5 text-muted-foreground">{claim.status}</span>
                            <span className="text-muted-foreground">{formatClaimTime(claim.updated_at)}</span>
                          </div>
                          <p className="text-foreground line-clamp-2">{claim.text}</p>
                          {claim.superseded_by && (
                            <p className="mt-1 text-muted-foreground">Superseded by {claim.superseded_by.slice(0, 8)}…</p>
                          )}
                        </button>
                        {canRevert && (
                          <button
                            type="button"
                            onClick={() => void handleRevert(claim)}
                            disabled={revertingId === claim.claim_id}
                            className="shrink-0 inline-flex items-center gap-1 rounded-lg border border-border px-2 py-1 hover:bg-accent disabled:opacity-50"
                            title="Restore this claim as active"
                          >
                            {revertingId === claim.claim_id ? (
                              <Loader2 className="size-3 animate-spin" />
                            ) : (
                              <RotateCcw className="size-3" />
                            )}
                            Revert
                          </button>
                        )}
                      </div>

                      {expanded && showEvidence && (
                        <div className="mt-2 border-t border-border pt-2">
                          {edges.length === 0 ? (
                            <p className="text-muted-foreground">No linked evidence chunks.</p>
                          ) : (
                            <ul className="space-y-1">
                              {edges.map((edge) => (
                                <li key={edge.edge_id} className="flex flex-wrap items-center gap-2">
                                  {edge.chunk_id && (
                                    <span className="font-mono text-[10px] text-muted-foreground">{edge.chunk_id.slice(0, 10)}…</span>
                                  )}
                                  {edge.rationale && <span className="text-muted-foreground">{edge.rationale}</span>}
                                  {(edge.chunk_id || edge.source_id) && onReveal && (
                                    <button
                                      type="button"
                                      onClick={() => void openEvidence(edge)}
                                      className="text-primary hover:underline"
                                    >
                                      Open source
                                    </button>
                                  )}
                                </li>
                              ))}
                            </ul>
                          )}
                        </div>
                      )}
                    </div>
                  );
                })}
              </div>
            )}

            {revertableClaims.length > 0 && (
              <p className="mt-2 text-[11px] text-muted-foreground">
                {revertableClaims.length} superseded claim{revertableClaims.length === 1 ? "" : "s"} can be restored.
              </p>
            )}
          </div>
        </div>
      ) : null}
    </div>
  );
}
