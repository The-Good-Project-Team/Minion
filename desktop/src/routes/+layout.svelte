<script lang="ts">
  import "../app.css";
  import { onDestroy, onMount, setContext } from "svelte";
  import { page } from "$app/stores";
  import FocusBanner from "$lib/components/FocusBanner.svelte";
  import GraphScaffold from "$lib/components/GraphScaffold.svelte";
  import { listen } from "@tauri-apps/api/event";
  import {
    fetchGraphScaffold,
    fetchStatus,
    getConfig,
    onSidecarStatus,
    openEvents,
    screenContextStatus,
    type ConnState,
    type GraphScaffoldNode,
    type ScreenContextStatus,
    type SidecarStatus,
    type Status,
  } from "$lib/api";

  const APP_CTX = "minion-app";

  let conn = $state<ConnState>("connecting");
  let status = $state<Status | null>(null);
  let sidecar = $state<SidecarStatus | null>(null);
  let screenWatch = $state<ScreenContextStatus | null>(null);
  let graphRoot = $state<GraphScaffoldNode | null>(null);
  let screenLive = $state(false);
  let wakeHighlight = $state(false);

  setContext(APP_CTX, {
    get conn() {
      return conn;
    },
    get status() {
      return status;
    },
    set status(v: Status | null) {
      status = v;
    },
  });

  const nav = [
    { href: "/activity", label: "Activity" },
    { href: "/sources", label: "Sources" },
    { href: "/settings", label: "Settings" },
  ];

  let closeWs: (() => void) | null = null;
  let hbTimer: ReturnType<typeof setInterval> | null = null;

  async function refreshStatus() {
    try {
      status = await fetchStatus();
      conn = "open";
    } catch {
      conn = "closed";
    }
  }

  async function refreshScreen() {
    try {
      screenWatch = await screenContextStatus();
    } catch {
      screenWatch = null;
    }
  }

  async function refreshGraph() {
    try {
      const g = await fetchGraphScaffold();
      graphRoot = g.root;
    } catch {
      graphRoot = null;
    }
  }

  onMount(() => {
    let unlistenSidecar: (() => void) | null = null;
    let unlistenScreen: (() => void) | null = null;
    let unlistenWake: (() => void) | null = null;
    (async () => {
      sidecar = { state: "starting", message: "Starting Minion…" };
      unlistenSidecar = await onSidecarStatus((s) => {
        sidecar = s;
      });
      const cfg = await getConfig();
      if (cfg.sidecar_bootstrapped && cfg.sidecar_running) {
        sidecar = { state: "ready" };
      }
      await refreshStatus();
      await refreshGraph();
      closeWs = await openEvents(async (msg) => {
        if (msg.type === "heartbeat" && status) {
          status = { ...status, counts: msg.counts };
        }
        if (msg.type === "snapshot" || msg.type === "ready") {
          await refreshStatus();
          await refreshGraph();
        }
      });
      hbTimer = setInterval(refreshScreen, 4000);
      await refreshScreen();
      try {
        unlistenScreen = await listen("screen-context://update", () => {
          screenLive = true;
          void refreshScreen();
          setTimeout(() => {
            screenLive = false;
          }, 2500);
        });
      } catch {
        /* browser-only dev without Tauri */
      }
      try {
        unlistenWake = await listen<{ excerpt?: string; ts?: number }>("listening://wake", () => {
          wakeHighlight = true;
          void refreshScreen();
          setTimeout(() => {
            wakeHighlight = false;
          }, 4500);
        });
      } catch {
        /* browser-only dev without Tauri */
      }
    })();

    return () => {
      unlistenSidecar?.();
      unlistenScreen?.();
      unlistenWake?.();
    };
  });

  onDestroy(() => {
    closeWs?.();
    if (hbTimer) clearInterval(hbTimer);
  });

  function isActive(href: string): boolean {
    return $page.url.pathname === href || $page.url.pathname.startsWith(href + "/");
  }
</script>

<svelte:head>
  <title>Minion</title>
  <link rel="preconnect" href="https://fonts.googleapis.com" />
  <link
    rel="stylesheet"
    href="https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=Instrument+Serif:ital@0;1&display=swap"
  />
</svelte:head>

{#if sidecar && sidecar.state !== "ready"}
  <div class="bootstrap-overlay">
    <div class="card" style="max-width:420px;margin:4rem auto;">
      <h2>{sidecar.state === "error" ? "Minion can't start" : "Setting up Minion"}</h2>
      <p class="muted">{sidecar.message ?? "Working…"}</p>
    </div>
  </div>
{/if}

<div class="shell">
  <aside class="shell-nav">
    <div class="shell-brand">
      <img src="/minion.png" alt="" />
      <span>Minion</span>
    </div>
    {#each nav as item}
      <a class="nav-link" class:active={isActive(item.href)} href={item.href}>{item.label}</a>
    {/each}
    <GraphScaffold root={graphRoot} />
    <div class="muted" style="margin-top:auto;padding:0.5rem 0.75rem;font-size:0.75rem;">
      {conn === "open" ? "Sidecar connected" : "Sidecar…"}
    </div>
  </aside>
  <div class="shell-main">
    <FocusBanner {screenWatch} {status} livePulse={screenLive} {wakeHighlight} />
    <div class="shell-content">
      <slot />
    </div>
  </div>
</div>

<style>
  .bootstrap-overlay {
    position: fixed;
    inset: 0;
    background: rgba(243, 246, 251, 0.92);
    z-index: 100;
  }
</style>
