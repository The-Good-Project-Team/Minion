<script lang="ts">
  import { onMount } from "svelte";
  import { fetchToday, resolveHealthIssue, type SystemIssue } from "$lib/api";

  let issues = $state<SystemIssue[]>([]);

  async function load() {
    try {
      const t = await fetchToday();
      issues = t.needs_attention ?? [];
    } catch {
      issues = [];
    }
  }

  async function dismiss(id: string) {
    await resolveHealthIssue(id);
    await load();
  }

  onMount(load);
</script>

{#if issues.length}
  <div class="alert-strip">
    {#each issues as issue (issue.issue_id)}
      <div style="display:flex;gap:0.75rem;align-items:center;margin:0.25rem 0;">
        <span>{issue.body_md}</span>
        <button type="button" class="btn btn-ghost" onclick={() => void dismiss(issue.issue_id)}>Dismiss</button>
      </div>
    {/each}
  </div>
{/if}
