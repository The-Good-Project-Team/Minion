import { useEffect, useMemo, useRef, useState } from "react";
import { CheckCircle2, Database, FolderOpen, Plus, Settings, Sparkles, Upload } from "lucide-react";
import { open } from "@tauri-apps/plugin-dialog";

import {
  fetchFeed,
  agentNext,
  agentDismiss,
  agentOnboardingReply,
  onSidecarStatus,
  openMacosPrivacySettings,
  openAgentReplyStream,
  snapshotLifeEvidence,
  fetchResourcePollNext,
  answerResourcePoll,
  recordConnectorIntent,
  saveOnboardingProfile,
  openSession,
  copyIntoInbox,
  getConfig,
  ingestText,
  revealInFinder,
  type ActivityFeedBundle,
  type SessionOpenResponse,
  type SidecarStatus,
  type AgentStreamState,
  type CouncilFeedItem,
  type FeedItem,
  type FeedRow,
  type OnboardingTurn,
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

type OnboardingStep =
  | "name"
  | "contacts"
  | "accessibility"
  | "screen-recording"
  | "resource_poll"
  | "connector"
  | "done";
type PermissionStep = Exclude<OnboardingStep, "name" | "resource_poll" | "connector" | "done">;

const ONBOARDING_DONE_KEY = "minion:onboarding_done";
const ONBOARDING_NAME_KEY = "minion:onboarding_name";
const ONBOARDING_INTRO =
  "I keep your data yours.\nI help you remember useful people, files, apps, and answers on this Mac. I'll ask before connecting anything private.\nWhat should I call you?";

const CONNECTOR_SUGGESTIONS = ["Gmail", "Slack", "A folder on this Mac"] as const;

function storedName(): string {
  return window.localStorage.getItem(ONBOARDING_NAME_KEY) ?? "";
}

function initialOnboardingStep(): OnboardingStep {
  if (window.localStorage.getItem(ONBOARDING_DONE_KEY) === "true") return "done";
  return storedName() ? "contacts" : "name";
}

function nextPermissionStep(step: PermissionStep): OnboardingStep {
  if (step === "contacts") return "accessibility";
  if (step === "accessibility") return "screen-recording";
  return "resource_poll";
}

function permissionSettled(status: string | undefined): boolean {
  return status === "granted" || status === "skipped" || status === "running";
}

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
  if (isCouncil(item)) return "assistant";
  if (item.kind === "you") return "user";
  return "assistant";
}

