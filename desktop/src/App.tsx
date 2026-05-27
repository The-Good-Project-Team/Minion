import { useEffect, useMemo, useRef, useState } from "react";
import { Database, FolderOpen, Settings, Sparkles } from "lucide-react";

import {
  fetchFeed,
  agentNext,
  openAgentReplyStream,
  type ActivityFeedBundle,
  type CouncilFeedItem,
  type FeedItem,
  type FeedRow,
} from "./lib/api";
import { Orb, type AgentState } from "@/registry/elevenlabs-ui/ui/orb";
import {
  Conversation,
  ConversationContent,
  ConversationEmptyState,
  ConversationScrollButton,
} from "@/registry/elevenlabs-ui/ui/conversation";
import { Message, MessageContent } from "@/registry/elevenlabs-ui/ui/message";
import { Button } from "@/registry/elevenlabs-ui/ui/button";
import { Card, CardContent } from "@/registry/elevenlabs-ui/ui/card";
import { cn } from "./lib/utils";

function isCouncil(item: FeedRow): item is CouncilFeedItem {
  return item.item_kind === "council";
}

function itemKey(item: FeedRow): string {
  return isCouncil(item) ? `council-${item.proposal.proposal_id}` : item.feed_id;
}

function itemText(item: FeedRow): string {
  if (isCouncil(item)) {
    return [item.proposal.title, item.proposal.summary, String(item.proposal.payload?.body ?? "")]
      .filter(Boolean)
      .join("\n\n");
  }
  const raw = (item.body || item.title || "").trim();
  return raw.replace(/^\*\*[^:]+:\*\*\s*/gm, "").trim();
}

function itemRole(item: FeedRow): "user" | "assistant" {
  return !isCouncil(item) && item.kind === "you" ? "user" : "assistant";
}

function timeLabel(ts: number): string {
  if (!ts) return "";
  return new Date(ts * 1000).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}

