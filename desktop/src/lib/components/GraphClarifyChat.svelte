<script lang="ts">
  import {
    chatNextThread,
    chatReply,
    fetchChatThread,
    fetchChatThreads,
    type ChatThread,
  } from "$lib/api";

  let threads = $state<ChatThread[]>([]);
  let active = $state<ChatThread | null>(null);
  let reply = $state("");
  let err = $state<string | null>(null);
  let busy = $state(false);

  export async function load() {
    try {
      const t = await fetchChatThreads("open");
      threads = t.threads;
      if (active) {
        active = await fetchChatThread(active.thread_id);
      }
      err = null;
    } catch (e) {
      err = e instanceof Error ? e.message : String(e);
    }
  }

  async function openThread(id: string) {
    active = await fetchChatThread(id);
  }

  async function nextQuestion() {
    busy = true;
    try {
      const r = await chatNextThread();
      if (r.thread) {
        active = r.thread;
        await load();
      }
    } catch (e) {
      err = e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }

  async function send(action?: string) {
    if (!active) return;
    busy = true;
    try {
      await chatReply(active.thread_id, reply, action);
      reply = "";
      await load();
      active = threads[0] ? await fetchChatThread(threads[0].thread_id) : null;
    } catch (e) {
      err = e instanceof Error ? e.message : String(e);
    } finally {
      busy = false;
    }
  }

  type CorpusHit = { path?: string; text?: string };

  function corpusHits(meta: Record<string, unknown> | undefined): CorpusHit[] {
    if (!meta) return [];
    const direct = meta.corpus_hits;
    if (Array.isArray(direct)) return direct as CorpusHit[];
    const nested = (meta as { hits?: CorpusHit[] }).hits;
    return Array.isArray(nested) ? nested : [];
  }

  load();
</script>

<div class="clarify-panel">
  <header class="clarify-head">
    <div>
      <p class="clarify-eyebrow">Graph</p>
      <h2>Clarify</h2>
    </div>
    <button type="button" class="btn btn-next" disabled={busy} onclick={() => void nextQuestion()}>
      {busy ? "…" : "Next"}
    </button>
  </header>
  <p class="clarify-lede">Short questions about your graph — answers use your notes when possible.</p>

  {#if err}
    <p class="clarify-err">{err}</p>
  {/if}

  {#if threads.length}
    <div class="thread-tabs" role="tablist">
      {#each threads as t (t.thread_id)}
        <button
          type="button"
          class="thread-tab"
          class:active={active?.thread_id === t.thread_id}
          role="tab"
          aria-selected={active?.thread_id === t.thread_id}
          onclick={() => void openThread(t.thread_id)}
        >
          {t.topic || "Thread"}
        </button>
      {/each}
    </div>
  {/if}

  <section class="chat-pane">
    {#if active}
      <div class="messages">
        {#each active.messages ?? [] as m, i (m.message_id)}
          <article
            class="msg"
            class:user={m.role === "user"}
            class:assistant={m.role === "assistant"}
            style="--msg-i: {i}"
          >
            <div class="msg-role">{m.role}</div>
            <div class="msg-body">{@html m.body_md.replace(/\n/g, "<br />")}</div>
            {#if corpusHits(m.meta).length}
              <details class="corpus">
                <summary>From your notes</summary>
                <ul>
                  {#each corpusHits(m.meta) as h (h.path)}
                    <li><span class="mono">{h.path ?? "note"}</span> — {(h.text ?? "").slice(0, 100)}</li>
                  {/each}
                </ul>
              </details>
            {/if}
          </article>
        {/each}
      </div>
      <div class="reply-box">
        <textarea bind:value={reply} rows="3" placeholder="Your answer…"></textarea>
        <div class="reply-actions">
          <button type="button" class="btn btn-primary" disabled={busy} onclick={() => void send("approve")}>
            Send
          </button>
          <button type="button" class="btn btn-dismiss" disabled={busy} onclick={() => void send("reject")}>
            Dismiss
          </button>
        </div>
      </div>
    {:else}
      <p class="chat-empty muted">
        No open clarifications. Tap <strong>Next</strong> when Minion has a question.
      </p>
    {/if}
  </section>
</div>

<style>
  .clarify-panel {
    display: flex;
    flex-direction: column;
    min-height: 0;
    height: 100%;
    padding: 1rem 1.05rem;
    background: var(--panel);
    border: 1px solid var(--border);
    border-radius: var(--radius);
    box-shadow: var(--shadow-s);
  }
  .clarify-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 0.75rem;
    margin-bottom: 0.35rem;
  }
  .clarify-eyebrow {
    margin: 0 0 0.15rem;
    font-size: 0.62rem;
    font-weight: 600;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--muted);
  }
  .clarify-head h2 {
    margin: 0;
    font-family: var(--font-display);
    font-size: 1.35rem;
    font-weight: 400;
    line-height: 1.1;
  }
  .btn-next {
    flex-shrink: 0;
    font-size: 0.78rem;
    padding: 0.3rem 0.65rem;
    background: var(--accent);
    color: white;
    border-color: var(--accent);
    transition: opacity 0.15s ease;
  }
  .btn-next:hover:not(:disabled) {
    opacity: 0.9;
  }
  .btn-next:disabled {
    opacity: 0.55;
    cursor: wait;
  }
  .clarify-lede {
    margin: 0 0 0.65rem;
    font-size: 0.78rem;
    line-height: 1.4;
    color: var(--muted);
  }
  .clarify-err {
    font-size: 0.8rem;
    margin: 0 0 0.5rem;
    color: var(--danger);
  }
  .thread-tabs {
    display: flex;
    flex-wrap: wrap;
    gap: 0.35rem;
    margin-bottom: 0.65rem;
    padding-bottom: 0.65rem;
    border-bottom: 1px solid var(--border);
  }
  .thread-tab {
    font-size: 0.72rem;
    padding: 0.22rem 0.5rem;
    border-radius: 3px;
    border: 1px solid var(--border);
    background: transparent;
    color: var(--muted);
    cursor: pointer;
    max-width: 100%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    transition:
      color 0.15s ease,
      border-color 0.15s ease,
      background 0.15s ease;
  }
  .thread-tab:hover {
    color: var(--ink);
    border-color: color-mix(in srgb, var(--accent) 40%, var(--border));
  }
  .thread-tab.active {
    color: var(--accent);
    background: var(--accent-soft);
    border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
    font-weight: 500;
  }
  .chat-pane {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }
  .messages {
    flex: 1;
    overflow-y: auto;
    display: flex;
    flex-direction: column;
    gap: 0.55rem;
    margin-bottom: 0.65rem;
    min-height: 140px;
    padding-right: 0.15rem;
  }
  .msg {
    padding: 0.55rem 0.65rem;
    border-radius: var(--radius);
    background: color-mix(in srgb, var(--panel-2) 65%, var(--panel));
    border: 1px solid color-mix(in srgb, var(--border) 80%, transparent);
    font-size: 0.84rem;
    animation: msg-in 0.35s ease both;
    animation-delay: calc(var(--msg-i, 0) * 40ms);
  }
  @keyframes msg-in {
    from {
      opacity: 0;
      transform: translateY(4px);
    }
    to {
      opacity: 1;
      transform: translateY(0);
    }
  }
  .msg.user {
    background: var(--accent-soft);
    border-color: color-mix(in srgb, var(--accent) 28%, var(--border));
    margin-left: 0.75rem;
  }
  .msg.assistant {
    margin-right: 0.5rem;
  }
  .msg-role {
    font-size: 0.62rem;
    text-transform: uppercase;
    letter-spacing: 0.08em;
    color: var(--muted);
    margin-bottom: 0.25rem;
  }
  .msg-body {
    line-height: 1.45;
    color: var(--ink);
  }
  .corpus {
    margin-top: 0.4rem;
    font-size: 0.7rem;
    color: var(--muted);
  }
  .corpus summary {
    cursor: pointer;
  }
  .chat-empty {
    flex: 1;
    display: flex;
    align-items: center;
    font-size: 0.84rem;
    line-height: 1.45;
    padding: 1rem 0;
  }
  .reply-box textarea {
    width: 100%;
    font-family: inherit;
    font-size: 0.85rem;
    border-radius: var(--radius);
    border: 1px solid var(--border);
    padding: 0.5rem 0.55rem;
    resize: vertical;
    background: var(--bg);
    color: var(--ink);
    transition: border-color 0.15s ease;
  }
  .reply-box textarea:focus {
    outline: none;
    border-color: var(--accent);
  }
  .reply-actions {
    display: flex;
    gap: 0.4rem;
    margin-top: 0.45rem;
  }
  .btn-primary {
    background: var(--accent);
    color: white;
    border-color: var(--accent);
    font-size: 0.8rem;
    padding: 0.3rem 0.7rem;
  }
  .btn-dismiss {
    font-size: 0.8rem;
    padding: 0.3rem 0.7rem;
    background: transparent;
    color: var(--muted);
  }
  .btn-dismiss:hover:not(:disabled) {
    color: var(--ink);
    border-color: var(--muted);
  }
</style>
