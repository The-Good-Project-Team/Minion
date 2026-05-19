<script lang="ts">
  import { onMount } from "svelte";
  import { goto } from "$app/navigation";

  onMount(() => {
    goto("/activity", { replaceState: true });
  });
</script>

<p class="muted">Loading…</p>