export function App() {
  const [bundle, setBundle] = useState<ActivityFeedBundle | null>(null);
  const [draft, setDraft] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [streamBuf, setStreamBuf] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const activeThreadRef = useRef<string | null>(null);

  async function load() {
    try {
      const next = await fetchFeed({ limit: 100 });
      activeThreadRef.current = next.agent?.active_thread_id ?? activeThreadRef.current;
      setBundle(next);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  useEffect(() => {
    void load();
    const poll = window.setInterval(() => void load(), 30_000);
    const onChat = () => void load();
    window.addEventListener("minion:chat_updated", onChat);
    return () => {
      window.clearInterval(poll);
      window.removeEventListener("minion:chat_updated", onChat);
    };
  }, []);

  const rows = useMemo(
    () => [...(bundle?.items ?? [])].sort((a, b) => a.ts - b.ts),
    [bundle?.items],
  );
  const graphTotal = bundle?.graph?.user_node_count ?? 0;
  const activeNodes = bundle?.graph?.spine?.active_nodes ?? [];
  const memoryHits = bundle?.memory_prefetch ?? [];
  const agentState: AgentState = error ? null : streaming ? "talking" : bundle ? "listening" : "thinking";

  async function ensureThread(): Promise<string | null> {
    if (activeThreadRef.current) return activeThreadRef.current;
      const next = await agentNext();
    const tid = next.thread?.thread_id ?? null;
    activeThreadRef.current = tid;
    await load();
    return tid;
  }

  async function send() {
    const message = draft.trim();
    if (!message || busy || streaming) return;
    setBusy(true);
    setDraft("");
    setStreamBuf("");
    try {
      const threadId = await ensureThread();
      if (!threadId) throw new Error("No active Minion thread.");
      setStreaming(true);
      const stream = openAgentReplyStream(message, threadId, undefined, {
        onDelta: (delta) => setStreamBuf((s) => s + delta),
        onDone: async () => {
          setStreaming(false);
          setStreamBuf("");
          await load();
        },
        onError: (msg) => {
          setStreaming(false);
          setStreamBuf("");
          setError(msg);
        },
      });
      await stream.finished;
      stream.cancel();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
      setStreaming(false);
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="relative grid h-screen grid-cols-[minmax(280px,360px)_1fr] overflow-hidden bg-background text-foreground">
      <aside className="relative flex min-h-0 flex-col border-r border-border/70 bg-card/70 p-5 shadow-2xl shadow-slate-950/5 backdrop-blur-xl">
        <div className="pointer-events-none absolute inset-x-0 top-0 h-48 bg-[radial-gradient(circle_at_50%_0%,rgba(30,107,143,0.18),transparent_70%)]" />
        <div className="relative z-10 flex items-center gap-3">
          <img src="/minion.png" alt="" className="size-9 rounded-xl" />
          <div>
            <div className="text-sm font-semibold">Minion</div>
            <div className="text-xs text-muted-foreground">Your personal MCP</div>
          </div>
        </div>

        <div className="relative z-10 mx-auto mt-10 h-56 w-56">
          <Orb
            agentState={agentState}
            colors={["#d6f3ff", "#1f6c90"]}
            seed={2000}
            className="h-full w-full"
          />
        </div>

        <div className="relative z-10 mt-7 space-y-3">
          <p className="text-xs font-bold uppercase tracking-[0.18em] text-primary">Local identity graph</p>
          <h1 className="font-serif text-5xl leading-[0.9] tracking-[-0.04em]">
            No one AI owns your memory.
          </h1>
          <p className="text-sm leading-6 text-muted-foreground">
            Chat first. Ambient underneath. Contacts, Gmail, files, screen context, and graph evidence live on this machine.
          </p>
        </div>

        <div className="relative z-10 mt-7 grid grid-cols-3 gap-2">
          <Metric label="Graph" value={graphTotal} />
          <Metric label="Active" value={activeNodes.length} />
          <Metric label="Fetched" value={memoryHits.length} />
        </div>

        <div className="relative z-10 mt-auto flex gap-2 pt-6">
          <a className="inline-flex flex-1 items-center justify-center gap-2 rounded-md border border-border bg-background/70 px-3 py-2 text-sm font-medium" href="/sources">
            <FolderOpen className="size-4" />
            Sources
          </a>
          <a className="inline-flex flex-1 items-center justify-center gap-2 rounded-md border border-border bg-background/70 px-3 py-2 text-sm font-medium" href="/settings">
            <Settings className="size-4" />
            Settings
          </a>
        </div>
      </aside>

      <section className="flex min-h-0 flex-col p-5">
        <header className="mb-4 flex items-center justify-between gap-4">
          <div>
            <p className="text-xs font-bold uppercase tracking-[0.18em] text-primary">Live agent</p>
            <h2 className="font-serif text-4xl leading-none tracking-[-0.04em]">Ask your life.</h2>
          </div>
          <div className="flex items-center gap-2 rounded-full border border-border bg-card px-3 py-2 text-xs text-muted-foreground">
            <span className={cn("size-2 rounded-full", error ? "bg-destructive" : streaming ? "bg-amber-500" : "bg-emerald-500")} />
            {error ? "Sidecar offline" : streaming ? "Thinking" : "Listening"}
          </div>
        </header>

        <Card className="min-h-0 flex-1 overflow-hidden border-border/80 bg-card/80 py-0 shadow-2xl shadow-slate-950/10">
          <CardContent className="flex h-full min-h-0 flex-col px-0">
            <Conversation className="min-h-0 flex-1">
              <ConversationContent className="space-y-1 p-5">
                {error ? (
                  <ConversationEmptyState
                    icon={<Database className="size-6" />}
                    title="Minion cannot reach the local sidecar"
                    description={error}
                  />
                ) : rows.length === 0 && !streaming ? (
                  <ConversationEmptyState
                    icon={<Sparkles className="size-6" />}
                    title="Start with a person, project, or memory"
                    description="Minion will answer with your local context graph and evidence."
                  />
                ) : (
                  rows.map((item) => (
                    <Message key={itemKey(item)} from={itemRole(item)}>
                      <MessageContent variant="contained" className="whitespace-pre-wrap leading-6">
                        <div>{itemText(item)}</div>
                        <div className="mt-2 text-[11px] opacity-55">{timeLabel(item.ts)}</div>
                      </MessageContent>
                    </Message>
                  ))
                )}
                {streaming && streamBuf ? (
                  <Message from="assistant">
                    <MessageContent variant="contained" className="whitespace-pre-wrap leading-6">
                      {streamBuf}
                    </MessageContent>
                  </Message>
                ) : null}
              </ConversationContent>
              <ConversationScrollButton />
            </Conversation>

            <div className="border-t border-border/70 bg-background/60 p-3 backdrop-blur">
              <div className="flex items-end gap-2 rounded-2xl border border-border bg-card p-2 shadow-lg">
                <textarea
                  value={draft}
                  onChange={(e) => setDraft(e.currentTarget.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && !e.shiftKey) {
                      e.preventDefault();
                      void send();
                    }
                  }}
                  rows={2}
                  placeholder="Ask about a person, email, project, file, commitment, or what you were doing…"
                  className="min-h-12 flex-1 resize-none bg-transparent px-3 py-2 text-sm leading-6 outline-none placeholder:text-muted-foreground"
                />
                <Button disabled={!draft.trim() || busy || streaming} onClick={() => void send()} className="rounded-full px-5">
                  Ask
                </Button>
              </div>
            </div>
          </CardContent>
        </Card>
      </section>
    </main>
  );
}

function Metric({ label, value }: { label: string; value: number }) {
  return (
    <div className="rounded-2xl border border-border bg-background/70 p-3">
      <div className="font-serif text-3xl leading-none tracking-[-0.04em]">{value}</div>
      <div className="mt-1 text-[11px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
        {label}
      </div>
    </div>
  );
}
