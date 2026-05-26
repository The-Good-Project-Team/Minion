<script lang="ts">
  import {
    councilApprove,
    fortyTwoDismiss,
    fortyTwoReply,
    patchIdentityClaim,
    patchTask,
    resolveHealthIssue,
    type CouncilFeedItem,
    type FeedItem,
    type FeedRow,
  } from "$lib/api";

  const CAPTURE_KINDS = new Set([
    "window_snapshot",
    "browser_visit",
    "window_focus",
    "focus",
    "ax_content_changed",
  ]);

  type SpeakerId = "minion" | "forty_two" | "you" | "coach";

  type Speaker = {
    id: SpeakerId;
    name: string;
    subtitle: string;
    initial: string;
  };

  const SPEAKERS: Record<SpeakerId, Speaker> = {
    minion: { id: "minion", name: "Minion", subtitle: "", initial: "M" },
    forty_two: { id: "forty_two", name: "42", subtitle: "", initial: "42" },
    you: { id: "you", name: "You", subtitle: "", initial: "Y" },
    coach: { id: "coach", name: "Coach", subtitle: "", initial: "C" },
  };

  let {
    items = [],
    onRefresh,
    streamingPreview = "",
    streaming = false,
  }: {
    items?: FeedRow[];
    onRefresh?: () => void | Promise<void>;
    streamingPreview?: string;
    streaming?: boolean;
  } = $props();

  let busy = $state<string | null>(null);

  const ordered = $derived(
    [...items].sort((a, b) => {
      const ta = isCouncil(a) ? a.ts : a.ts;
      const tb = isCouncil(b) ? b.ts : b.ts;
      return ta - tb;
    }),
  );

  function isCouncil(item: FeedRow): item is CouncilFeedItem {
    return item.item_kind === "council";
  }

  function rowKey(item: FeedRow): string {
    if (isCouncil(item)) return `council-${item.proposal.proposal_id}`;
    return item.feed_id;
  }

  function speakerFor(item: FeedItem): Speaker {
    if (item.kind === "graph_update" || item.kind === "graph_status") return SPEAKERS.forty_two;
    if (item.kind === "forty_two") return SPEAKERS.forty_two;
    if (item.kind === "you") return SPEAKERS.you;
    return SPEAKERS.minion;
  }

  function speakerForCouncil(_item: CouncilFeedItem): Speaker {
    return SPEAKERS.coach;
  }

  function messageText(item: FeedItem): string {
    const raw = (item.body || item.title || "").trim();
    if (item.kind === "forty_two" || item.kind === "you") {
      return raw.replace(/^\*\*42:\*\*\s*/gm, "").trim();
    }
    if (item.kind === "graph_update" || item.kind === "graph_status") {
      return raw || item.title || "Graph update.";
    }
    if (CAPTURE_KINDS.has(item.kind)) {
      return item.title || raw;
    }
    if (item.body && item.title) return `${item.title} — ${item.body}`;
    return item.title || item.body || "";
  }

  function formatTs(ts: number): string {
    if (!ts) return "";
    const d = new Date(ts * 1000);
    const now = new Date();
    const sameDay =
      d.getDate() === now.getDate() &&
      d.getMonth() === now.getMonth() &&
      d.getFullYear() === now.getFullYear();
    return d.toLocaleString(undefined, {
      hour: "numeric",
      minute: "2-digit",
      ...(sameDay ? {} : { month: "short", day: "numeric" }),
    });
  }

  function councilText(item: CouncilFeedItem): string {
    const parts: string[] = [];
    const title = String(item.proposal?.title ?? "").trim();
    const summary = String(item.proposal?.summary ?? "").trim();
    const body = String(item.proposal?.payload?.body ?? "").trim();
    if (title) parts.push(title);
    if (summary) parts.push(summary);
    if (body) parts.push(body);
    return parts.join("\n\n");
  }

  async function councilAct(item: CouncilFeedItem, actionId: string) {
    const pid = item.proposal?.proposal_id;
    if (!pid) return;
    busy = `council-${pid}`;
    try {
      await councilApprove({ proposal_id: pid, action: actionId });
      await onRefresh?.();
    } finally {
      busy = null;
    }
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
        item.refs.thread_id
      ) {
        if (actionId.startsWith("pick:")) {
          const name = actionId.slice(5).trim();
          if (name) await fortyTwoReply(name, item.refs.thread_id);
        } else if (actionId === "dismiss") {
          await fortyTwoDismiss(item.refs.thread_id);
        }
      }
      await onRefresh?.();
    } finally {
      busy = null;
    }
  }
</script>

