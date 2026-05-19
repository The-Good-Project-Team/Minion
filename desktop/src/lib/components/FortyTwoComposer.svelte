<script lang="ts">
  import { fortyTwoDismiss, fortyTwoNext, fortyTwoReply } from "$lib/api";

  let {
    activeThreadId = null,
    needsQuestion = false,
    onRefresh,
  }: {
    activeThreadId?: string | null;
    needsQuestion?: boolean;
    onRefresh?: () => void | Promise<void>;
  } = $props();

  let draft = $state("");
  let busy = $state(false);
  let err = $state<string | null>(null);

  $effect(() => {
    if (needsQuestion && !busy) {
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

  async function send() {
    const text = draft.trim();
    if (!text && !activeThreadId) return;
    busy = true;
    err = null;
    try {
      await fortyTwoReply(text, activeThreadId ?? undefined);
      draft = "";
      await onRefresh?.();
    } catch (e) {
      err = e instanceof Error ? e.message : String(e);
    } finally {
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
  {#if err}
    <p class="composer-err">{err}</p>
  {/if}
  <div class="composer-row">
    <span class="composer-label" aria-hidden="true">42</span>
    <textarea
      bind:value={draft}
      rows="2"
      placeholder={activeThreadId
        ? "Answer 42 — fills your life graph…"
        : "42 finds the next empty spot on your graph…"}
      disabled={busy || !activeThreadId}
      onkeydown={onKeydown}
    ></textarea>
    <button
      type="button"
      class="btn btn-send"
      disabled={busy || !draft.trim() || !activeThreadId}
      onclick={() => void send()}
    >
      {busy ? "…" : "Send"}
    </button>
  </div>
</div>

<style>
  .composer {
    position: sticky;
    bottom: 0;
    margin-top: 0.75rem;
    padding: 0.65rem 0.75rem;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: var(--shadow-s);
  }
  .composer-row {
    display: flex;
    gap: 0.5rem;
    align-items: flex-end;
  }
  .composer-label {
    flex-shrink: 0;
    width: 2rem;
    height: 2rem;
    display: flex;
    align-items: center;
    justify-content: center;
    font-family: var(--font-display);
    font-size: 1.1rem;
    font-weight: 600;
    color: var(--accent);
    border: 1px solid color-mix(in srgb, var(--accent) 35%, var(--border));
    border-radius: var(--radius);
    background: var(--accent-soft);
  }
  .composer-row textarea {
    flex: 1;
    font-family: inherit;
    font-size: 0.88rem;
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 0.45rem 0.55rem;
    resize: none;
    background: var(--bg);
    color: var(--ink);
    min-height: 2.5rem;
  }
  .composer-row textarea:focus {
    outline: none;
    border-color: var(--accent);
  }
  .btn-send {
    flex-shrink: 0;
    background: var(--accent);
    color: white;
    border-color: var(--accent);
    font-size: 0.8rem;
    padding: 0.45rem 0.75rem;
  }
  .composer-err {
    font-size: 0.78rem;
    color: var(--danger);
    margin: 0 0 0.35rem;
  }
</style>
