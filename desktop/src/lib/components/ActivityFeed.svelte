<script lang="ts">
  import {
    fortyTwoDismiss,
    patchIdentityClaim,
    patchTask,
    resolveHealthIssue,
    type CouncilFeedItem,
    type FeedItem,
    type FeedRow,
  } from "$lib/api";
  import ProposalCard from "$lib/components/ProposalCard.svelte";

  const CAPTURE_KINDS = new Set([
    "window_snapshot",
    "browser_visit",
    "window_focus",
    "focus",
    "ax_content_changed",
  ]);

  let {
    now = null,
    prefetch = [],
    items = [],
    onRefresh,
  }: {
    now?: FeedItem | null;
    prefetch?: FeedItem[];
    items?: FeedRow[];
    onRefresh?: () => void | Promise<void>;
  } = $props();

  let busy = $state<string | null>(null);

  function isCouncil(item: FeedRow): item is CouncilFeedItem {
    return item.item_kind === "council";
  }

  function riverKey(item: FeedRow): string {
    if (isCouncil(item)) return `council-${item.proposal.proposal_id}`;
    return item.feed_id;
  }

  function laneLabel(lane: string): string {
    if (lane === "now") return "Now";
    if (lane === "observed") return "Observed";
    if (lane === "parsed") return "Parsed";
    if (lane === "suggestion") return "Suggestion";
    if (lane === "conversation") return "42";
    return lane;
  }

  function isConversation(kind: string): boolean {
    return kind === "forty_two" || kind === "you";
  }

  function kindIcon(kind: string): string {
    if (kind === "window_snapshot" || kind === "window_focus" || kind === "focus") return "◫";
    if (kind === "browser_visit") return "◎";
    if (kind === "related_memory") return "↗";
    if (kind === "inferred_task") return "◇";
    return "·";
  }

  function isCapture(kind: string): boolean {
    return CAPTURE_KINDS.has(kind);
  }

  function formatTs(ts: number): string {
    if (!ts) return "";
    return new Date(ts * 1000).toLocaleString(undefined, {
      hour: "numeric",
      minute: "2-digit",
      month: "short",
      day: "numeric",
    });
  }

  async function act(item: FeedItem, actionId: string) {
    busy = item.feed_id;
    try {
      if (item.kind === "inferred_task" && item.refs.task_id) {
        if (actionId === "accept") await patchTask(item.refs.task_id, { status: "open" });
        if (actionId === "dismiss") await patchTask(item.refs.task_id, { status: "archived" });
      }
      if (item.kind === "identity_claim" && item.refs.claim_id) {
        if (actionId === "approve") await patchIdentityClaim(item.refs.claim_id, { status: "active" });
        if (actionId === "reject") await patchIdentityClaim(item.refs.claim_id, { status: "rejected" });
      }
      if (item.kind === "health_issue" && item.refs.issue_id && actionId === "resolve") {
        await resolveHealthIssue(item.refs.issue_id);
      }
      if (
        (item.kind === "forty_two" || item.lane === "conversation") &&
        item.refs.thread_id &&
        actionId === "dismiss"
      ) {
        await fortyTwoDismiss(item.refs.thread_id);
      }
      await onRefresh?.();
    } finally {
      busy = null;
    }
  }

  const empty = $derived(!now && prefetch.length === 0 && items.length === 0);
</script>

