<script lang="ts">
  import { onMount } from "svelte";
  import { page } from "$app/stores";
  import { fetchWikiPage, patchWikiPage, type WikiPage } from "$lib/api";

  let wikiPage = $state<WikiPage | null>(null);
  let links = $state<unknown[]>([]);

  const pageId = $derived($page.params.pageId ?? "");

  async function load(id: string) {
    const r = await fetchWikiPage(id);
    wikiPage = r.page;
    links = r.links;
  }

  async function approve() {
    if (!wikiPage) return;
    await patchWikiPage(wikiPage.page_id, { status: "active" });
    await load(wikiPage.page_id);
  }

  $effect(() => {
    if (pageId) void load(pageId);
  });
</script>

{#if wikiPage}
  <a href="/wiki" class="muted">← Wiki</a>
  <h1 style="margin:0.5rem 0 1rem;">{wikiPage.title}</h1>
  <p class="muted">{wikiPage.page_type} · {wikiPage.status}</p>
  {#if wikiPage.status === "proposed"}
    <button type="button" class="btn btn-primary" style="margin-bottom:1rem;" onclick={() => void approve()}>Approve</button>
  {/if}
  <div class="card">
    <pre class="mono" style="white-space:pre-wrap;margin:0;">{wikiPage.body_md}</pre>
  </div>
{:else}
  <p class="muted">Loading…</p>
{/if}

<style>
  .btn-primary {
    background: var(--accent);
    color: white;
    border-color: var(--accent);
  }
</style>
