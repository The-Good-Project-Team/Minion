<script lang="ts">
  import type { ScreenContextStatus, Status } from "$lib/api";

  let {
    screenWatch = null,
    status = null,
    livePulse = false,
    wakeHighlight = false,
  }: {
    screenWatch?: ScreenContextStatus | null;
    status?: Status | null;
    livePulse?: boolean;
    wakeHighlight?: boolean;
  } = $props();

  const focus = $derived(screenWatch?.last_event ?? null);
  const app = $derived(
    focus && typeof focus === "object" ? String((focus as Record<string, unknown>).app_name ?? "").trim() : "",
  );
  const title = $derived(
    focus && typeof focus === "object" ? String((focus as Record<string, unknown>).window_title ?? "").trim() : "",
  );

  const macos = $derived(screenWatch?.macos_watch ?? null);
  const watcherOff = $derived(
    !screenWatch?.watcher_supported ||
      macos?.watchers_env_disabled === true ||
      status?.watcher?.running === false,
  );
  const micOn = $derived(
    screenWatch?.full_listening_active === true || screenWatch?.listening_active === true,
  );
  const doing = $derived(app || title);
</script>

{#if doing || !watcherOff || micOn || wakeHighlight}
  <div
    class="focus-strip"
    class:live={!watcherOff || micOn}
    class:wake={wakeHighlight}
    class:idle={watcherOff && !micOn && !doing}
  >
    {#if doing}
      <span class="doing">{app}{#if title} — {title}{/if}</span>
    {:else if micOn}
      <span class="status">Mic on</span>
    {:else if watcherOff}
      <span class="status muted">Capture off</span>
    {:else}
      <span class="status muted">Watching…</span>
    {/if}
  </div>
{/if}

<style>
  .focus-strip {
    padding: 0.4rem 1.25rem;
    border-bottom: 1px solid var(--border);
    font-size: 0.88rem;
    line-height: 1.35;
    background: var(--panel);
  }
  .focus-strip.live {
    background: color-mix(in srgb, var(--accent-soft) 25%, var(--panel));
  }
  .focus-strip.wake {
    background: color-mix(in srgb, #c9a227 22%, var(--panel));
  }
  .doing {
    font-weight: 500;
    color: var(--ink);
  }
  .status {
    font-size: 0.8rem;
  }
  .muted {
    color: var(--muted);
  }
</style>
