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
      bundle = await fetchFeed({ limit: 100 });
      err = null;
    } catch (e) {
      err = e instanceof Error ? e.message : String(e);
    }
  }

  onMount(() => {
    const onChatPush = () => void load();
    window.addEventListener("minion:chat_updated", onChatPush);
    void load();
    const poll = setInterval(() => void load(), 45_000);
    return () => {
      window.removeEventListener("minion:chat_updated", onChatPush);
      clearInterval(poll);
    };
  });

  const riverItems = $derived(bundle?.items ?? []);
  const fortyTwo = $derived(bundle?.forty_two);
</script>

<div class="home" aria-label="Minion">
  <a class="settings-fab" href="/settings" title="Settings" aria-label="Settings">
    <svg width="18" height="18" viewBox="0 0 24 24" fill="none" aria-hidden="true">
      <path
        d="M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6Z"
        stroke="currentColor"
        stroke-width="1.5"
      />
      <path
        d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 1 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 1 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 1 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 1 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 1 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 1 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 1 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9c.2.65.77 1.09 1.44 1.09H21a2 2 0 1 1 0 4h-.09c-.67 0-1.24.44-1.44 1.09Z"
        stroke="currentColor"
        stroke-width="1.5"
      />
    </svg>
  </a>

  <h1 class="sr-only">Minion</h1>

  {#if err}
    <p class="home-err">{err}</p>
  {:else if !bundle}
    <p class="home-loading muted">Loading…</p>
  {:else}
    <div class="feed-scroll">
      <LifeChannel
        items={riverItems}
        {streaming}
        streamingPreview={streamBuf}
        onRefresh={load}
      />
    </div>

    <footer class="composer-dock">
      <FortyTwoComposer
        bind:streaming
        bind:streamBuf
        activeThreadId={fortyTwo?.active_thread_id ?? null}
        needsQuestion={fortyTwo?.needs_question ?? false}
        suggestions={fortyTwo?.suggestions ?? []}
        onRefresh={load}
      />
    </footer>
  {/if}
</div>

<style>
  .home {
    position: relative;
    display: flex;
    flex-direction: column;
    flex: 1;
    min-height: 0;
    height: 100%;
    max-width: 40rem;
    width: 100%;
    margin: 0 auto;
    padding: 0 0.85rem;
  }

  .sr-only {
    position: absolute;
    width: 1px;
    height: 1px;
    padding: 0;
    margin: -1px;
    overflow: hidden;
    clip: rect(0, 0, 0, 0);
    white-space: nowrap;
    border: 0;
  }

  .settings-fab {
    position: absolute;
    top: 0.65rem;
    right: 0.85rem;
    z-index: 2;
    display: flex;
    align-items: center;
    justify-content: center;
    width: 2rem;
    height: 2rem;
    border-radius: 999px;
    color: var(--muted);
    opacity: 0.55;
    transition:
      opacity 0.15s ease,
      color 0.15s ease,
      background 0.15s ease;
  }

  .settings-fab:hover {
    opacity: 1;
    color: var(--accent);
    background: color-mix(in srgb, var(--accent-soft) 70%, transparent);
  }

  .feed-scroll {
    flex: 1;
    min-height: 0;
    overflow-y: auto;
    overflow-x: hidden;
    scroll-behavior: smooth;
    padding: 2.5rem 0 0.35rem;
  }

  .composer-dock {
    flex-shrink: 0;
    padding-bottom: max(0.75rem, env(safe-area-inset-bottom));
    background: linear-gradient(to top, var(--bg) 82%, transparent);
  }

  .home-err {
    color: var(--danger);
    font-size: 0.85rem;
    padding: 3rem 0 1rem;
  }

  .home-loading {
    padding: 3rem 0;
    font-size: 0.88rem;
    text-align: center;
  }
</style>
