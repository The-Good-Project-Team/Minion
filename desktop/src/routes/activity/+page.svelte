<script lang="ts">
  import { onMount } from "svelte";
  import ActivityFeed from "$lib/components/ActivityFeed.svelte";
  import FortyTwoComposer from "$lib/components/FortyTwoComposer.svelte";
  import { fetchChatBadge, fetchFeed, type ActivityFeedBundle, type FeedItem } from "$lib/api";

  let bundle = $state<ActivityFeedBundle | null>(null);
  let err = $state<string | null>(null);
  let chatOpen = $state(0);

  async function load() {
    try {
      bundle = await fetchFeed({ limit: 80 });
      err = null;
    } catch (e) {
      err = e instanceof Error ? e.message : String(e);
    }
    try {
      const b = await fetchChatBadge();
      chatOpen = b.open_count;
    } catch {
      chatOpen = 0;
    }
  }

  onMount(load);

  const nowItem = $derived(bundle?.now ?? null);
  const prefetch = $derived((bundle?.memory_prefetch ?? []) as FeedItem[]);
  const riverItems = $derived(bundle?.items ?? []);
  const fortyTwo = $derived(bundle?.forty_two);
</script>

<header class="activity-masthead">
  <div class="masthead-copy">
    <p class="eyebrow">Context vault</p>
    <h1>
      Activity
      {#if chatOpen > 0}
        <span class="chat-badge" title="Open threads with 42">{chatOpen}</span>
      {/if}
    </h1>
    <p class="lede">
      One stream — capture, sync, and <strong>42</strong> asking the questions so you don't have to.
    </p>
  </div>
  <button type="button" class="btn btn-refresh" onclick={() => void load()} aria-label="Refresh activity">
    Refresh
  </button>
</header>

<section class="activity-main" aria-label="Activity stream">
  {#if err}
    <div class="state-card"><p class="muted">{err}</p></div>
  {:else if !bundle}
    <div class="state-card"><p class="muted">Loading activity…</p></div>
  {:else}
    <ActivityFeed now={nowItem} prefetch={prefetch} items={riverItems} onRefresh={load} />
    <FortyTwoComposer
      activeThreadId={fortyTwo?.active_thread_id ?? null}
      needsQuestion={fortyTwo?.needs_question ?? false}
      onRefresh={load}
    />
  {/if}
</section>

<style>
  .activity-masthead {
    display: flex;
    align-items: flex-end;
    justify-content: space-between;
    gap: 1.25rem;
    margin-bottom: 1.25rem;
    padding-bottom: 1rem;
    border-bottom: 1px solid var(--border);
  }
  .masthead-copy {
    min-width: 0;
  }
  .eyebrow {
    margin: 0 0 0.2rem;
    font-size: 0.68rem;
    font-weight: 600;
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--muted);
  }
  .activity-masthead h1 {
    margin: 0;
    font-family: var(--font-display);
    font-size: clamp(1.75rem, 3vw, 2.15rem);
    font-weight: 400;
    line-height: 1.1;
    display: flex;
    align-items: center;
    gap: 0.5rem;
    color: var(--ink);
  }
  .chat-badge {
    font-family: var(--font-body);
    font-size: 0.68rem;
    font-weight: 600;
    background: var(--accent);
    color: white;
    border-radius: 999px;
    min-width: 1.35rem;
    height: 1.35rem;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 0 0.4rem;
    vertical-align: middle;
  }
  .lede {
    margin: 0.35rem 0 0;
    font-size: 0.88rem;
    color: var(--muted);
    max-width: 42ch;
    line-height: 1.45;
  }
  .lede strong {
    font-weight: 600;
    color: var(--accent);
  }
  .btn-refresh {
    flex-shrink: 0;
    font-size: 0.8rem;
    letter-spacing: 0.02em;
  }
  .state-card {
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 1.25rem 1.35rem;
    box-shadow: var(--shadow-s);
  }
  .activity-main {
    min-width: 0;
    max-width: 52rem;
  }
</style>