<div class="channel" aria-label="Agent feed">
  {#if ordered.length === 0 && !streaming}
    <p class="channel-empty muted">Agents are quiet. They'll post here as they observe and learn.</p>
  {:else}
    <ol class="message-list">
      {#each ordered as item (rowKey(item))}
        <li class="message-row">
          {#if isCouncil(item)}
            {@const sp = speakerForCouncil(item)}
            <article class="msg" data-speaker={sp.id}>
              <div class="avatar" data-speaker={sp.id} aria-hidden="true">{sp.initial}</div>
              <div class="msg-main">
                <header class="msg-head">
                  <span class="msg-name">{sp.name}</span>
                  {#if sp.subtitle}<span class="msg-sub">{sp.subtitle}</span>{/if}
                  <time class="msg-time">{formatTs(item.ts)}</time>
                </header>
                {#if councilText(item)}
                  <div class="msg-body">{councilText(item)}</div>
                {/if}
                {#if item.approval?.options?.length}
                  <div class="msg-actions">
                    {#each item.approval.options as action}
                      <button
                        type="button"
                        class="msg-btn"
                        disabled={busy === `council-${item.proposal?.proposal_id}`}
                        onclick={() => void councilAct(item, action.id)}
                      >
                        {action.label}
                      </button>
                    {/each}
                  </div>
                {/if}
              </div>
            </article>
          {:else}
            {@const sp = speakerFor(item)}
            <article class="msg" data-speaker={sp.id}>
              <div class="avatar" data-speaker={sp.id} aria-hidden="true">{sp.initial}</div>
              <div class="msg-main">
                <header class="msg-head">
                  <span class="msg-name">{sp.name}</span>
                  {#if sp.subtitle}<span class="msg-sub">{sp.subtitle}</span>{/if}
                  <time class="msg-time">{formatTs(item.ts)}</time>
                </header>
                {#if messageText(item)}
                  <div class="msg-body">{messageText(item)}</div>
                {/if}
                {#if item.actions?.length}
                  <div class="msg-actions">
                    {#each item.actions as action}
                      <button
                        type="button"
                        class="msg-btn"
                        disabled={busy === item.feed_id}
                        onclick={() => void act(item, action.id)}
                      >
                        {action.label}
                      </button>
                    {/each}
                  </div>
                {/if}
              </div>
            </article>
          {/if}
        </li>
      {/each}
      {#if streaming && streamingPreview}
        <li class="message-row">
          <article class="msg msg-pending" data-speaker="forty_two">
            <div class="avatar" data-speaker="forty_two" aria-hidden="true">42</div>
            <div class="msg-main">
              <header class="msg-head">
                <span class="msg-name">42</span>
                <span class="msg-sub">typing…</span>
              </header>
              <div class="msg-body">{streamingPreview}</div>
            </div>
          </article>
        </li>
      {/if}
    </ol>
  {/if}
</div>

<style>
  .channel {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }
  .channel-empty {
    padding: 3rem 0.5rem;
    font-size: 0.88rem;
    text-align: center;
    line-height: 1.5;
  }
  .message-list {
    list-style: none;
    margin: 0;
    padding: 0.5rem 0 1rem;
    display: flex;
    flex-direction: column;
    gap: 0.15rem;
  }
  .message-row {
    margin: 0;
    padding: 0.35rem 0;
  }
  .message-row:hover {
    background: color-mix(in srgb, var(--panel) 55%, transparent);
    border-radius: var(--radius);
  }
  .msg {
    display: grid;
    grid-template-columns: 2.25rem 1fr;
    gap: 0.65rem;
    padding: 0.15rem 0.35rem;
    align-items: start;
  }
  .avatar {
    width: 2.25rem;
    height: 2.25rem;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 0.72rem;
    font-weight: 700;
    flex-shrink: 0;
    margin-top: 0.1rem;
  }
  .avatar[data-speaker="minion"] {
    background: #1a3a52;
    color: #e8f4fc;
  }
  .avatar[data-speaker="forty_two"] {
    background: #2d4a6e;
    color: #d4e8ff;
    font-size: 0.62rem;
  }
  .avatar[data-speaker="you"] {
    background: var(--accent);
    color: white;
  }
  .avatar[data-speaker="coach"] {
    background: #4a3d6b;
    color: #efe8ff;
  }
  .msg-main {
    min-width: 0;
  }
  .msg-head {
    display: flex;
    align-items: baseline;
    flex-wrap: wrap;
    gap: 0.35rem;
    line-height: 1.2;
    margin-bottom: 0.2rem;
  }
  .msg-name {
    font-weight: 600;
    font-size: 0.9rem;
  }
  .msg-sub {
    font-size: 0.72rem;
    color: var(--muted);
  }
  .msg-sub::before {
    content: "· ";
  }
  .msg-time {
    margin-left: auto;
    font-size: 0.68rem;
    color: var(--muted);
    opacity: 0;
    transition: opacity 0.12s ease;
  }
  .msg:hover .msg-time {
    opacity: 1;
  }
  .msg-body {
    font-size: 0.9rem;
    line-height: 1.5;
    white-space: pre-wrap;
    word-break: break-word;
    color: var(--ink);
  }
  .msg-pending .msg-body {
    color: var(--muted);
  }
  .msg-actions {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
    margin-top: 0.45rem;
  }
  .msg-btn {
    font: inherit;
    font-size: 0.75rem;
    padding: 0.2rem 0.55rem;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--panel);
    cursor: pointer;
  }
  .msg-btn:hover:not(:disabled) {
    border-color: var(--accent);
    background: var(--accent-soft);
  }
  .msg-btn:disabled {
    opacity: 0.5;
  }
</style>
