import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Clock, Loader2, Sparkles } from "lucide-react";
import {
  fetchFeed,
  fetchGraphCandidates,
  fetchProfileSummary,
  fetchToday,
  openSession,
  type ActivityFeedBundle,
  type ProfileSummary,
  type Source,
  type TodayBundle,
} from "../lib/api";
import {
  FEED_LANE_LABELS,
  formatFeedTime,
  getFeedLane,
  isFeedItem,
  type FeedSection,
} from "../lib/feedUtils";
import { MemorySearch } from "./MemorySearch";

type ActivityHomeProps = {
  displayName?: string;
  activeProfileId?: string | null;
  activeProfileName?: string | null;
  sources: Source[];
  onReveal: (path: string) => void | Promise<void>;
  onNavigateGraph: () => void;
  onNavigateWork?: () => void;
};

const LANE_ORDER: FeedSection[] = ["now", "observed", "parsed", "suggestion", "errors", "other"];
const BRIEFING_REFRESH_MS = 60_000;

function todayKey(): string {
  return new Date().toISOString().slice(0, 10);
}

export function ActivityHome({
  displayName,
  activeProfileId,
  activeProfileName,
  sources,
  onReveal,
  onNavigateGraph,
  onNavigateWork,
}: ActivityHomeProps) {
  const [today, setToday] = useState<TodayBundle | null>(null);
  const [profileSummary, setProfileSummary] = useState<ProfileSummary | null>(null);
  const [candidateCount, setCandidateCount] = useState(0);
  const [todayLoading, setTodayLoading] = useState(true);
  const [feed, setFeed] = useState<ActivityFeedBundle | null>(null);
  const [feedLoading, setFeedLoading] = useState(true);
  const [feedError, setFeedError] = useState<string | null>(null);
  const [laneFilter, setLaneFilter] = useState<"all" | FeedSection>("all");
  const [timeRange, setTimeRange] = useState<"last_hour" | "last_day" | "last_week" | "all">("last_day");
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});
  const lastBriefingRefresh = useRef(0);

  const sinceHours = useMemo(() => {
    if (timeRange === "all" || timeRange === "last_week") return 168;
    if (timeRange === "last_day") return 24;
    return 1;
  }, [timeRange]);

  const loadBriefing = useCallback(async () => {
    setTodayLoading(true);
    setFeedLoading(true);
    setFeedError(null);
    try {
      if (displayName) {
        await openSession({ display_name: displayName }).catch(() => undefined);
      }

      const profileSummaryPromise = activeProfileId
        ? fetchProfileSummary(activeProfileId).catch(() => null)
        : Promise.resolve(null);

      const [todayBundle, feedBundle, candidates, summary] = await Promise.all([
        fetchToday(),
        fetchFeed({
          limit: 100,
          since_hours: sinceHours,
          profile_id: activeProfileId ?? undefined,
        }),
        fetchGraphCandidates("open").catch(() => ({ candidates: [], count: 0 })),
        profileSummaryPromise,
      ]);

      setToday(todayBundle);
      setFeed(feedBundle);
      setCandidateCount(candidates.count ?? candidates.candidates.length);
      setProfileSummary(summary);
      sessionStorage.setItem("minion:briefing_date", todayKey());
      lastBriefingRefresh.current = Date.now();
    } catch (e) {
      console.error("Failed to load briefing:", e);
      setFeedError("Failed to load activity feed");
    } finally {
      setTodayLoading(false);
      setFeedLoading(false);
    }
  }, [displayName, activeProfileId, sinceHours]);

  useEffect(() => {
    void loadBriefing();
  }, [loadBriefing]);

  useEffect(() => {
    function onVisibilityChange() {
      if (document.visibilityState !== "visible") return;
      const isNewDay = sessionStorage.getItem("minion:briefing_date") !== todayKey();
      const stale = Date.now() - lastBriefingRefresh.current > BRIEFING_REFRESH_MS;
      if (isNewDay || stale) void loadBriefing();
    }

    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => document.removeEventListener("visibilitychange", onVisibilityChange);
  }, [loadBriefing]);

  const sessionBriefing =
    feed?.session?.briefing_summary?.trim() ||
    (feed?.session?.request_preview ? `Since you were here: ${feed.session.request_preview}` : "");

  const workingContext = today?.working_context as {
    focus?: { app_name?: string; window_title?: string };
    graph_context?: { open_candidates?: Array<{ title?: string }> };
  } | undefined;

  const focusLine =
    feed?.now?.title?.trim() ||
    workingContext?.focus?.window_title?.trim() ||
    workingContext?.focus?.app_name?.trim() ||
    today?.attention_24h?.top_apps?.trim() ||
    "";

  const graphHighlight =
    feed?.graph?.highlights?.[0]?.title?.trim() ||
    workingContext?.graph_context?.open_candidates?.[0]?.title?.trim() ||
    "";

  const issueCount = today?.needs_attention?.length ?? 0;
  const pendingWork = today?.work_items?.inferred_pending?.length ?? 0;
  const openWork = today?.work_items?.open?.length ?? 0;
  const reviewWork = today?.work_items?.review?.length ?? 0;
  const consentLevel = profileSummary?.consent_preview?.max_release_level;

  const filteredItems = useMemo(() => {
    if (!feed) return [];
    return feed.items.filter((item) => {
      const lane = getFeedLane(item);
      if (laneFilter !== "all" && lane !== laneFilter) return false;
      return true;
    });
  }, [feed, laneFilter]);

  const itemsByLane = useMemo(() => {
    const map: Record<FeedSection, typeof filteredItems> = {
      now: [],
      observed: [],
      parsed: [],
      suggestion: [],
      errors: [],
      other: [],
    };
    for (const item of filteredItems) {
      map[getFeedLane(item)].push(item);
    }
    return map;
  }, [filteredItems]);

  function toggleLane(lane: string) {
    setCollapsed((prev) => ({ ...prev, [lane]: !prev[lane] }));
  }

  function handleItemClick(item: (typeof filteredItems)[number]) {
    if (!isFeedItem(item)) {
      onNavigateGraph();
      return;
    }
    const refs = item.refs ?? {};
    if (refs.source_id) {
      const source = sources.find((s) => s.source_id === refs.source_id);
      if (source?.path) {
        void onReveal(source.path);
        return;
      }
    }
    if (refs.path) {
      void onReveal(refs.path);
      return;
    }
    if (refs.node_id) {
      onNavigateGraph();
    }
  }

  const briefingLoading = todayLoading && !today;

  return (
    <div className="mt-6 space-y-6">
      <section className="rounded-2xl border border-primary/20 bg-gradient-to-br from-primary/5 to-card p-4">
        <div className="flex items-center justify-between gap-2">
          <h2 className="inline-flex items-center gap-1.5 font-medium">
            <Sparkles className="size-4 text-primary" /> Today
          </h2>
          <div className="flex items-center gap-2 text-xs text-muted-foreground">
            {activeProfileName && <span>{activeProfileName}</span>}
            {profileSummary && (
              <span>
                {profileSummary.counts.sources} sources · {profileSummary.counts.chunks} chunks
              </span>
            )}
          </div>
        </div>
        {briefingLoading ? (
          <div className="mt-3 flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" /> Loading briefing…
          </div>
        ) : (
          <div className="mt-3 space-y-3 text-sm">
            {sessionBriefing ? (
              <p className="text-foreground">{sessionBriefing}</p>
            ) : (
              <p className="text-muted-foreground">
                {displayName ? `Welcome back, ${displayName}.` : "Welcome back."}{" "}
                {focusLine ? `Recent focus: ${focusLine}.` : "Your memory is ready to search."}
              </p>
            )}

            <div className="grid gap-2 sm:grid-cols-2">
              {focusLine && (
                <div className="rounded-lg bg-background/60 px-3 py-2 text-xs">
                  <span className="font-medium text-foreground">Focus</span>
                  <p className="mt-0.5 truncate text-muted-foreground">{focusLine}</p>
                </div>
              )}
              {graphHighlight && (
                <button
                  type="button"
                  onClick={onNavigateGraph}
                  className="rounded-lg bg-background/60 px-3 py-2 text-left text-xs hover:bg-accent/40"
                >
                  <span className="font-medium text-foreground">Graph highlight</span>
                  <p className="mt-0.5 truncate text-muted-foreground">{graphHighlight}</p>
                </button>
              )}
            </div>

            <div className="flex flex-wrap gap-2">
              {candidateCount > 0 && (
                <button
                  type="button"
                  onClick={onNavigateGraph}
                  className="rounded-full border border-violet-500/30 bg-violet-500/10 px-2.5 py-1 text-xs text-violet-700 dark:text-violet-300 hover:bg-violet-500/20"
                >
                  {candidateCount} graph question{candidateCount === 1 ? "" : "s"}
                </button>
              )}
              {(pendingWork > 0 || openWork > 0 || reviewWork > 0) && (
                <button
                  type="button"
                  onClick={onNavigateWork ?? onNavigateGraph}
                  className="rounded-full border border-blue-500/30 bg-blue-500/10 px-2.5 py-1 text-xs text-blue-700 dark:text-blue-300 hover:bg-blue-500/20"
                >
                  {openWork + reviewWork + pendingWork} work item
                  {openWork + reviewWork + pendingWork === 1 ? "" : "s"}
                </button>
              )}
              {issueCount > 0 && (
                <span className="rounded-full border border-amber-500/30 bg-amber-500/10 px-2.5 py-1 text-xs text-amber-700 dark:text-amber-300">
                  {issueCount} issue{issueCount === 1 ? "" : "s"} need attention
                </span>
              )}
              {consentLevel != null && (
                <span className="rounded-full border border-border px-2.5 py-1 text-xs text-muted-foreground">
                  Consent level {consentLevel}/5
                </span>
              )}
            </div>
          </div>
        )}
      </section>

      <MemorySearch profileId={activeProfileId} profileName={activeProfileName} />

      <section className="rounded-2xl border border-border bg-card p-4">
        <div className="mb-4 flex items-center justify-between gap-2">
          <h2 className="inline-flex items-center gap-1.5 font-medium">
            <Clock className="size-4 text-primary" /> Activity
          </h2>
          <div className="flex items-center gap-2">
            <select
              value={laneFilter}
              onChange={(e) => setLaneFilter(e.target.value as "all" | FeedSection)}
              className="rounded-lg border border-border bg-background px-2 py-1 text-xs hover:bg-accent"
            >
              <option value="all">All lanes</option>
              {LANE_ORDER.map((lane) => (
                <option key={lane} value={lane}>
                  {FEED_LANE_LABELS[lane]}
                </option>
              ))}
            </select>
            <select
              value={timeRange}
              onChange={(e) => setTimeRange(e.target.value as typeof timeRange)}
              className="rounded-lg border border-border bg-background px-2 py-1 text-xs hover:bg-accent"
            >
              <option value="last_hour">Last hour</option>
              <option value="last_day">Last day</option>
              <option value="last_week">Last week</option>
              <option value="all">All time</option>
            </select>
          </div>
        </div>

        {feedLoading && !feed ? (
          <div className="flex items-center justify-center py-8">
            <Loader2 className="size-6 animate-spin text-muted-foreground" />
          </div>
        ) : feedError ? (
          <div className="py-8 text-center">
            <p className="text-sm text-muted-foreground">{feedError}</p>
            <button type="button" onClick={() => void loadBriefing()} className="mt-2 text-sm text-primary hover:underline">
              Retry
            </button>
          </div>
        ) : (
          <div className="space-y-3">
            {LANE_ORDER.map((lane) => {
              const laneItems = itemsByLane[lane];
              if (laneItems.length === 0) return null;
              const laneId = `lane-${lane}`;
              const isCollapsed = collapsed[laneId];
              return (
                <div key={lane} className="rounded-lg border border-border bg-muted/30">
                  <button
                    type="button"
                    onClick={() => toggleLane(laneId)}
                    className="flex w-full items-center justify-between p-3 transition-colors hover:bg-accent/40"
                  >
                    <span className="text-sm font-medium">
                      {FEED_LANE_LABELS[lane]} ({laneItems.length})
                    </span>
                    <span className="text-muted-foreground">{isCollapsed ? "▶" : "▼"}</span>
                  </button>
                  {!isCollapsed && (
                    <div className="space-y-2 border-t border-border p-3">
                      {laneItems.slice(0, 12).map((item, idx) => {
                        const title = isFeedItem(item) ? item.title : item.proposal.title;
                        const body = isFeedItem(item) ? item.body : item.proposal.summary;
                        const itemId = isFeedItem(item) ? item.feed_id : item.proposal.proposal_id;
                        return (
                          <div
                            key={`${itemId}-${idx}`}
                            role="button"
                            tabIndex={0}
                            onClick={() => handleItemClick(item)}
                            onKeyDown={(e) => {
                              if (e.key === "Enter") handleItemClick(item);
                            }}
                            className="flex cursor-pointer items-start gap-2 rounded p-2 text-xs hover:bg-accent/40"
                          >
                            <span className="shrink-0 text-muted-foreground">{formatFeedTime(item.ts)}</span>
                            <div className="min-w-0 flex-1">
                              <p className="truncate font-medium">{title}</p>
                              {body && <p className="truncate text-muted-foreground">{body}</p>}
                            </div>
                          </div>
                        );
                      })}
                      {laneItems.length > 12 && (
                        <p className="text-center text-xs text-muted-foreground">+{laneItems.length - 12} more</p>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
            {filteredItems.length === 0 && (
              <p className="py-4 text-center text-sm text-muted-foreground">No recent activity for this profile</p>
            )}
          </div>
        )}
      </section>
    </div>
  );
}
