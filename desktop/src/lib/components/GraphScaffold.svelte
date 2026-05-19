<script lang="ts">
  import type { GraphScaffoldNode } from "$lib/api";

  let { root = null }: { root?: GraphScaffoldNode | null } = $props();

  function branch(node: GraphScaffoldNode, depth = 0): GraphScaffoldNode[] {
    const rows: GraphScaffoldNode[] = [{ ...node, depth }];
    for (const ch of node.children ?? []) {
      rows.push(...branch(ch, depth + 1));
    }
    return rows;
  }

  const rows = $derived(root ? branch(root) : []);
</script>

<div class="graph-panel">
  <h2 class="graph-title">Your graph</h2>
  <p class="graph-sub muted">Pre-generated life map — sources fill it with evidence.</p>
  {#if !root}
    <p class="muted">Loading graph…</p>
  {:else}
    <ul class="graph-tree">
      {#each rows as row (row.node_id)}
        <li
          class="graph-node"
          class:filled={(row.filled_count ?? 0) > 0}
          style="padding-left: {0.35 + (row.depth ?? 0) * 0.65}rem"
        >
          <span class="graph-label">{row.title}</span>
          <span class="graph-count">{(row.filled_count ?? 0) > 0 ? row.filled_count : "—"}</span>
          {#if row.summary && row.depth === 0}
            <span class="graph-hint muted">{row.summary}</span>
          {/if}
        </li>
      {/each}
    </ul>
  {/if}
</div>

<style>
  .graph-panel {
    margin-top: 1.25rem;
    padding-top: 1rem;
    border-top: 1px solid var(--border);
    max-height: 52vh;
    overflow-y: auto;
  }
  .graph-title {
    margin: 0 0 0.25rem;
    font-size: 0.78rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    color: var(--muted);
  }
  .graph-sub {
    margin: 0 0 0.65rem;
    font-size: 0.72rem;
  }
  .graph-tree {
    list-style: none;
    margin: 0;
    padding: 0;
  }
  .graph-node {
    display: grid;
    grid-template-columns: 1fr auto;
    grid-template-rows: auto auto;
    gap: 0 0.5rem;
    padding: 0.28rem 0.5rem;
    border-radius: var(--radius);
    font-size: 0.8rem;
  }
  .graph-node.filled {
    background: var(--accent-soft);
  }
  .graph-label {
    font-weight: 600;
    color: var(--ink);
  }
  .graph-count {
    font-variant-numeric: tabular-nums;
    color: var(--muted);
    font-size: 0.72rem;
  }
  .graph-hint {
    grid-column: 1 / -1;
    font-size: 0.68rem;
  }
</style>
