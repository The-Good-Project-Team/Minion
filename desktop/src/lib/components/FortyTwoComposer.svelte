<script lang="ts">
  import { fortyTwoNext, openFortyTwoReplyStream } from "$lib/api";

  let {
    activeThreadId = null,
    needsQuestion = false,
    suggestions = [],
    onRefresh,
    streaming = $bindable(false),
    streamBuf = $bindable(""),
  }: {
    activeThreadId?: string | null;
    needsQuestion?: boolean;
    suggestions?: import("$lib/api").FortyTwoSuggestion[];
    onRefresh?: () => void | Promise<void>;
    streaming?: boolean;
    streamBuf?: string;
  } = $props();

  let draft = $state("");
  let busy = $state(false);
  let err = $state<string | null>(null);

  $effect(() => {
    if (needsQuestion && !busy && !streaming) {
      void poke42();
    }
  });

  async function poke42() {
    busy = true;
    err = null;
    try {
      await fortyTwoNext();
      await onRefresh?.();
    } catch (e) {
      err = e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }

  async function pick(name: string) {
    if (!activeThreadId || busy || streaming) return;
    draft = name;
    await send();
  }

  async function send() {
    const text = draft.trim();
    if (!text || !activeThreadId) return;
    busy = true;
    streaming = true;
    streamBuf = "";
    err = null;
    const tid = activeThreadId;
    draft = "";

    const stream = openFortyTwoReplyStream(text, tid, undefined, {
      onDelta: (d) => {
        streamBuf += d;
      },
      onDone: async () => {
        streaming = false;
        streamBuf = "";
        await onRefresh?.();
      },
      onError: (m) => {
        err = m;
        streaming = false;
        streamBuf = "";
      },
    });
    try {
      await stream.finished;
    } finally {
      stream.cancel();
      busy = false;
    }
  }

  function onKeydown(e: KeyboardEvent) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  }
</script>

<div class="composer">
  {#if err}<p class="composer-err">{err}</p>{/if}
  {#if suggestions.length > 0 && activeThreadId && !busy && !streaming}
    <div class="chips">
      {#each suggestions as s (s.name)}
        <button type="button" class="chip" onclick={() => void pick(s.name)}>{s.name}</button>
      {/each}
    </div>
  {/if}
  <div class="row">
    <textarea
      bind:value={draft}
      rows="2"
      placeholder={activeThreadId ? "Message #life" : "Message #life"}
      disabled={busy || streaming || !activeThreadId}
      onkeydown={onKeydown}
    ></textarea>
    <button type="button" class="btn-send" disabled={busy || streaming || !draft.trim() || !activeThreadId} onclick={() => void send()}>
      Send
    </button>
  </div>
</div>

<style>
  .composer {
    position: sticky;
    bottom: 0;
    margin-top: 0.5rem;
    padding-top: 0.5rem;
    border-top: 1px solid var(--border);
    background: var(--bg);
  }
  .chips {
    display: flex;
    flex-wrap: wrap;
    gap: 0.3rem;
    margin-bottom: 0.4rem;
  }
  .chip {
    font-size: 0.75rem;
    padding: 0.15rem 0.5rem;
    border-radius: 999px;
    border: 1px solid var(--border);
    background: var(--panel);
    cursor: pointer;
  }
  .row {
    display: flex;
    gap: 0.45rem;
    align-items: flex-end;
  }
  textarea {
    flex: 1;
    font: inherit;
    font-size: 0.88rem;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 0.4rem 0.5rem;
    resize: none;
  }
  .btn-send {
    background: var(--accent);
    color: white;
    border: none;
    border-radius: var(--radius);
    padding: 0.45rem 0.7rem;
    font-size: 0.8rem;
  }
  .composer-err {
    color: var(--danger);
    font-size: 0.78rem;
    margin: 0 0 0.35rem;
  }
</style>