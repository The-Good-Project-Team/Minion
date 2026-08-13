import type { CouncilFeedItem, FeedItem, FeedRow } from "./api";

export function formatFeedTime(ts: number): string {
  const now = Date.now() / 1000;
  const diff = now - ts;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)}m ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)}h ago`;
  return `${Math.floor(diff / 86400)}d ago`;
}

export function isFeedItem(item: FeedRow): item is FeedItem {
  return item.item_kind !== "council";
}

export function isCouncilFeedItem(item: FeedRow): item is CouncilFeedItem {
  return item.item_kind === "council";
}

export type FeedSection = "now" | "observed" | "parsed" | "suggestion" | "errors" | "other";

export function getFeedLane(item: FeedRow): FeedSection {
  if (item.item_kind === "council") return "suggestion";
  const lane = String(item.lane || "").toLowerCase();
  if (lane === "now") return "now";
  if (lane === "observed" || lane === "observation") return "observed";
  if (lane === "parsed") return "parsed";
  if (lane === "suggestion") return "suggestion";
  const kind = item.kind?.toLowerCase() || "";
  if (kind.includes("error") || kind.includes("failed") || kind.includes("issue")) return "errors";
  if (kind.includes("ingest") || kind.includes("source") || kind.includes("indexed")) return "parsed";
  if (kind.includes("ambient") || kind.includes("screen") || kind.includes("focus")) return "observed";
  if (kind.includes("graph") || kind.includes("node")) return "suggestion";
  return "other";
}

export const FEED_LANE_LABELS: Record<FeedSection, string> = {
  now: "Now",
  observed: "Observed",
  parsed: "Parsed",
  suggestion: "Suggestions",
  errors: "Needs attention",
  other: "Other",
};
