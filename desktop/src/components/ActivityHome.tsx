import { useCallback, useEffect, useMemo, useState } from "react";
import { Clock, Loader2, Sparkles } from "lucide-react";
import {
  fetchFeed,
  fetchToday,
  openSession,
  type ActivityFeedBundle,
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
};

const LANE_ORDER: FeedSection[] = ["now", "observed", "parsed", "suggestion", "errors", "other"];

export function ActivityHome({
  displayName,
  activeProfileId,
  activeProfileName,
  sources,
  onReveal,
  onNavigateGraph,
}: ActivityHomeProps) {
  const [today, setToday] = useState<TodayBundle | null>(null);
  const [todayLoading, setTodayLoading] = useState(true);
  const [feed, setFeed] = useState<ActivityFeedBundle | null>(null);
  const [feedLoading, setFeedLoading] = useState(true);
  const [feedError, setFeedError] = useState<string | null>(null);
  const [laneFilter, setLaneFilter] = useState<"all" | FeedSection>("all");
  const [timeRange, setTimeRange] = useState<"last_hour" | "last_day" | "last_week" | "all">("last_day");
  const [collapsed, setCollapsed] = useState<Record<string, boolean>>({});

  const sinceHours = useMemo(() => {
    if (timeRange === "all" || timeRange === "last_week") return 168;
    if (timeRange === "last_day") return 24;
    return 1;
  }, [timeRange]);

  const loadToday = useCallback(async () => {
    setTodayLoading(true);
    try {
      if (displayName) {
        await openSession({ display_name: displayName }).catch(() => undefined);
      }
      const bundle = await fetchToday();
      setToday(bundle);
    } catch (e) {
      console.error("Failed to load today bundle:", e);
    } finally {
      setTodayLoading(false);
    }
  }, [displayName]);

  const loadFeed = useCallback(async () => {
    setFeedLoading(true);
    setFeedError(null);
    try {
      const data = await fetchFeed({
        limit: 100,
        since_hours: sinceHours,
        profile_id: activeProfileId ?? undefined,
      });
      setFeed(data);
    } catch (e) {
      console.error("Failed to load activity feed:", e);
      setFeedError("Failed to load activity feed");
    } finally {
      setFeedLoading(false);
    }
  }, [sinceHours, activeProfileId]);

  useEffect(() => {
    void loadToday();
  }, [loadToday, activeProfileId]);

  useEffect(() => {
    void loadFeed();
  }, [loadFeed]);

  const sessionBriefing =
    feed?.session?.briefing_summary?.trim() ||
    (feed?.session?.request_preview ? `Since you were here: ${feed.session.request_preview}` : "");

  const attentionLine = today?.attention_24h?.top_apps?.trim();
  const issueCount = today?.needs_attention?.length ?? 0;
  const pendingWork = today?.work_items?.inferred_pending?.length ?? 0;

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

  return (
    <div className="mt-6 space-y-6">
      <section className="rounded-2xl border border-primary/20 bg-gradient-to-br from-primary/5 to-card p-4">
        <div className="flex items-center justify-between gap-2">
          <h2 className="inline-flex items-center gap-1.5 font-medium">
            <Sparkles className="size-4 text-primary" /> Today
          </h2>
          {activeProfileName && (
            <span className="text-xs text-muted-foreground">{activeProfileName}</span>
          )}
        </div>
        {todayLoading && !today ? (
          <div className="mt-3 flex items-center gap-2 text-sm text-muted-foreground">
            <Loader2 className="size-4 animate-spin" /> Loading briefing…
          </div>
        ) : (
          <div className="mt-3 space-y-2 text-sm">
            {sessionBriefing ? (
              <p className="text-foreground">{sessionBriefing}</p>
            ) : (
              <p className="text-muted-foreground">
                {displayName ? `Welcome back, ${displayName}.` : "Welcome back."}{" "}
                {attentionLine ? `Recent focus: ${attentionLine}.` : "Your memory is ready to search."}
              </p>
            )}
            {feed?.now && (
              <p className="text-xs text-muted-foreground">
                <span className="font-medium text-foreground">Now:</span> {feed.now.title}
              </p>
            )}
            <div className="flex flex-wrap gap-3 text-xs text-muted-foreground">
              {issueCount > 0 && <span>{issueCount} issue{issueCount === 1 ? "" : "s"} need attention</span>}
              {pendingWork > 0 && <span>{pendingWork} suggested task{pendingWork === 1 ? "" : "s"}</span>}
              {today?.work_items?.open?.length ? (
                <span>{today.work_items.open.length} open work item{today.work_items.open.length === 1 ? "" : "s"}</span>
              ) : null}
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
            <button type="button" onClick={() => void loadFeed()} className="mt-2 text-sm text-primary hover:underline">
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