function isConversation(item: FeedRow): boolean {
  if (isCouncil(item)) return false;
  return item.lane === "conversation" && (item.kind === "forty_two" || item.kind === "you");
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
  const [sidecarStatus, setSidecarStatus] = useState<SidecarStatus>({
    state: "starting",
    message: "Starting local sidecar...",
  });
  const [busy, setBusy] = useState(false);
  const [prompting, setPrompting] = useState(false);
  const [onboardingStep, setOnboardingStep] = useState<OnboardingStep>(initialOnboardingStep);
  const [displayName, setDisplayName] = useState(storedName);
  const [onboardingMessages, setOnboardingMessages] = useState<OnboardingTurn[]>([]);
  const [permissionBusy, setPermissionBusy] = useState<PermissionStep | null>(null);
  const [permissionNote, setPermissionNote] = useState("");
  const [permissionStatus, setPermissionStatus] = useState<Record<string, string>>({});
  const [pollQuestion, setPollQuestion] = useState<{
    resource_id: string;
    question: string;
    label: string;
  } | null>(null);
  const [quickTitle, setQuickTitle] = useState("Quick context");
  const [quickText, setQuickText] = useState("");
  const [quickStatus, setQuickStatus] = useState("");
  const [quickBusy, setQuickBusy] = useState(false);
  const activeThreadRef = useRef<string | null>(null);
  const promptingRef = useRef(false);
  const onboardingDoneRef = useRef(onboardingStep === "done");
  const onboardingStepRef = useRef(onboardingStep);
  const promptedStepsRef = useRef<Set<OnboardingStep>>(new Set());
  const [sessionOpen, setSessionOpen] = useState<SessionOpenResponse | null>(null);
  const [sessionLoading, setSessionLoading] = useState(false);
  const lastSessionAtRef = useRef(0);
  const sessionInFlightRef = useRef(false);

  useEffect(() => {
    onboardingDoneRef.current = onboardingStep === "done";
    onboardingStepRef.current = onboardingStep;
  }, [onboardingStep]);

  useEffect(() => {
    if (onboardingStep !== "name" || onboardingMessages.length > 0) return;
    setOnboardingMessages([{ role: "assistant", content: ONBOARDING_INTRO }]);
    promptedStepsRef.current.add("name");
  }, [onboardingStep, onboardingMessages.length]);

  useEffect(() => {
    if (onboardingStep !== "contacts") return;
    if (!permissionSettled(permissionStatus.contacts)) return;
    const next = nextPermissionStep("contacts");
    if (next === onboardingStep) return;
    promptedStepsRef.current.add("contacts");
    setOnboardingStep(next);
  }, [onboardingStep, permissionStatus.contacts]);

  async function runOpenSession() {
    if (onboardingStep !== "done" || sessionInFlightRef.current) return;
    const now = Date.now();
    if (now - lastSessionAtRef.current < 60_000 && sessionOpen) return;
    sessionInFlightRef.current = true;
    lastSessionAtRef.current = now;
    setSessionLoading(true);
    try {
      const out = await openSession({ display_name: displayName });
      setSessionOpen(out);
      if (out.thread_id) activeThreadRef.current = out.thread_id;
      const refreshed = await fetchFeed({ limit: 100 });
      activeThreadRef.current = refreshed.agent?.active_thread_id ?? activeThreadRef.current;
      setBundle(refreshed);
      setError(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setSessionLoading(false);
      sessionInFlightRef.current = false;
    }
  }

  async function promptNextQuestion(agent: AgentStreamState) {
    if (promptingRef.current || agent.active_thread_id || !agent.needs_question) return;
    promptingRef.current = true;
    setPrompting(true);
    try {
      const next = await agentNext();
      const tid = next.thread?.thread_id ?? null;
      if (tid) activeThreadRef.current = tid;
      const refreshed = await fetchFeed({ limit: 100 });
      activeThreadRef.current = refreshed.agent?.active_thread_id ?? activeThreadRef.current;
      setBundle(refreshed);
    } finally {
      promptingRef.current = false;
      setPrompting(false);
    }
  }

  async function promptOnboarding(step: OnboardingStep, transcript = onboardingMessages) {
    if (step === "done" || promptedStepsRef.current.has(step)) return;
    promptedStepsRef.current.add(step);
    setPrompting(true);
    try {
      const out = await agentOnboardingReply({
        step,
        display_name: displayName,
        transcript,
        permission_status: permissionStatus,
      });
      if (onboardingStepRef.current !== step) return;
      setOnboardingMessages((prev) => [...prev, { role: "assistant", content: out.message }]);
    } catch (e) {
      if (onboardingStepRef.current !== step) return;
      setOnboardingMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          content: e instanceof Error ? e.message : String(e),
        },
      ]);
    } finally {
      setPrompting(false);
    }
  }

  async function load() {
    try {
      const next = await fetchFeed({ limit: 100 });
      activeThreadRef.current = next.agent?.active_thread_id ?? activeThreadRef.current;
      setBundle(next);
      setError(null);
      if (
        next.agent &&
        onboardingDoneRef.current &&
        !sessionInFlightRef.current &&
        !next.agent.active_thread_id &&
        next.agent.needs_question
      ) {
        void promptNextQuestion(next.agent);
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    }
  }

  useEffect(() => {
    let cancelled = false;
    void onSidecarStatus((s) => {
      if (cancelled) return;
      setSidecarStatus(s);
      if (s.state === "ready") void load();
      if (s.state === "error") setError(s.message || "Sidecar failed to start.");
    }).then((unlisten) => {
      if (cancelled) unlisten();
    });
    void load();
    const poll = window.setInterval(() => void load(), 30_000);
    const onChat = () => void load();
    window.addEventListener("minion:chat_updated", onChat);
    return () => {
      cancelled = true;
      window.clearInterval(poll);
      window.removeEventListener("minion:chat_updated", onChat);
    };
  }, []);

  useEffect(() => {
    if (onboardingStep !== "done") return;
    void runOpenSession();
  }, [onboardingStep]);

  useEffect(() => {
    if (onboardingStep !== "done") return;
    const onFocus = () => void runOpenSession();
    window.addEventListener("focus", onFocus);
    return () => window.removeEventListener("focus", onFocus);
  }, [onboardingStep, displayName]);

  useEffect(() => {
    if (onboardingStep !== "done") void promptOnboarding(onboardingStep);
  }, [onboardingStep, displayName]);

  useEffect(() => {
    if (onboardingStep !== "resource_poll") return;
    void (async () => {
      try {
        const out = await fetchResourcePollNext();
        const q = out.question;
        if (q) {
          setPollQuestion({
            resource_id: q.resource_id,
            question: q.question,
            label: q.label,
          });
        } else {
          setOnboardingStep("connector");
        }
      } catch {
        setPollQuestion(null);
      }
    })();
  }, [onboardingStep]);

  const agent = bundle?.agent;
  const conversationRows = useMemo(
    () =>
      [...(bundle?.items ?? [])]
        .filter(isConversation)
        .sort((a, b) => a.ts - b.ts),
    [bundle?.items],
  );
  const agentQuestion = (agent?.question_body_md ?? "").trim();
  const sessionRequest = (sessionOpen?.request_md ?? "").trim();
  const pendingQuestion = agentQuestion || sessionRequest;
  const suggestions = agent?.suggestions ?? [];
  const onboardingActive = onboardingStep !== "done";
  const awaitingAnswer =
    onboardingStep === "done" && Boolean(pendingQuestion) && Boolean(agent?.active_thread_id || sessionRequest);
  const graphTotal = bundle?.graph?.user_node_count ?? 0;
  const activeNodes = bundle?.graph?.spine?.active_nodes ?? [];
  const memoryHits = bundle?.memory_prefetch ?? [];
  const bootingSidecar = !bundle && sidecarStatus.state !== "error";
  const sidecarLabel =
    sidecarStatus.state === "ready"
      ? "Sidecar ready"
      : sidecarStatus.state === "error"
        ? "Sidecar offline"
        : sidecarStatus.state === "installing"
          ? "Installing sidecar"
          : "Starting sidecar";
  const composerOnboardingSteps: OnboardingStep[] = [
    "name",
    "connector",
    "contacts",
    "accessibility",
    "screen-recording",
  ];
  const canComposeOnboarding = onboardingActive && composerOnboardingSteps.includes(onboardingStep);
  const agentState: AgentState = error
    ? null
    : onboardingActive || streaming || prompting
      ? "talking"
      : agent?.needs_question
        ? "thinking"
        : bundle
          ? "listening"
          : "thinking";

  async function ensureThread(): Promise<string | null> {
    if (activeThreadRef.current) return activeThreadRef.current;
      const next = await agentNext();
    const tid = next.thread?.thread_id ?? null;
    activeThreadRef.current = tid;
    await load();
    return tid;
  }

  async function sendAnswer(message: string, action?: string) {
    const text = message.trim();
    if (!text && action !== "dismiss") return;
    if (busy || streaming) return;
    setBusy(true);
    if (!action) setDraft("");
    setStreamBuf("");
    try {
      const threadId = await ensureThread();
      if (!threadId) throw new Error("No active Minion thread.");
      if (action === "dismiss") {
        await agentDismiss(threadId);
        activeThreadRef.current = null;
        await load();
        return;
      }
      setStreaming(true);
      const stream = openAgentReplyStream(text, threadId, action, {
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

  async function finishOnboarding() {
    try {
      if (displayName.trim()) await saveOnboardingProfile(displayName.trim());
      const out = await agentOnboardingReply({
        step: "done",
        display_name: displayName,
        transcript: onboardingMessages,
        permission_status: permissionStatus,
      });
      setOnboardingMessages((prev) => [...prev, { role: "assistant", content: out.message }]);
    } catch {
      /* bridge message is best-effort */
    }
    window.localStorage.setItem(ONBOARDING_DONE_KEY, "true");
    setOnboardingStep("done");
    setPermissionNote("");
    try {
      const next = await fetchFeed({ limit: 100 });
      activeThreadRef.current = next.agent?.active_thread_id ?? activeThreadRef.current;
      setBundle(next);
      void runOpenSession();
    } catch {
      await load();
    }
  }

  function advancePastPermissionStep(step: PermissionStep) {
    promptedStepsRef.current.add(step);
    setOnboardingStep(nextPermissionStep(step));
    setPermissionNote("");
  }

  function advanceOnboarding(step: PermissionStep) {
    setPermissionStatus((prev) => ({
      ...prev,
      [step]: prev[step] === "granted" ? "granted" : "skipped",
    }));
    setOnboardingMessages((prev) => [...prev, { role: "user", content: permissionCopy(step).continueLabel }]);
    advancePastPermissionStep(step);
  }

  async function startContactsSyncInBackground() {
    const status = permissionStatus.contacts;
    if (status === "granted" || status === "running") return;
    setPermissionStatus((prev) => ({ ...prev, contacts: "running" }));
    setOnboardingMessages((prev) => [
      ...prev,
      { role: "user", content: "Yes — sync Contacts" },
      {
        role: "assistant",
        content: "Got it — I'll index Contacts in the background while we keep going.",
      },
    ]);
    advancePastPermissionStep("contacts");
    try {
      const out = await snapshotLifeEvidence();
      const count = out.indexed_contacts ?? out.contacts ?? 0;
      if (out.skipped) {
        setPermissionStatus((prev) => ({ ...prev, contacts: "denied" }));
        setOnboardingMessages((prev) => [
          ...prev,
          {
            role: "assistant",
            content:
              "Contacts didn't open — you can enable Minion in Privacy & Security > Contacts anytime.",
          },
        ]);
        return;
      }
      setPermissionStatus((prev) => ({ ...prev, contacts: "granted" }));
      setOnboardingMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          content: `Contacts are ready — I saved ${count} name${count === 1 ? "" : "s"} on this Mac.`,
        },
      ]);
    } catch (e) {
      setPermissionStatus((prev) => ({ ...prev, contacts: "denied" }));
      setOnboardingMessages((prev) => [
        ...prev,
        {
          role: "assistant",
          content: e instanceof Error ? e.message : String(e),
        },
      ]);
    }
  }

  async function runPermissionStep(step: PermissionStep) {
    if (step === "contacts") {
      await startContactsSyncInBackground();
      return;
    }
    setPermissionBusy(step);
    setPermissionNote("");
    try {
      await openMacosPrivacySettings(step);
      setPermissionStatus((prev) => ({ ...prev, [step]: "settings_opened" }));
      setPermissionNote("System Settings is open. Enable Minion, then come back here and continue.");
    } catch (e) {
      setPermissionNote(e instanceof Error ? e.message : String(e));
    } finally {
      setPermissionBusy(null);
    }
  }

  async function pickConnectorSuggestion(source: string) {
    if (source === "Not now") {
      setOnboardingMessages((prev) => [...prev, { role: "user", content: "Not now" }]);
      await finishOnboarding();
      return;
    }
    setOnboardingMessages((prev) => [...prev, { role: "user", content: source }]);
    try {
      await recordConnectorIntent({ source_text: source });
    } catch {
      /* best-effort */
    }
    await finishOnboarding();
  }

  async function submitOnboardingName() {
    const name = draft.trim();
    if (!name) return;
    window.localStorage.setItem(ONBOARDING_NAME_KEY, name);
    setDisplayName(name);
    setOnboardingMessages((prev) => [...prev, { role: "user", content: name }]);
    setDraft("");
    try {
      await saveOnboardingProfile(name);
    } catch {
      /* local name still works offline */
    }
    promptedStepsRef.current.delete("contacts");
    setOnboardingStep("contacts");
  }

  async function answerPoll(uses: boolean) {
    if (!pollQuestion || busy) return;
    setBusy(true);
    try {
      const out = await answerResourcePoll({
        resource_id: pollQuestion.resource_id,
        uses,
      });
      setOnboardingMessages((prev) => [
        ...prev,
        { role: "user", content: uses ? `Yes — ${pollQuestion.label}` : `Not now — ${pollQuestion.label}` },
      ]);
      if (out.poll_complete) {
        promptedStepsRef.current.delete("resource_poll");
        setOnboardingStep("connector");
        setPollQuestion(null);
      } else if (out.next_question) {
        setPollQuestion({
          resource_id: out.next_question.resource_id,
          question: out.next_question.question,
          label: out.next_question.label,
        });
        promptedStepsRef.current.delete("resource_poll");
        void promptOnboarding("resource_poll");
      }
    } finally {
      setBusy(false);
    }
  }

  async function send() {
    if (onboardingStep === "name") {
      await submitOnboardingName();
      return;
    }
    if (
      onboardingStep === "contacts" ||
      onboardingStep === "accessibility" ||
      onboardingStep === "screen-recording"
    ) {
      const text = draft.trim();
      if (!text) return;
      const nextTranscript: OnboardingTurn[] = [...onboardingMessages, { role: "user", content: text }];
      setOnboardingMessages(nextTranscript);
      setDraft("");
      promptedStepsRef.current.delete(onboardingStep);
      void promptOnboarding(onboardingStep, nextTranscript);
      return;
    }
    if (onboardingStep === "connector") {
      const source = draft.trim();
      if (!source) return;
      setOnboardingMessages((prev) => [...prev, { role: "user", content: source }]);
      setDraft("");
      await recordConnectorIntent({ source_text: source });
      await finishOnboarding();
      return;
    }
    await sendAnswer(draft);
  }

  async function addQuickText() {
    const text = quickText.trim();
    if (!text || quickBusy) return;
    setQuickBusy(true);
    setQuickStatus("");
    try {
      await ingestText({ title: quickTitle.trim() || "Quick context", text });
      setQuickText("");
      setQuickStatus("Saved. Minion is indexing it now.");
      await load();
    } catch (e) {
      setQuickStatus(e instanceof Error ? e.message : String(e));
    } finally {
      setQuickBusy(false);
    }
  }

  async function addFiles() {
    if (quickBusy) return;
    setQuickBusy(true);
    setQuickStatus("");
    try {
      const selected = await open({
        multiple: true,
        directory: false,
        title: "Add files to Minion",
      });
      const paths = Array.isArray(selected) ? selected : selected ? [selected] : [];
      if (paths.length === 0) return;
      const out = await copyIntoInbox(paths);
      const copied = out.drops.reduce((sum, drop) => sum + drop.copied, 0);
      setQuickStatus(copied ? `Added ${copied} file${copied === 1 ? "" : "s"} to the server.` : "No new files added.");
      await load();
    } catch (e) {
      setQuickStatus(e instanceof Error ? e.message : String(e));
    } finally {
      setQuickBusy(false);
    }
  }

  async function showInbox() {
    try {
      const cfg = await getConfig();
      await revealInFinder(cfg.inbox);
    } catch (e) {
      setQuickStatus(e instanceof Error ? e.message : String(e));
    }
  }

  return (
    <main className="relative grid h-screen grid-cols-[minmax(280px,340px)_1fr] overflow-hidden bg-background text-foreground">
      <aside className="relative flex min-h-0 flex-col overflow-y-auto border-r border-border/70 bg-card/80 p-6 shadow-2xl shadow-slate-950/5 backdrop-blur-xl">
        <div className="pointer-events-none absolute inset-x-0 top-0 h-40 bg-[radial-gradient(circle_at_50%_0%,rgba(30,107,143,0.14),transparent_72%)]" />
        <div className="relative z-10 flex items-center gap-3">
          <div className="grid size-10 place-items-center overflow-hidden rounded-2xl border border-primary/20 bg-primary/10 shadow-inner">
            <img src="/minion.png" alt="" className="size-full object-cover" />
          </div>
          <div>
            <div className="text-sm font-semibold">Minion</div>
            <div className="text-xs text-muted-foreground">I keep your data yours.</div>
          </div>
        </div>

        <div className="relative z-10 mt-10">
          <div className="mx-auto h-40 w-40 opacity-90">
            <Orb
              agentState={agentState}
              colors={["#d6f3ff", "#1f6c90"]}
              seed={2000}
              className="h-full w-full"
            />
          </div>
          <div className="mt-8 space-y-3">
            <h1 className="font-serif text-4xl leading-[0.95] tracking-[-0.04em]">
              Your memory stays yours.
            </h1>
            <p className="max-w-[24ch] text-sm leading-6 text-muted-foreground">
              I help you remember people, files, and answers on this Mac — one useful question at a time.
            </p>
          </div>
        </div>

        <div className="relative z-10 mt-8 rounded-3xl border border-border bg-background/65 p-4">
          <div className="mb-3 flex items-center justify-between text-xs">
            <span className="font-semibold text-muted-foreground">Today</span>
            <span className={cn("rounded-full px-2 py-1", bootingSidecar ? "bg-amber-500/10 text-amber-700" : "bg-primary/10 text-primary")}>
              {bootingSidecar ? "Starting" : "Ready"}
            </span>
          </div>
          <div className="grid grid-cols-3 gap-2">
            <Metric label="Saved" value={graphTotal} />
            <Metric label="Active" value={activeNodes.length} />
            <Metric label="Fetched" value={memoryHits.length} />
          </div>
        </div>

        <div className="relative z-10 mt-4 rounded-3xl border border-border bg-background/75 p-4 shadow-lg shadow-slate-950/5">
          <div className="mb-3 flex items-center justify-between gap-3">
            <div>
              <p className="text-[11px] font-bold uppercase tracking-[0.14em] text-primary">Add context</p>
              <p className="mt-1 text-xs leading-5 text-muted-foreground">Drop in text or pick files. Minion indexes them in the background.</p>
            </div>
            <div className="grid size-8 shrink-0 place-items-center rounded-2xl bg-primary/10 text-primary">
              <Plus className="size-4" />
            </div>
          </div>
          <input
            value={quickTitle}
            onChange={(e) => setQuickTitle(e.currentTarget.value)}
            className="mb-2 w-full rounded-2xl border border-border bg-card px-3 py-2 text-xs outline-none placeholder:text-muted-foreground"
            placeholder="Title"
          />
          <textarea
            value={quickText}
            onChange={(e) => setQuickText(e.currentTarget.value)}
            rows={4}
            className="w-full resize-none rounded-2xl border border-border bg-card px-3 py-2 text-xs leading-5 outline-none placeholder:text-muted-foreground"
            placeholder="Paste text you want Minion to remember..."
          />
          <div className="mt-3 flex flex-wrap gap-2">
            <Button type="button" size="sm" className="rounded-full" disabled={quickBusy || !quickText.trim()} onClick={() => void addQuickText()}>
              Save text
            </Button>
            <Button type="button" size="sm" variant="outline" className="rounded-full" disabled={quickBusy} onClick={() => void addFiles()}>
              <Upload className="mr-1 size-3.5" />
              Add files
            </Button>
            <Button type="button" size="sm" variant="ghost" className="rounded-full" onClick={() => void showInbox()}>
              Inbox
            </Button>
          </div>
          {quickStatus ? <p className="mt-2 text-xs leading-5 text-muted-foreground">{quickStatus}</p> : null}
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
            <p className="text-xs font-bold uppercase tracking-[0.18em] text-primary">
              {onboardingActive ? "First conversation" : "Your day"}
            </p>
            <h2 className="font-serif text-4xl leading-none tracking-[-0.04em]">
              {onboardingActive
                ? "Minion is getting set up."
                : sessionLoading
                  ? "Minion is catching up…"
                  : awaitingAnswer
                    ? "Minion is asking."
                    : agent?.needs_question
                      ? "Finding the next gap…"
                      : "Ask me anything."}
            </h2>
          </div>
          <div className="flex items-center gap-2 rounded-full border border-border bg-card px-3 py-2 text-xs text-muted-foreground">
            <span
              className={cn(
                "size-2 rounded-full",
                sidecarStatus.state === "error"
                  ? "bg-destructive"
                  : bootingSidecar || streaming || prompting
                    ? "bg-amber-500"
                    : awaitingAnswer
                      ? "bg-primary"
                      : "bg-emerald-500",
              )}
            />
            {bootingSidecar
              ? sidecarLabel
              : error
                ? "Sidecar offline"
              : onboardingActive
                ? "Introductions"
                : sessionLoading
                  ? "Briefing"
                  : streaming
                    ? "Thinking"
                    : prompting
                      ? "Asking"
                      : awaitingAnswer
                        ? "Waiting for you"
                        : "Listening"}
          </div>
        </header>

        <Card className="min-h-0 flex-1 overflow-hidden border-border/80 bg-card/80 py-0 shadow-2xl shadow-slate-950/10">
          <CardContent className="flex h-full min-h-0 flex-col px-0">
            <Conversation className="min-h-0 flex-1">
              <ConversationContent className="space-y-1 p-5">
                {bootingSidecar && !onboardingActive ? (
                  <LoadingScreen message={sidecarStatus.message || "Preparing local memory and sidecar services."} />
                ) : error ? (
                  <ConversationEmptyState
                    icon={<Database className="size-6" />}
                    title="Minion cannot reach the local sidecar"
                    description={error}
                  />
                ) : onboardingActive ? (
                  <>
                    {bootingSidecar ? (
                      <Message from="assistant">
                        <MessageContent variant="contained" className="whitespace-pre-wrap leading-6">
                          One moment — I'm waking up on this Mac.
                        </MessageContent>
                      </Message>
                    ) : null}
                    <OnboardingMessages
                      messages={onboardingMessages}
                      prompting={prompting}
                      permissionNote={permissionNote}
                    />
                    <OnboardingActions
                      step={onboardingStep}
                      busy={permissionBusy}
                      draft={draft}
                      pollQuestion={pollQuestion}
                      permissionStatus={permissionStatus}
                      onRun={runPermissionStep}
                      onContinue={advanceOnboarding}
                      onFinish={finishOnboarding}
                      onPollAnswer={(uses) => void answerPoll(uses)}
                      onSkipPolls={() => setOnboardingStep("connector")}
                      onConnectorPick={(source) => void pickConnectorSuggestion(source)}
                    />
                  </>
                ) : sessionLoading && !sessionOpen?.briefing_md ? (
                  <ConversationEmptyState
                    icon={<Sparkles className="size-6" />}
                    title="Minion is catching up"
                    description="Pulling together what changed since your last visit."
                  />
                ) : conversationRows.length === 0 && !streaming && !pendingQuestion && !sessionOpen?.briefing_md ? (
                  <ConversationEmptyState
                    icon={<Sparkles className="size-6" />}
                    title="Minion is building your graph"
                    description="Sync sources in Settings or tell Minion what to connect next."
                  />
                ) : (
                  <>
                    {sessionOpen?.briefing_md ? (
                      <Message from="assistant">
                        <MessageContent variant="contained" className="whitespace-pre-wrap leading-6">
                          {sessionOpen.briefing_md}
                        </MessageContent>
                      </Message>
                    ) : null}
                    {conversationRows.map((item) => (
                      <Message key={itemKey(item)} from={itemRole(item)}>
                        <MessageContent variant="contained" className="whitespace-pre-wrap leading-6">
                          <div>{itemText(item)}</div>
                          <div className="mt-2 text-[11px] opacity-55">{timeLabel(item.ts)}</div>
                        </MessageContent>
                      </Message>
                    ))}
                  </>
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
              {!onboardingActive && pendingQuestion ? (
                <div className="mb-3 rounded-2xl border border-primary/25 bg-primary/5 px-4 py-3">
                  <p className="text-[11px] font-bold uppercase tracking-[0.14em] text-primary">Minion asks</p>
                  <p className="mt-1 whitespace-pre-wrap text-sm leading-6">{pendingQuestion}</p>
                </div>
              ) : null}
              {!onboardingActive && suggestions.length > 0 ? (
                <div className="mb-2 flex flex-wrap gap-2">
                  {suggestions.map((s) => (
                    <Button
                      key={s.name}
                      type="button"
                      variant="outline"
                      size="sm"
                      className="rounded-full"
                      disabled={busy || streaming}
                      onClick={() => void sendAnswer(s.name)}
                    >
                      {s.name}
                    </Button>
                  ))}
                </div>
              ) : null}
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
                  disabled={onboardingActive && !canComposeOnboarding}
                  placeholder={
                    onboardingStep === "name"
                      ? "Type your name..."
                      : onboardingStep === "connector"
                        ? "Type a source, or tap a suggestion above..."
                        : onboardingActive
                          ? "Ask Minion something, or use the choices above..."
                        : awaitingAnswer
                      ? "Answer Minion — who is this, how do you know them, or skip for now…"
                      : "Reply when Minion asks, or add context for the next question…"
                  }
                  className="min-h-12 flex-1 resize-none bg-transparent px-3 py-2 text-sm leading-6 outline-none placeholder:text-muted-foreground"
                />
                <div className="flex flex-col gap-2">
                  <Button
                    disabled={
                      !draft.trim() ||
                      busy ||
                      streaming ||
                      sessionLoading ||
                      (onboardingActive && !canComposeOnboarding) ||
                      (!onboardingActive && !pendingQuestion)
                    }
                    onClick={() => void send()}
                    className="rounded-full px-5"
                  >
                    {onboardingStep === "name" ? "Tell Minion" : onboardingStep === "connector" ? "Send" : "Reply"}
                  </Button>
                  {!onboardingActive && awaitingAnswer ? (
                    <Button
                      type="button"
                      variant="ghost"
                      size="sm"
                      className="rounded-full text-xs"
                      disabled={busy || streaming}
                      onClick={() => void sendAnswer("", "dismiss")}
                    >
                      Skip
                    </Button>
                  ) : null}
                </div>
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
    <div className="rounded-2xl border border-border bg-card/80 p-3">
      <div className="font-serif text-2xl leading-none tracking-[-0.04em]">{value}</div>
      <div className="mt-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">
        {label}
      </div>
    </div>
  );
}

function LoadingScreen({ message }: { message: string }) {
  return (
    <div className="flex min-h-[420px] items-center justify-center p-6">
      <div className="w-full max-w-md rounded-[2rem] border border-border bg-background/70 p-8 text-center shadow-xl shadow-slate-950/5">
        <div className="mx-auto mb-6 grid size-16 place-items-center rounded-3xl border border-primary/20 bg-primary/10">
          <div className="size-7 animate-pulse rounded-full bg-primary/70 shadow-[0_0_40px_rgba(30,107,143,0.45)]" />
        </div>
        <p className="text-[11px] font-bold uppercase tracking-[0.22em] text-primary">Starting Minion</p>
        <h3 className="mt-3 font-serif text-3xl leading-none tracking-[-0.04em]">Warming up your local memory.</h3>
        <p className="mx-auto mt-4 max-w-sm text-sm leading-6 text-muted-foreground">{message}</p>
        <div className="mt-7 overflow-hidden rounded-full bg-border">
          <div className="h-1 w-1/2 animate-[pulse_1.4s_ease-in-out_infinite] rounded-full bg-primary" />
        </div>
      </div>
    </div>
  );
}

function permissionCopy(step: PermissionStep): { title: string; body: string; action: string; continueLabel: string } {
  if (step === "contacts") {
    return {
      title: "Can I read your Contacts?",
      body:
        "This helps me recognize people you mention later. I only read names from your local address book, and it stays on this Mac.",
      action: "Yes — sync Contacts",
      continueLabel: "Contacts are ready",
    };
  }
  if (step === "accessibility") {
    return {
      title: "Can I use Accessibility?",
      body:
        "This lets me see the active app and selected text when you ask for help, so you do not have to copy everything by hand.",
      action: "Open Accessibility Settings",
      continueLabel: "I enabled Accessibility",
    };
  }
  return {
    title: "Can I use Screen Recording?",
    body:
      "This is optional. It helps when an app will not share text directly. You can skip it and Minion will still work.",
    action: "Open Screen Recording Settings",
    continueLabel: "I enabled Screen Recording",
  };
}

function OnboardingMessages({
  messages,
  prompting,
  permissionNote,
}: {
  messages: OnboardingTurn[];
  prompting: boolean;
  permissionNote: string;
}) {
  return (
    <>
      {messages.map((message, idx) => (
        <Message key={`${message.role}-${idx}`} from={message.role === "user" ? "user" : "assistant"}>
          <MessageContent variant="contained" className="whitespace-pre-wrap leading-6">
            {message.content}
          </MessageContent>
        </Message>
      ))}
      {prompting ? (
        <Message from="assistant">
          <MessageContent variant="contained" className="whitespace-pre-wrap leading-6">
            Minion is typing...
          </MessageContent>
        </Message>
      ) : null}
      {permissionNote ? (
        <Message from="assistant">
          <MessageContent variant="contained" className="whitespace-pre-wrap leading-6">
            <div className="mb-1 flex items-center gap-2 text-primary">
              <CheckCircle2 className="size-4" />
              <span className="text-[11px] font-bold uppercase tracking-[0.14em]">Permission update</span>
            </div>
            {permissionNote}
          </MessageContent>
        </Message>
      ) : null}
    </>
  );
}

function OnboardingActions({
  step,
  busy,
  draft,
  pollQuestion,
  permissionStatus,
  onRun,
  onContinue,
  onFinish,
  onPollAnswer,
  onSkipPolls,
  onConnectorPick,
}: {
  step: OnboardingStep;
  busy: PermissionStep | null;
  draft: string;
  pollQuestion: { resource_id: string; question: string; label: string } | null;
  permissionStatus: Record<string, string>;
  onRun: (step: PermissionStep) => void | Promise<void>;
  onContinue: (step: PermissionStep) => void;
  onFinish: () => void;
  onPollAnswer: (uses: boolean) => void;
  onSkipPolls: () => void;
  onConnectorPick: (source: string) => void;
}) {
  if (step === "name") return null;
  if (step === "resource_poll" && pollQuestion) {
    return (
      <Message from="assistant">
        <MessageContent variant="contained" className="space-y-4 leading-6">
          <div>
            <p className="mt-1">{pollQuestion.question}</p>
          </div>
          <div className="flex flex-wrap gap-2">
            <Button type="button" disabled={Boolean(busy)} onClick={() => onPollAnswer(true)} className="rounded-full">
              Yes — {pollQuestion.label}
            </Button>
            <Button type="button" variant="outline" disabled={Boolean(busy)} onClick={() => onPollAnswer(false)} className="rounded-full">
              Not now
            </Button>
            <Button type="button" variant="ghost" onClick={onSkipPolls} className="rounded-full">
              Skip remaining
            </Button>
          </div>
        </MessageContent>
      </Message>
    );
  }
  if (step === "done") {
    return (
      <Message from="assistant">
        <MessageContent variant="contained" className="flex items-center justify-between gap-3 leading-6">
          <span>You're all set. Ask me anything.</span>
          <Button type="button" size="sm" onClick={onFinish}>
            Start using Minion
          </Button>
        </MessageContent>
      </Message>
    );
  }
  if (step === "connector") {
    return (
      <Message from="assistant">
        <MessageContent variant="contained" className="space-y-3 leading-6">
          <div className="flex flex-wrap gap-2">
            {CONNECTOR_SUGGESTIONS.map((label) => (
              <Button
                key={label}
                type="button"
                variant="outline"
                className="rounded-full"
                onClick={() => onConnectorPick(label)}
              >
                {label}
              </Button>
            ))}
            <Button type="button" variant="ghost" className="rounded-full" onClick={() => onConnectorPick("Not now")}>
              Not now
            </Button>
          </div>
        </MessageContent>
      </Message>
    );
  }
  if (step !== "contacts" && step !== "accessibility" && step !== "screen-recording") {
    return null;
  }
  if (permissionSettled(permissionStatus[step])) {
    return null;
  }
  const copy = permissionCopy(step);
  return (
    <Message from="assistant">
      <MessageContent variant="contained" className="space-y-3 leading-6">
        <div className="flex flex-wrap gap-2">
          <Button type="button" disabled={busy === step} onClick={() => void onRun(step)} className="rounded-full">
            {busy === step ? "Working..." : copy.action}
          </Button>
          {step !== "contacts" ? (
            <Button type="button" variant="outline" onClick={() => onContinue(step)} className="rounded-full">
              {copy.continueLabel}
            </Button>
          ) : null}
          <Button type="button" variant="ghost" onClick={() => onContinue(step)} className="rounded-full">
            Not now
          </Button>
        </div>
      </MessageContent>
    </Message>
  );
}
