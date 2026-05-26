<script lang="ts">
  import type { GraphScaffoldResponse } from "$lib/api";

  let { graph = null }: { graph?: GraphScaffoldResponse | null } = $props();

  const highlights = $derived(graph?.highlights ?? []);
  const total = $derived(graph?.user_node_count ?? 0);
  const ft = $derived(graph?.forty_two);
</script>

<nav class="graph-panel" aria-label="Life graph">
  <div class="graph-head">
    <span class="graph-count">{total}</span>
    <span class="graph-label">on your graph</span>
  </div>

  {#if ft?.active_thread_id && ft?.question_preview}
    <a class="graph-cta" href="/">{ft.question_preview}</a>
  {/if}

  {#if highlights.length}
    <ul class="graph-list">
      {#each highlights.slice(0, 6) as h (h.node_id)}
        <li>
          <span class="k">{h.node_kind}</span>
          <span class="t">{h.title}</span>
        </li>
      {/each}
    </ul>
  {/if}
</nav>

<style>
  .graph-panel {
    margin-top: 1rem;
    padding-top: 0.85rem;
    border-top: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    gap: 0.45rem;
    max-height: 40vh;
    overflow-y: auto;
  }
  .graph-head {
    font-size: 0.75rem;
    color: var(--muted);
  }
  .graph-count {
    font-weight: 600;
    color: var(--ink);
    font-size: 0.95rem;
  }
  .graph-cta {
    font-size: 0.75rem;
    line-height: 1.3;
    padding: 0.35rem 0.45rem;
    border-radius: var(--radius);
    background: var(--accent-soft);
    color: var(--accent);
    text-decoration: none;
    display: block;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .graph-list {
    list-style: none;
    margin: 0;
    padding: 0;
    font-size: 0.72rem;
  }
  .graph-list li {
    display: flex;
    gap: 0.35rem;
    padding: 0.2rem 0;
    line-height: 1.3;
  }
  .k {
    text-transform: uppercase;
    font-size: 0.62rem;
    color: var(--muted);
    flex-shrink: 0;
  }
  .t {
    color: var(--ink);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
</style>