<div class="river">
  {#if empty}
    <div class="river-empty">
      <p class="muted">No activity yet — switch apps or drop a file into Sources.</p>
    </div>
  {:else}
    {#if now}
      <section class="river-section river-now" aria-label="Right now">
        <h2 class="section-label">Now</h2>
        <article class="feed-item lane-now kind-capture now-hero">
          <header class="feed-head">
            <span class="lane-chip lane-chip-now">{laneLabel(now.lane)}</span>
            <time class="feed-ts">{formatTs(now.ts)}</time>
          </header>
          <div class="feed-main">
            <span class="kind-glyph" aria-hidden="true">{kindIcon(now.kind)}</span>
            <div>
              <h3 class="feed-title">{now.title}</h3>
              {#if now.body}
                <p class="feed-body">{now.body}</p>
              {/if}
            </div>
          </div>
        </article>
      </section>
    {/if}

    {#if prefetch.length}
      <section class="river-section river-prefetch" aria-label="Related memory">
        <h2 class="section-label section-label-quiet">Related notes</h2>
        <div class="prefetch-stack">
          {#each prefetch as item (item.feed_id)}
            <article class="feed-item kind-related_memory prefetch-card">
              <header class="feed-head">
                <span class="lane-chip lane-chip-suggestion">{laneLabel(item.lane)}</span>
                <span class="feed-kind mono">{item.kind}</span>
              </header>
              <h3 class="feed-title">{item.title}</h3>
              {#if item.body}
                <p class="feed-body muted">{item.body}</p>
              {/if}
            </article>
          {/each}
        </div>
      </section>
    {/if}

    {#if items.length}
      <section class="river-section river-timeline" aria-label="Timeline">
        <h2 class="section-label">Timeline</h2>
        <ol class="timeline">
          {#each items as item, i (riverKey(item))}
            <li class="timeline-row" style="--i: {i}">
              {#if isCouncil(item)}
                <ProposalCard {item} {onRefresh} />
              {:else if isConversation(item.kind)}
                <article
                  class="chat-bubble"
                  class:chat-bubble-42={item.kind === "forty_two"}
                  class:chat-bubble-you={item.kind === "you"}
                >
                  <header class="chat-bubble-head">
                    <span class="chat-speaker">{item.kind === "forty_two" ? "42" : "You"}</span>
                    <time class="feed-ts">{formatTs(item.ts)}</time>
                  </header>
                  {#if item.body}
                    <p class="chat-bubble-body">{item.body}</p>
                  {/if}
                  {#if item.actions?.length}
                    <div class="feed-actions">
                      {#each item.actions as action}
                        <button
                          type="button"
                          class="btn"
                          disabled={busy === item.feed_id}
                          onclick={() => void act(item, action.id)}
                        >
                          {action.label}
                        </button>
                      {/each}
                    </div>
                  {/if}
                </article>
              {:else}
                <article
                  class="feed-item lane-{item.lane}"
                  class:kind-capture={isCapture(item.kind)}
                  class:kind-related_memory={item.kind === "related_memory"}
                >
                  <header class="feed-head">
                    <span class="lane-chip lane-chip-{item.lane}">{laneLabel(item.lane)}</span>
                    <span class="feed-kind mono">{item.kind}</span>
                    <time class="feed-ts">{formatTs(item.ts)}</time>
                  </header>
                  <div class="feed-main">
                    {#if isCapture(item.kind)}
                      <span class="kind-glyph capture" aria-hidden="true">{kindIcon(item.kind)}</span>
                    {/if}
                    <div class="feed-copy">
                      <h3 class="feed-title">{item.title}</h3>
                      {#if item.body}
                        <p class="feed-body" class:muted={!isCapture(item.kind)}>{item.body}</p>
                      {/if}
                    </div>
                  </div>
                  {#if item.parse}
                    <div class="feed-parse">
                      <span class="parse-status">{item.parse.status}</span>
                      {#if item.parse.reason}
                        <span class="parse-reason"> — {item.parse.reason}</span>
                      {/if}
                    </div>
                  {/if}
                  {#if item.graph_kinds?.length}
                    <div class="feed-graph-tags">
                      {#each item.graph_kinds as g}
                        <span class="graph-tag">{g}</span>
                      {/each}
                    </div>
                  {/if}
                  {#if item.actions?.length}
                    <div class="feed-actions">
                      {#each item.actions as action}
                        <button
                          type="button"
                          class="btn"
                          class:btn-primary={action.id === "accept" || action.id === "approve"}
                          disabled={busy === item.feed_id}
                          onclick={() => void act(item, action.id)}
                        >
                          {action.label}
                        </button>
                      {/each}
                    </div>
                  {/if}
                </article>
              {/if}
            </li>
          {/each}
        </ol>
      </section>
    {/if}
  {/if}
</div>

<style>
  .river {
    display: flex;
    flex-direction: column;
    gap: 1.5rem;
  }
  .river-empty {
    background: var(--panel);
    border: 1px dashed var(--border);
    border-radius: var(--radius);
    padding: 2rem 1.5rem;
    text-align: center;
  }
  .river-section {
    margin: 0;
  }
  .section-label {
    margin: 0 0 0.65rem;
    font-family: var(--font-display);
    font-size: 1.05rem;
    font-weight: 400;
    color: var(--ink);
    letter-spacing: 0.01em;
  }
  .section-label-quiet {
    font-size: 0.82rem;
    font-family: var(--font-body);
    font-weight: 600;
    text-transform: uppercase;
    letter-spacing: 0.1em;
    color: var(--muted);
  }
  .timeline {
    list-style: none;
    margin: 0;
    padding: 0 0 0 0.65rem;
    border-left: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    gap: 0.55rem;
  }
  .timeline-row {
    position: relative;
    animation: row-in 0.45s ease both;
    animation-delay: calc(var(--i, 0) * 35ms);
  }
  .timeline-row::before {
    content: "";
    position: absolute;
    left: calc(-0.65rem - 4px);
    top: 1.1rem;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--panel);
    border: 1px solid var(--border);
    transition:
      border-color 0.15s ease,
      background 0.15s ease;
  }
  .timeline-row:hover::before {
    border-color: var(--accent);
    background: var(--accent-soft);
  }
  @keyframes row-in {
    from {
      opacity: 0;
      transform: translateY(6px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }
  .prefetch-stack {
    display: flex;
    flex-direction: column;
    gap: 0.4rem;
  }
  .feed-item {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 0.8rem 0.95rem;
    box-shadow: var(--shadow-s);
    transition:
      border-color 0.18s ease,
      box-shadow 0.18s ease,
      transform 0.18s ease;
  }
  .feed-item:hover {
    border-color: color-mix(in srgb, var(--accent) 35%, var(--border));
    box-shadow: 0 3px 12px rgba(15, 28, 42, 0.07);
    transform: translateY(-1px);
  }
  .feed-item.lane-now,
  .now-hero {
    border-left: 3px solid var(--lane-now);
    background: linear-gradient(135deg, var(--accent-soft) 0%, var(--panel) 55%);
  }
  .feed-item.lane-suggestion:not(.prefetch-card) {
    border-left: 3px solid var(--lane-suggestion);
  }
  .feed-item.lane-observed {
    border-left: 3px solid var(--lane-observed);
  }
  .feed-item.lane-parsed {
    border-left: 3px solid #5a8f6e;
  }
  .feed-item.kind-capture {
    border-left-width: 3px;
    border-left-color: var(--accent);
  }
  .feed-item.kind-capture .feed-title {
    font-weight: 600;
  }
  .feed-item.kind-capture .feed-body {
    font-size: 0.84rem;
    line-height: 1.5;
    color: var(--ink);
    white-space: pre-wrap;
    word-break: break-word;
  }
  .prefetch-card,
  .feed-item.kind-related_memory {
    opacity: 0.72;
    background: color-mix(in srgb, var(--panel) 88%, var(--panel-2));
    border-style: dashed;
    box-shadow: none;
  }
  .prefetch-card:hover,
  .feed-item.kind-related_memory:hover {
    opacity: 0.9;
    transform: none;
    box-shadow: var(--shadow-s);
  }
  .feed-head {
    display: flex;
    flex-wrap: wrap;
    gap: 0.45rem;
    align-items: center;
    font-size: 0.7rem;
    margin-bottom: 0.4rem;
  }
  .lane-chip {
    font-weight: 600;
    font-size: 0.62rem;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    padding: 0.12rem 0.45rem;
    border-radius: 3px;
    border: 1px solid transparent;
  }
  .lane-chip-now {
    color: var(--lane-now);
    background: color-mix(in srgb, var(--accent-soft) 80%, transparent);
    border-color: color-mix(in srgb, var(--lane-now) 25%, transparent);
  }
  .lane-chip-observed {
    color: var(--lane-observed);
    background: color-mix(in srgb, var(--lane-observed) 12%, var(--panel));
    border-color: color-mix(in srgb, var(--lane-observed) 22%, transparent);
  }
  .lane-chip-suggestion {
    color: var(--lane-suggestion);
    background: color-mix(in srgb, var(--warn) 14%, var(--panel));
    border-color: color-mix(in srgb, var(--warn) 28%, transparent);
  }
  .lane-chip-parsed {
    color: #4a7a5c;
    background: color-mix(in srgb, #5a8f6e 12%, var(--panel));
  }
  .feed-kind {
    color: var(--muted);
    font-size: 0.65rem;
  }
  .feed-ts {
    margin-left: auto;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
  }
  .feed-main {
    display: flex;
    gap: 0.55rem;
    align-items: flex-start;
  }
  .kind-glyph {
    flex-shrink: 0;
    width: 1.35rem;
    height: 1.35rem;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.85rem;
    color: var(--muted);
    border: 1px solid var(--border);
    border-radius: 4px;
    background: var(--panel-2);
  }
  .kind-glyph.capture {
    color: var(--accent);
    border-color: color-mix(in srgb, var(--accent) 30%, var(--border));
    background: var(--accent-soft);
  }
  .feed-copy {
    min-width: 0;
    flex: 1;
  }
  .feed-title {
    margin: 0 0 0.3rem;
    font-size: 0.92rem;
    font-weight: 500;
    color: var(--ink);
    line-height: 1.35;
  }
  .feed-body {
    margin: 0;
    font-size: 0.82rem;
    line-height: 1.45;
  }
  .feed-parse {
    font-size: 0.74rem;
    margin-top: 0.45rem;
    padding-top: 0.4rem;
    border-top: 1px solid color-mix(in srgb, var(--border) 70%, transparent);
  }
  .parse-status {
    font-weight: 600;
    color: var(--accent);
  }
  .parse-reason {
    color: var(--muted);
  }
  .feed-graph-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem;
    margin-top: 0.4rem;
  }
  .graph-tag {
    font-size: 0.62rem;
    padding: 0.08rem 0.38rem;
    border-radius: 3px;
    background: var(--panel-2);
    color: var(--muted);
    text-transform: uppercase;
    letter-spacing: 0.04em;
  }
  .feed-actions {
    display: flex;
    gap: 0.45rem;
    margin-top: 0.55rem;
    flex-wrap: wrap;
  }
  .timeline-row:has(.chat-bubble) {
    list-style: none;
  }
  .timeline-row:has(.chat-bubble)::before {
    display: none;
  }
  .chat-bubble {
    max-width: min(36rem, 92%);
    padding: 0.65rem 0.85rem;
    border-radius: var(--radius);
    border: 1px solid var(--border);
    background: var(--panel);
    box-shadow: var(--shadow-s);
  }
  .chat-bubble-42 {
    margin-right: auto;
    border-left: 3px solid var(--accent);
    background: color-mix(in srgb, var(--accent-soft) 40%, var(--panel));
  }
  .chat-bubble-you {
    margin-left: auto;
    border-right: 3px solid color-mix(in srgb, var(--muted) 50%, var(--border));
    background: var(--panel-2);
  }
  .chat-bubble-head {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.35rem;
    font-size: 0.7rem;
  }
  .chat-speaker {
    font-weight: 600;
    font-family: var(--font-display);
    color: var(--accent);
  }
  .chat-bubble-you .chat-speaker {
    color: var(--ink);
  }
  .chat-bubble-body {
    margin: 0;
    font-size: 0.86rem;
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-word;
  }
</style>
