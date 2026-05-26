<script lang="ts">
  import { councilApprove, type CouncilFeedItem } from "$lib/api";
  import { invoke } from "$lib/tauri-bridge";

  let {
    item,
    onRefresh,
  }: {
    item: CouncilFeedItem;
    onRefresh?: () => void | Promise<void>;
  } = $props();

  let busy = $state(false);

  const intensity = $derived(item.proposal?.intensity ?? "standard");

  async function act(actionId: string) {
    const pid = item.proposal?.proposal_id;
    if (!pid) return;
    busy = true;
    try {
      const result = await councilApprove({ proposal_id: pid, action: actionId });
      if (actionId === "approve" && result.ok) {
        try {
          await invoke("council_bridge_open", {
            skillId: item.required_skill,
            payload: item.proposal?.payload ?? {},
          });
        } catch {
          /* bridge optional off-web */
        }
      }
      await onRefresh?.();
    } finally {
      busy = false;
    }
  }
</script>

<article class="feed-item council-card" class:elevated={intensity === "elevated"}>
  <header class="feed-head">
    <span class="feed-lane">{intensity === "elevated" ? "Elevated" : "Council"}</span>
    <span class="feed-kind mono">{item.proposal?.proposal_type}</span>
    <time class="feed-ts muted">{new Date(item.ts * 1000).toLocaleString()}</time>
  </header>
  <h3 class="feed-title">{item.proposal?.title}</h3>
  {#if item.proposal?.summary}
    <p class="feed-body muted">{item.proposal.summary}</p>
  {/if}
  {#if item.proposal?.payload?.body}
    <blockquote class="payload-preview">{item.proposal.payload.body}</blockquote>
  {/if}
  {#if item.required_info && Object.keys(item.required_info).length}
    <ul class="info-keys muted">
      {#each Object.entries(item.required_info) as [key, entry]}
        <li>{key}: {entry.status}{entry.label ? ` (${entry.label})` : ""}</li>
      {/each}
    </ul>
  {/if}
  <div class="feed-actions">
    {#each item.approval?.options ?? [] as action}
      <button
        type="button"
        class="btn"
        class:btn-primary={action.id === "approve"}
        disabled={busy}
        onclick={() => void act(action.id)}
      >
        {action.label}
      </button>
    {/each}
  </div>
</article>

<style>
  .council-card.elevated {
    border-color: var(--accent, #c9a227);
    box-shadow: 0 0 0 1px color-mix(in srgb, var(--accent, #c9a227) 35%, transparent);
  }
  .payload-preview {
    margin: 0.5rem 0;
    padding: 0.5rem 0.75rem;
    border-left: 3px solid var(--border);
    font-size: 0.9rem;
    white-space: pre-wrap;
  }
  .info-keys {
    font-size: 0.75rem;
    list-style: none;
    padding: 0;
    margin: 0.35rem 0;
  }
</style>
