<script lang="ts">
  import { onMount } from "svelte";
  import { open as openDialog } from "@tauri-apps/plugin-dialog";
  import {
    copyIntoInbox,
    fetchSources,
    search,
    type SearchHit,
    type Source,
  } from "$lib/api";

  let sources = $state<Source[]>([]);
  let query = $state("");
  let hits = $state<SearchHit[]>([]);
  let showLog = $state(false);
  let logLines = $state<string[]>([]);

  function pushLog(line: string) {
    logLines = [...logLines.slice(-80), line];
  }

  async function refreshSources() {
    sources = (await fetchSources({ limit: 200 })).sources;
  }

  async function doSearch() {
    if (!query.trim()) return;
    hits = (await search({ query: query.trim(), top_k: 12 })).results;
  }

  async function browse() {
    const picked = await openDialog({ multiple: true });
    if (!picked) return;
    const paths = Array.isArray(picked) ? picked : [picked];
    for (const p of paths) {
      pushLog(`Copying ${p}…`);
      await copyIntoInbox([p]);
    }
    await refreshSources();
  }

  onMount(refreshSources);
</script>

<h1 style="margin:0 0 1rem;">Sources</h1>

<div class="card">
  <h2>Live capture</h2>
  <p class="muted">
    Your desk is the primary source: window focus, all visible apps, browser page text, and optional mic.
    Configure collectors and permissions in <a href="/settings">Settings</a>. Activity shows what was captured.
  </p>
</div>

<div class="card">
  <h2>Optional file imports</h2>
  <p class="muted">Drop exports and files here — indexing runs in the background.</p>
  <button type="button" class="btn btn-primary" onclick={() => void browse()}>Choose files…</button>
</div>

<div class="card">
  <h2>Search vault</h2>
  <div style="display:flex;gap:0.5rem;margin-bottom:0.75rem;">
    <input bind:value={query} placeholder="Semantic search…" style="flex:1;padding:0.4rem;border:1px solid var(--border);border-radius:8px;" />
    <button type="button" class="btn" onclick={() => void doSearch()}>Search</button>
  </div>
  {#each hits as h}
    <div style="margin-bottom:0.5rem;font-size:0.85rem;">
      <span class="mono">{h.path}</span>
      <div class="muted">{(h.text || "").slice(0, 140)}</div>
    </div>
  {/each}
</div>

<div class="card">
  <button type="button" class="btn btn-ghost" onclick={() => (showLog = !showLog)}>
    {showLog ? "Hide" : "Show"} activity log
  </button>
  {#if showLog}
    <pre class="mono" style="max-height:200px;overflow:auto;margin-top:0.5rem;">{logLines.join("\n") || "No events."}</pre>
  {/if}
</div>

<div class="card">
  <h2>Indexed ({sources.length})</h2>
  <ul style="max-height:280px;overflow:auto;padding-left:1.2rem;font-size:0.85rem;">
    {#each sources as s}
      <li><span class="muted">{s.kind}</span> {s.path}</li>
    {/each}
  </ul>
</div>

<style>
  .btn-primary {
    background: var(--accent);
    color: white;
    border-color: var(--accent);
  }
</style>
