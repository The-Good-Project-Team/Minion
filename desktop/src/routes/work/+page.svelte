<script lang="ts">
  import { onMount } from "svelte";
  import { fetchTasks, patchTask, type WorkItem } from "$lib/api";

  let items = $state<WorkItem[]>([]);

  async function load() {
    const r = await fetchTasks({ limit: 50 });
    items = r.tasks.filter(
      (t) =>
        (t.origin === "inferred" || t.origin === "agent") &&
        t.status !== "archived" &&
        t.status !== "done",
    );
  }

  async function accept(task: WorkItem) {
    await patchTask(task.task_id, { status: "open" });
    await load();
  }

  async function dismiss(task: WorkItem) {
    await patchTask(task.task_id, { status: "archived" });
    await load();
  }

  onMount(load);
</script>

<h1 style="margin:0 0 1rem;">Work</h1>
<p class="muted" style="margin-bottom:1rem;">Inferred items for review — accept to pin, dismiss to archive.</p>

{#if items.length === 0}
  <div class="card"><p class="muted">Nothing inferred yet.</p></div>
{:else}
  {#each items as task (task.task_id)}
    <div class="card">
      <h3 style="margin:0 0 0.35rem;">{task.title}</h3>
      <p class="muted">{task.body_md || task.origin}</p>
      <div style="display:flex;gap:0.5rem;margin-top:0.75rem;">
        <button type="button" class="btn btn-primary" onclick={() => void accept(task)}>Accept</button>
        <button type="button" class="btn" onclick={() => void dismiss(task)}>Dismiss</button>
      </div>
    </div>
  {/each}
{/if}
