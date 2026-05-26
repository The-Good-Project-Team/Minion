<script lang="ts">
  import LifeChannel from "$lib/components/LifeChannel.svelte";
  import type { FeedRow } from "$lib/api";

  let {
    items = [],
    onRefresh,
    streamingPreview = "",
    streaming = false,
  }: {
    now?: unknown;
    prefetch?: unknown;
    items?: FeedRow[];
    onRefresh?: () => void | Promise<void>;
    streamingPreview?: string;
    streaming?: boolean;
  } = $props();
</script>

<LifeChannel {items} {onRefresh} {streamingPreview} {streaming} />
