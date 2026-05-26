<script lang="ts">
  import "../app.css";
  import { onDestroy, onMount, setContext } from "svelte";
  import { page } from "$app/stores";
  import FocusBanner from "$lib/components/FocusBanner.svelte";
  import GraphScaffold from "$lib/components/GraphScaffold.svelte";
  import { listen } from "$lib/tauri-bridge";
  import {
    fetchGraphScaffold,
    fetchMenuStatus,
    fetchStatus,
    getConfig,
    onSidecarStatus,
    openEvents,
    screenContextStatus,
    type ConnState,
    type MenuStatusResponse,
    type ScreenContextStatus,
    type SidecarStatus,
    type Status,
  } from "$lib/api";

  const APP_CTX = "minion-app";

  let conn = $state<ConnState>("connecting");
  let status = $state<Status | null>(null);
  let sidecar = $state<SidecarStatus | null>(null);
  let screenWatch = $state<ScreenContextStatus | null>(null);
  let graphData = $state<import("$lib/api").GraphScaffoldResponse | null>(null);
  let menuStatus = $state<MenuStatusResponse | null>(null);
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
      graphData = await fetchGraphScaffold();
    } catch {
      graphData = null;
    }
  }

  async function refreshMenu() {
    try {
      menuStatus = await fetchMenuStatus();
    } catch {
      menuStatus = null;
    }
  }

  onMount(() => {
    let unlistenSidecar: (() => void) | null = null;
    let unlistenScreen: (() => void) | null = null;
    let unlistenWake: (() => void) | null = null;
    let unlistenMenu: (() => void) | null = null;
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
      await refreshMenu();
      closeWs = await openEvents(async (msg) => {
        if (msg.type === "heartbeat" && status) {
          status = { ...status, counts: msg.counts };
        }
        if (msg.type === "snapshot" || msg.type === "ready") {
          await refreshStatus();
          await refreshGraph();
          await refreshMenu();
        }
        if (msg.type === "chat_updated") {
          await refreshGraph();
          await refreshMenu();
          window.dispatchEvent(new CustomEvent("minion:chat_updated", { detail: msg }));
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
        unlistenMenu = await listen<MenuStatusResponse>("menu://status", (ev) => {
          menuStatus = ev.payload;
          window.dispatchEvent(new CustomEvent("minion:menu_status", { detail: ev.payload }));
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
      unlistenMenu?.();
    };
  });

  onDestroy(() => {
    closeWs?.();
    if (hbTimer) clearInterval(hbTimer);
  });

  function isHome(): boolean {
    const p = $page.url.pathname;
    return p === "/" || p === "/activity" || p === "/chat";
  }

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
    <a class="shell-brand" class:active={isHome()} href="/">
      <img src="/minion.png" alt="" />
      <span>Minion</span>
    </a>
    {#each nav as item}
      <a class="nav-link" class:active={isActive(item.href)} href={item.href}>{item.label}</a>
    {/each}
    <GraphScaffold graph={graphData} />
  </aside>
  <div class="shell-main">
    {#if menuStatus?.should_notify && menuStatus.next_question}
      <div class="question-bar">
        <div class="question-copy">
          <strong>{menuStatus.next_question.title}</strong>
          {#if menuStatus.next_question.body && menuStatus.next_question.body !== menuStatus.next_question.title}
            <span>{menuStatus.next_question.body}</span>
          {/if}
        </div>
        <a class="question-link" href="/">Open</a>
      </div>
    {/if}
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
  .question-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 0.8rem;
    padding: 0.55rem 0.8rem;
    border-bottom: 1px solid var(--border);
    background: var(--panel);
  }
  .question-copy {
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 0.12rem;
    font-size: 0.82rem;
    line-height: 1.25;
  }
  .question-copy strong,
  .question-copy span {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .question-copy span {
    color: var(--muted);
  }
  .question-link {
    flex-shrink: 0;
    padding: 0.28rem 0.58rem;
    border: 1px solid var(--accent);
    border-radius: var(--radius);
    color: var(--accent);
    font-size: 0.78rem;
    font-weight: 600;
    text-decoration: none;
  }
</style>
