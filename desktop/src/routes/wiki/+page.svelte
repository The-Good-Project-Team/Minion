<script lang="ts">
  import { onMount } from "svelte";
  import { fetchWikiPages, type WikiPage } from "$lib/api";

  let pages = $state<WikiPage[]>([]);
  let filter = $state("");

  async function load() {
    const r = await fetchWikiPages({ q: filter || undefined, limit: 100 });
    pages = r.pages;
  }

  onMount(load);
</script>

<h1 style="margin:0 0 1rem;">Wiki</h1>

<div style="margin-bottom:1rem;display:flex;gap:0.5rem;">
  <input bind:value={filter} placeholder="Search titles…" style="flex:1;padding:0.4rem 0.6rem;border-radius:8px;border:1px solid var(--border);" />
  <button type="button" class="btn" onclick={() => void load()}>Search</button>
</div>

{#each pages as p (p.page_id)}
  <a class="card" href="/wiki/{p.page_id}" style="display:block;color:inherit;">
    <span class="muted">{p.page_type}</span>
    <h3 style="margin:0.2rem 0;">{p.title}</h3>
    <p class="muted">{(p.body_md || "").slice(0, 160)}</p>
  </a>
{:else}
  <div class="card"><p class="muted">No wiki pages yet — agents propose updates via MCP.</p></div>
{/each}
