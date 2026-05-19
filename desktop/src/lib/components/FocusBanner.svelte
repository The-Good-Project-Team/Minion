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
    /** Set when screen-context://update fires (fresh capture). */
    livePulse?: boolean;
    /** Set when listening://wake fires (wake word in transcript). */
    wakeHighlight?: boolean;
  } = $props();

  const focus = $derived(screenWatch?.last_event ?? null);
  const app = $derived(
    focus && typeof focus === "object" ? String((focus as Record<string, unknown>).app_name ?? "") : "",
  );
  const title = $derived(
    focus && typeof focus === "object" ? String((focus as Record<string, unknown>).window_title ?? "") : "",
  );
  const lastTs = $derived(
    focus && typeof focus === "object" ? Number((focus as Record<string, unknown>).ts ?? 0) : 0,
  );

  const macos = $derived(screenWatch?.macos_watch ?? null);
  const pollSec = $derived(macos?.poll_interval_sec ?? 5);
  const watcherOff = $derived(
    !screenWatch?.watcher_supported ||
      macos?.watchers_env_disabled === true ||
      status?.watcher?.running === false,
  );
  const micOn = $derived(
    screenWatch?.full_listening_active === true || screenWatch?.listening_active === true,
  );
  const recentlyHeard = $derived(lastTs > 0 && Date.now() / 1000 - lastTs < pollSec * 3);
  const listening = $derived(
    wakeHighlight || micOn || (!watcherOff && (recentlyHeard || livePulse || status?.watcher?.running === true)),
  );

  const collectors = $derived(screenWatch?.ambient_collectors ?? {});

  const streams = $derived.by(() => {
    const out: string[] = [];
    const c = collectors;
    if (c.window_focus !== false && screenWatch?.watcher_supported && !macos?.watchers_env_disabled) {
      out.push("Window focus");
    }
    if (c.ax_content_changed !== false && macos?.ax_text_sample_enabled) {
      out.push("Accessibility text");
    }
    if (c.process_snapshot) out.push("Processes");
    if (c.browser_visit) out.push("Browser hints");
    if (c.app_launched) out.push("App launches");
    if (c.screen_reader !== false) out.push("All windows");
    if (screenWatch?.full_listening_active || c.full_listening) out.push("Full mic");
    else if (screenWatch?.listening_active || c.listening) out.push("Mic session");
    if (c.screenshot_fallback !== false) {
      out.push("Screen capture + OCR");
    }
    return out;
  });

  function formatAgo(ts: number): string {
    if (!ts) return "";
    const sec = Math.max(0, Math.floor(Date.now() / 1000 - ts));
    if (sec < 60) return `${sec}s ago`;
    if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
    return new Date(ts * 1000).toLocaleTimeString();
  }
</script>

<div
  class="focus-banner"
  class:listening
  class:wake={wakeHighlight}
  class:paused={watcherOff && !micOn}
>
  <div class="focus-inner">
    <div class="listen-row">
      <span class="listen-dot" class:pulse={listening} class:wake-dot={wakeHighlight} aria-hidden="true"></span>
      <span class="listen-label">
        {#if wakeHighlight}
          Heard Minion — listening
        {:else if micOn}
          Mic capture on
        {:else if listening}
          Listening
        {:else if watcherOff}
          Capture paused
        {:else}
          Awaiting signal
        {/if}
      </span>
      {#if lastTs}
        <span class="listen-ago">Last signal {formatAgo(lastTs)}</span>
      {/if}
    </div>

    {#if streams.length}
      <div class="stream-chips">
        {#each streams as s}
          <span class="stream-chip">{s}</span>
        {/each}
      </div>
    {/if}

    <p class="focus-now">
      {#if app || title}
        <span class="focus-label">Right now</span>
        <span class="focus-detail">{app}{#if title} — {title}{/if}</span>
      {:else if listening}
        <span class="muted">Switch apps or windows — Minion logs focus for the activity river.</span>
      {:else}
        <span class="muted">
          Grant Accessibility (and optionally Screen Recording) in System Settings → Privacy if capture
          stays empty.
        </span>
      {/if}
    </p>
  </div>
</div>

<style>
  .focus-banner {
    border-bottom: 1px solid var(--border);
    background: var(--panel);
    padding: 0.55rem 1.25rem;
  }
  .focus-banner.listening {
    background: color-mix(in srgb, var(--accent-soft) 35%, var(--panel));
  }
  .focus-banner.wake {
    background: color-mix(in srgb, #c9a227 28%, var(--panel));
    animation: wake-flash 0.6s ease-out;
  }
  @keyframes wake-flash {
    from {
      background: color-mix(in srgb, #e8c547 55%, var(--panel));
    }
    to {
      background: color-mix(in srgb, #c9a227 28%, var(--panel));
    }
  }
  .focus-banner.paused {
    background: color-mix(in srgb, var(--panel-2) 40%, var(--panel));
  }
  .focus-inner {
    max-width: 100%;
  }
  .listen-row {
    display: flex;
    align-items: center;
    gap: 0.5rem;
    margin-bottom: 0.35rem;
    font-size: 0.72rem;
  }
  .listen-dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--muted);
    flex-shrink: 0;
  }
  .listen-dot.pulse {
    background: #3d8f62;
    box-shadow: 0 0 0 0 rgba(61, 143, 98, 0.45);
    animation: pulse 2.2s ease-out infinite;
  }
  .listen-dot.wake-dot {
    background: #c9a227;
    animation: pulse-wake 1.4s ease-out infinite;
  }
  @keyframes pulse {
    0% {
      box-shadow: 0 0 0 0 rgba(61, 143, 98, 0.4);
    }
    70% {
      box-shadow: 0 0 0 7px rgba(61, 143, 98, 0);
    }
    100% {
      box-shadow: 0 0 0 0 rgba(61, 143, 98, 0);
    }
  }
  @keyframes pulse-wake {
    0% {
      box-shadow: 0 0 0 0 rgba(201, 162, 39, 0.55);
    }
    70% {
      box-shadow: 0 0 0 9px rgba(201, 162, 39, 0);
    }
    100% {
      box-shadow: 0 0 0 0 rgba(201, 162, 39, 0);
    }
  }
  .listen-label {
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink);
    font-size: 0.65rem;
  }
  .listen-ago {
    margin-left: auto;
    font-size: 0.7rem;
    color: var(--muted);
    font-variant-numeric: tabular-nums;
  }
  .stream-chips {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem;
    margin-bottom: 0.35rem;
  }
  .stream-chip {
    font-size: 0.64rem;
    padding: 0.1rem 0.4rem;
    border-radius: 3px;
    background: transparent;
    color: var(--muted);
    border: 1px solid color-mix(in srgb, var(--border) 90%, transparent);
    letter-spacing: 0.02em;
  }
  .focus-now {
    margin: 0;
    font-size: 0.86rem;
    line-height: 1.4;
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem 0.5rem;
    align-items: baseline;
  }
  .focus-label {
    font-size: 0.62rem;
    font-weight: 600;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--accent);
  }
  .focus-detail {
    color: var(--ink);
    font-weight: 500;
  }
  .muted {
    color: var(--muted);
  }
</style>
