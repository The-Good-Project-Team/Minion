<script lang="ts">
  import { onMount } from "svelte";
  import LifeChannel from "$lib/components/LifeChannel.svelte";
  import FortyTwoComposer from "$lib/components/FortyTwoComposer.svelte";
  import { fetchFeed } from "$lib/api";

  let bundle = $state<Awaited<ReturnType<typeof fetchFeed>> | null>(null);
  let err = $state<string | null>(null);
  let streaming = $state(false);
  let streamBuf = $state("");

  async function load() {
    try {
      bundle = await fetchFeed({ limit: 80 });
      err = null;
    } catch (e) {
      err = e instanceof Error ? e.message : String(e);
    }
  }

  onMount(() => {
    const onChatPush = () => void load();
    window.addEventListener("minion:chat_updated", onChatPush);
    void load();
    return () => window.removeEventListener("minion:chat_updated", onChatPush);
  });

  const riverItems = $derived(bundle?.items ?? []);
  const fortyTwo = $derived(bundle?.forty_two);
</script>

<section class="chat-page" aria-label="Chat">
  {#if err}
    <p class="err">{err}</p>
  {:else if !bundle}
    <p class="muted load">Loading…</p>
  {:else}
    <header class="channel-top">
      <h1 class="channel-title"># chat</h1>
      <p class="channel-lede muted">Minion, 42, You, and Coach share one thread. Talk here first.</p>
    </header>
    <div class="channel-scroll">
      <LifeChannel
        items={riverItems}
        {streaming}
        streamingPreview={streamBuf}
        onRefresh={load}
      />
    </div>
    <FortyTwoComposer
      bind:streaming
      bind:streamBuf
      activeThreadId={fortyTwo?.active_thread_id ?? null}
      needsQuestion={fortyTwo?.needs_question ?? false}
      suggestions={fortyTwo?.suggestions ?? []}
      onRefresh={load}
    />
  {/if}
</section>

<style>
  .chat-page {
    display: flex;
    flex-direction: column;
    flex: 1;
    min-height: 0;
    max-width: 52rem;
    width: 100%;
  }
  .err {
    color: var(--danger);
    font-size: 0.85rem;
  }
  .load {
    padding: 1rem 0;
    font-size: 0.85rem;
  }
  .channel-top {
    flex-shrink: 0;
    padding-bottom: 0.65rem;
    border-bottom: 1px solid var(--border);
    margin-bottom: 0.35rem;
  }
  .channel-title {
    margin: 0;
    font-family: var(--font-body);
    font-size: 1.15rem;
    font-weight: 600;
    letter-spacing: -0.01em;
  }
  .channel-lede {
    margin: 0.25rem 0 0;
    font-size: 0.8rem;
  }
  .channel-scroll {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
  }
</style>
