<script lang="ts">
  import {
    chatAsk,
    fetchChatAskThreads,
    fetchChatThread,
    type ChatThread,
  } from "$lib/api";

  let threads = $state<ChatThread[]>([]);
  let active = $state<ChatThread | null>(null);
  let draft = $state("");
  let err = $state<string | null>(null);
  let busy = $state(false);

  export async function load() {
    try {
      const t = await fetchChatAskThreads();
      threads = t.threads;
      if (active?.thread_id) {
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

  async function newChat() {
    active = null;
    draft = "";
  }

  async function send() {
    const text = draft.trim();
    if (!text) return;
    busy = true;
    err = null;
    try {
      const r = await chatAsk(text, active?.thread_id);
      if (r.thread) {
        active = r.thread;
        draft = "";
        await load();
      }
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
    return [];
  }

  function onKeydown(e: KeyboardEvent) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      void send();
    }
  }

  load();
</script>

<div class="ask-panel">
  <header class="ask-head">
    <div>
      <p class="ask-eyebrow">Vault</p>
      <h2>Ask</h2>
    </div>
    <button type="button" class="btn btn-new" disabled={busy} onclick={() => void newChat()}>
      New
    </button>
  </header>
  <p class="ask-lede">Questions over your indexed memory — capture, files, and ambient notes.</p>

  {#if err}
    <p class="ask-err">{err}</p>
  {/if}

  {#if threads.length}
    <div class="thread-tabs" role="tablist">
      {#each threads as t (t.thread_id)}
        <button
          type="button"
          class="thread-tab"
          class:active={active?.thread_id === t.thread_id}
          role="tab"
          onclick={() => void openThread(t.thread_id)}
        >
          {(t.topic || "Ask").slice(0, 24)}
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
                <summary>Sources</summary>
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
    {:else}
      <p class="chat-empty muted">Ask anything about what Minion has captured or indexed.</p>
    {/if}

    <div class="reply-box">
      <textarea
        bind:value={draft}
        rows="3"
        placeholder="What was I working on yesterday? Who is Alex? …"
        onkeydown={onKeydown}
      ></textarea>
      <div class="reply-actions">
        <button type="button" class="btn btn-primary" disabled={busy || !draft.trim()} onclick={() => void send()}>
          {busy ? "…" : "Ask"}
        </button>
      </div>
    </div>
  </section>
</div>

<style>
  .ask-panel {
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
  .ask-head {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    gap: 0.75rem;
    margin-bottom: 0.35rem;
  }
  .ask-eyebrow {
    margin: 0 0 0.15rem;
    font-size: 0.62rem;
    font-weight: 600;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--muted);
  }
  .ask-head h2 {
    margin: 0;
    font-family: var(--font-display);
    font-size: 1.35rem;
    font-weight: 400;
  }
  .btn-new {
    font-size: 0.78rem;
    padding: 0.3rem 0.65rem;
    background: transparent;
    border-color: var(--border);
  }
  .ask-lede {
    margin: 0 0 0.65rem;
    font-size: 0.78rem;
    color: var(--muted);
    line-height: 1.4;
  }
  .ask-err {
    font-size: 0.8rem;
    color: var(--danger);
    margin: 0 0 0.5rem;
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
  }
  .thread-tab.active {
    color: var(--accent);
    background: var(--accent-soft);
    border-color: color-mix(in srgb, var(--accent) 45%, var(--border));
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
    min-height: 120px;
  }
  .msg {
    padding: 0.55rem 0.65rem;
    border-radius: var(--radius);
    background: color-mix(in srgb, var(--panel-2) 65%, var(--panel));
    border: 1px solid var(--border);
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
    margin-left: 0.75rem;
  }
  .msg-role {
    font-size: 0.62rem;
    text-transform: uppercase;
    color: var(--muted);
    margin-bottom: 0.25rem;
  }
  .corpus {
    margin-top: 0.4rem;
    font-size: 0.7rem;
    color: var(--muted);
  }
  .chat-empty {
    font-size: 0.84rem;
    margin: 0 0 0.5rem;
  }
  .reply-box textarea {
    width: 100%;
    font-family: inherit;
    font-size: 0.85rem;
    border-radius: var(--radius);
    border: 1px solid var(--border);
    padding: 0.5rem;
    resize: vertical;
    background: var(--bg);
    color: var(--ink);
  }
  .reply-actions {
    margin-top: 0.45rem;
  }
  .btn-primary {
    background: var(--accent);
    color: white;
    border-color: var(--accent);
    font-size: 0.8rem;
    padding: 0.3rem 0.7rem;
  }
</style>
