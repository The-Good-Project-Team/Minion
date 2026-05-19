<script lang="ts">
  import { onMount } from "svelte";
  import {
    fetchIdentityClaims,
    fetchIdentityMirror,
    patchIdentityClaim,
    screenContextStatus,
    type IdentityClaim,
    type ScreenContextStatus,
  } from "$lib/api";

  let tab = $state<"mirror" | "proposed" | "active">("mirror");
  let mirrorMd = $state("");
  let history = $state<IdentityClaim[]>([]);
  let claims = $state<IdentityClaim[]>([]);
  let screenWatch = $state<ScreenContextStatus | null>(null);

  async function refresh() {
    if (tab === "mirror") {
      const r = await fetchIdentityMirror({ limit_history: 40 });
      mirrorMd = r.markdown;
      history = r.history;
    } else {
      claims = (await fetchIdentityClaims({ status: tab })).claims;
    }
    screenWatch = await screenContextStatus();
  }

  async function approve(id: string) {
    await patchIdentityClaim(id, { status: "active" });
    await refresh();
  }

  async function reject(id: string) {
    await patchIdentityClaim(id, { status: "rejected" });
    await refresh();
  }

  onMount(refresh);
</script>

<h1 style="margin:0 0 1rem;">Mirror</h1>

<div style="display:flex;gap:0.5rem;margin-bottom:1rem;">
  <button type="button" class="btn" class:btn-primary={tab === "mirror"} onclick={() => { tab = "mirror"; void refresh(); }}>Mirror</button>
  <button type="button" class="btn" class:btn-primary={tab === "proposed"} onclick={() => { tab = "proposed"; void refresh(); }}>Proposed</button>
  <button type="button" class="btn" class:btn-primary={tab === "active"} onclick={() => { tab = "active"; void refresh(); }}>Active</button>
</div>

{#if tab === "mirror"}
  <div class="card">
    <pre class="mono" style="white-space:pre-wrap;margin:0;">{mirrorMd || "Nothing yet."}</pre>
  </div>
  {#if screenWatch?.macos_watch}
    <div class="card muted">
      <h3>Screen context</h3>
      <p>AX sampling: {screenWatch.macos_watch.ax_text_sample_enabled ? "on" : "off"}</p>
    </div>
  {/if}
  {#if history.length}
    <div class="card">
      <h3>Revision history</h3>
      <ul>
        {#each history as c}
          <li>{c.kind} — {c.status}: {c.text.slice(0, 120)}</li>
        {/each}
      </ul>
    </div>
  {/if}
{:else}
  {#each claims as c}
    <div class="card">
      <p><strong>{c.kind}</strong> — {c.text}</p>
      {#if tab === "proposed"}
        <div style="display:flex;gap:0.5rem;margin-top:0.5rem;">
          <button type="button" class="btn btn-primary" onclick={() => void approve(c.claim_id)}>Approve</button>
          <button type="button" class="btn" onclick={() => void reject(c.claim_id)}>Reject</button>
        </div>
      {/if}
    </div>
  {:else}
    <div class="card"><p class="muted">No claims.</p></div>
  {/each}
{/if}

<style>
  .btn-primary {
    background: var(--accent);
    color: white;
    border-color: var(--accent);
  }
</style>
