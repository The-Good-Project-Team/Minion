import { useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { CheckCircle2, Circle, Database, FolderOpen, Plus, Settings, Sparkles, Upload } from "lucide-react";
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
  startIdentityCompanion,
  fetchSources,
  getConfig,
  ingestPath,
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

const MINION_LOGO = "/minion.png";

/** Types text out one character at a time with human-like, variable pauses. */
function useTypewriter(text: string, enabled: boolean): { shown: string; done: boolean } {
  const [shown, setShown] = useState(enabled ? "" : text);
  useEffect(() => {
    if (!enabled) {
      setShown(text);
      return;
    }
    let i = 0;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    setShown("");
    const tick = () => {
      if (cancelled) return;
      i += 1;
      setShown(text.slice(0, i));
      if (i >= text.length) return;
      const prev = text[i - 1];
      let delay = 18 + Math.random() * 22;
      if (prev === "." || prev === "!" || prev === "?") delay = 260 + Math.random() * 220;
      else if (prev === "," || prev === ";" || prev === ":") delay = 140 + Math.random() * 120;
      else if (prev === "\n") delay = 200 + Math.random() * 160;
      else if (Math.random() < 0.06) delay += 120 + Math.random() * 180;
      timer = setTimeout(tick, delay);
    };
    timer = setTimeout(tick, 120);
    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [text, enabled]);
  return { shown, done: shown.length >= text.length };
}

/** Minion's face; pulses a ring while it's speaking so you can see it talking to you. */
function SpeakingAvatar({ speaking }: { speaking?: boolean }) {
  return (
    <span className="relative inline-flex size-8 shrink-0 self-end">
      {speaking ? <span className="absolute inset-0 animate-ping rounded-full bg-primary/40" /> : null}
      <span
        className={cn(
          "relative grid size-8 place-items-center overflow-hidden rounded-full bg-card ring-1",
          speaking ? "ring-2 ring-primary/70" : "ring-border",
        )}
      >
        <img src={MINION_LOGO} alt="Minion" className="size-full object-cover" />
      </span>
    </span>
  );
}

function TypingIndicator() {
  return (
    <Message from="assistant">
      <SpeakingAvatar speaking />
      <MessageContent variant="contained" className="leading-6">
        <span className="inline-flex items-center gap-1 py-1.5">
          <span className="size-1.5 animate-bounce rounded-full bg-muted-foreground/60 [animation-delay:-0.3s]" />
          <span className="size-1.5 animate-bounce rounded-full bg-muted-foreground/60 [animation-delay:-0.15s]" />
          <span className="size-1.5 animate-bounce rounded-full bg-muted-foreground/60" />
        </span>
      </MessageContent>
    </Message>
  );
}

function AssistantMessage({
  children,
  contentClassName,
  stream,
  speaking,
}: {
  children?: ReactNode;
  contentClassName?: string;
  stream?: string;
  speaking?: boolean;
}) {
  const typed = useTypewriter(stream ?? "", stream != null);
  const isTyping = stream != null && !typed.done;
  return (
    <Message from="assistant">
      <SpeakingAvatar speaking={speaking || isTyping} />
      <MessageContent variant="contained" className={cn("whitespace-pre-wrap leading-6", contentClassName)}>
        {stream != null ? (
          <>
            {typed.shown}
            {isTyping ? <span className="ml-0.5 inline-block animate-pulse">▍</span> : null}
          </>
        ) : (
          children
        )}
      </MessageContent>
    </Message>
  );
}

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

type SourceOverview = {
  sourceCount: number;
  chunkCount: number;
  folderCount: number;
  aiExportCount: number;
  profileLinkCount: number;
  kindCounts: Record<string, number>;
};

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

function parentFolder(path: string): string | null {
  const clean = path.trim();
  if (!clean || !clean.includes("/")) return null;
  return clean.replace(/\/+$/, "").split("/").slice(0, -1).join("/") || "/";
}

function sourceOverviewFromSources(
  sources: Array<{ path: string; kind: string; chunk_count?: number; meta?: Record<string, unknown> }>,
  total: number,
  totalChunks = 0,
): SourceOverview {
  const paths = sources.map((source) => source.path);
  const searchable = sources.map((source) => `${source.path} ${String(source.meta?.title ?? "")} ${String(source.meta?.source ?? "")}`);
  const kindCounts = sources.reduce<Record<string, number>>((acc, source) => {
    acc[source.kind] = (acc[source.kind] ?? 0) + 1;
    return acc;
  }, {});
  return {
    sourceCount: total,
    chunkCount: totalChunks || sources.reduce((sum, source) => sum + (source.chunk_count ?? 0), 0),
    folderCount: new Set(paths.map(parentFolder).filter(Boolean)).size,
    aiExportCount: searchable.filter((text) => /chatgpt|openai|claude|anthropic|perplexity|gemini|cursor|copilot|grok/i.test(text)).length,
    profileLinkCount: searchable.filter((text) => /linkedin|facebook|github|instagram|twitter|x\.com|reddit|portfolio|personal-site/i.test(text)).length,
    kindCounts,
  };
}

/** Rank-ordered weights (sum = 100): earlier steps matter more for identity score. */
const IDENTITY_CHECKLIST_WEIGHTS = [15, 15, 12, 12, 10, 10, 8, 7, 6, 5] as const;

type IdentityChecklistId =
  | "name"
  | "ai_exports"
  | "profile_links"
  | "contacts"
  | "personal_sources"
  | "capture"
  | "people"
  | "projects_obligations"
  | "preferences"
  | "companion_chat";

type IdentityChecklistItem = {
  id: IdentityChecklistId;
  rank: number;
  label: string;
  detail: string;
  done: boolean;
  weight: number;
};

function buildIdentityChecklist(input: {
  displayName: string;
  contactsLoaded: boolean;
  sourceOverview: SourceOverview;
  permissionStatus: Record<string, string>;
  graphTotals: Record<string, number>;
  userChatted: boolean;
}): { items: IdentityChecklistItem[]; score: number; doneCount: number; next: IdentityChecklistItem | null } {
  const totals = input.graphTotals;
  const personCount = totals.person ?? 0;
  const captureReady =
    permissionSettled(input.permissionStatus.accessibility) &&
    permissionSettled(input.permissionStatus["screen-recording"]);
  const rows: Omit<IdentityChecklistItem, "rank" | "weight">[] = [
    {
      id: "name",
      label: "Name set",
      detail: input.displayName.trim() ? `Hi, ${input.displayName.trim()}` : "Tell Minion what to call you",
      done: Boolean(input.displayName.trim()),
    },
    {
      id: "ai_exports",
      label: "AI chat exports",
      detail: input.sourceOverview.aiExportCount
        ? `${input.sourceOverview.aiExportCount} export${input.sourceOverview.aiExportCount === 1 ? "" : "s"} added`
        : "ChatGPT, Claude, Cursor, etc.",
      done: input.sourceOverview.aiExportCount > 0,
    },
    {
      id: "profile_links",
      label: "Profile links",
      detail: input.sourceOverview.profileLinkCount
        ? `${input.sourceOverview.profileLinkCount} profile source${input.sourceOverview.profileLinkCount === 1 ? "" : "s"}`
        : "LinkedIn, GitHub, Facebook, site",
      done: input.sourceOverview.profileLinkCount > 0,
    },
    {
      id: "contacts",
      label: "Contacts loaded",
      detail: input.contactsLoaded
        ? personCount > 0
          ? `${personCount} people on this Mac`
          : "Contacts access granted"
        : "Import local Contacts",
      done: input.contactsLoaded,
    },
    {
      id: "personal_sources",
      label: "Personal files & folders",
      detail: input.sourceOverview.sourceCount
        ? `${input.sourceOverview.sourceCount} file${input.sourceOverview.sourceCount === 1 ? "" : "s"} · ${input.sourceOverview.folderCount} folder${input.sourceOverview.folderCount === 1 ? "" : "s"}`
        : "Add documents, notes, exports",
      done: input.sourceOverview.sourceCount > 0 || input.sourceOverview.folderCount > 0,
    },
    {
      id: "capture",
      label: "Screen & accessibility",
      detail: captureReady ? "Minion can read active work" : "Enable capture permissions",
      done: captureReady,
    },
    {
      id: "people",
      label: "People organized",
      detail: personCount >= 2 ? `${personCount} people mapped` : "Map who matters most",
      done: personCount >= 2,
    },
    {
      id: "projects_obligations",
      label: "Projects & obligations",
      detail:
        (totals.project ?? 0) + (totals.obligation ?? 0) > 0
          ? `${totals.project ?? 0} project${totals.project === 1 ? "" : "s"} · ${totals.obligation ?? 0} obligation${totals.obligation === 1 ? "" : "s"}`
          : "Work, promises, follow-ups",
      done: (totals.project ?? 0) > 0 || (totals.obligation ?? 0) > 0,
    },
    {
      id: "preferences",
      label: "Preferences saved",
      detail: (totals.preference ?? 0) > 0 ? `${totals.preference} preference${totals.preference === 1 ? "" : "s"}` : "How you like work to feel",
      done: (totals.preference ?? 0) > 0,
    },
    {
      id: "companion_chat",
      label: "Companion chat started",
      detail: input.userChatted ? "You're organizing with Minion" : "Send your first organizing message",
      done: input.userChatted,
    },
  ];
  const items = rows.map((row, idx) => ({
    ...row,
    rank: idx + 1,
    weight: IDENTITY_CHECKLIST_WEIGHTS[idx] ?? 5,
  }));
  const score = items.reduce((sum, item) => sum + (item.done ? item.weight : 0), 0);
  const doneCount = items.filter((item) => item.done).length;
  const next = items.find((item) => !item.done) ?? null;
  return { items, score, doneCount, next };
}

function identityScoreLabel(score: number): string {
  if (score >= 90) return "Grounded";
  if (score >= 70) return "Taking shape";
  if (score >= 45) return "Getting started";
  return "Just beginning";
}

const CHECKLIST_CTA: Record<IdentityChecklistId, string> = {
  name: "Set name",
  ai_exports: "Import",
  profile_links: "Add",
  contacts: "Import",
  personal_sources: "Add files",
  capture: "Enable",
  people: "Organize",
  projects_obligations: "Organize",
  preferences: "Set",
  companion_chat: "Chat",
};

const ONBOARDING_FLOW: { step: OnboardingStep; label: string; blurb: string }[] = [
  { step: "name", label: "Say hello", blurb: "Tell Minion what to call you." },
  { step: "contacts", label: "Add your people", blurb: "Let Minion know who matters. You can skip this." },
  { step: "accessibility", label: "Help with your work", blurb: "So Minion can see what you're working on. You can skip this." },
  { step: "screen-recording", label: "Help with your screen", blurb: "For screenshots and open windows. You can skip this." },
  { step: "connector", label: "Connect your stuff", blurb: "Pick something to remember — a folder, Gmail, or Slack." },
];

function OnboardingProgress({ step }: { step: OnboardingStep }) {
  const order = ONBOARDING_FLOW.map((s) => s.step);
  // resource_poll happens just before "connect your stuff"; done is past the end.
  const currentIdx =
    step === "done"
      ? ONBOARDING_FLOW.length
      : step === "resource_poll"
        ? order.indexOf("connector")
        : Math.max(0, order.indexOf(step));
  const finishing = step === "done";
  const pct = Math.round((currentIdx / ONBOARDING_FLOW.length) * 100);
  return (
    <div className="relative z-10 mt-8 flex flex-1 flex-col">
      <div className="mb-5 h-2 overflow-hidden rounded-full bg-muted">
        <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${Math.max(6, pct)}%` }} />
      </div>
      <ol className="space-y-3">
        {ONBOARDING_FLOW.map((s, idx) => {
          const done = idx < currentIdx;
          const current = idx === currentIdx && !finishing;
          return (
            <li key={s.step} className="flex items-start gap-3">
              {done ? (
                <CheckCircle2 className="mt-0.5 size-5 shrink-0 text-primary" />
              ) : current ? (
                <span className="mt-0.5 grid size-5 shrink-0 place-items-center rounded-full bg-primary text-[11px] font-bold text-primary-foreground">
                  {idx + 1}
                </span>
              ) : (
                <Circle className="mt-0.5 size-5 shrink-0 text-muted-foreground/40" />
              )}
              <div className="min-w-0">
                <div
                  className={cn(
                    "text-sm font-semibold",
                    current ? "text-foreground" : done ? "text-muted-foreground" : "text-muted-foreground/60",
                  )}
                >
                  {s.label}
                </div>
                {current ? <div className="mt-0.5 text-xs leading-5 text-muted-foreground">{s.blurb}</div> : null}
              </div>
            </li>
          );
        })}
      </ol>
      <div className="mt-auto rounded-2xl border border-primary/15 bg-primary/5 p-3 text-xs leading-5 text-muted-foreground">
        Minion asks before anything private. Your data stays on this Mac.
      </div>
    </div>
  );
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
  const [fileDragging, setFileDragging] = useState(false);
  const [fileOverZone, setFileOverZone] = useState(false);
  const [dropNotices, setDropNotices] = useState<string[]>([]);
  const dropZoneRef = useRef<HTMLDivElement | null>(null);
  const dropFilesRef = useRef<(files: FileList | null) => void>(() => {});
  const [sourceOverview, setSourceOverview] = useState<SourceOverview>({
    sourceCount: 0,
    chunkCount: 0,
    folderCount: 0,
    aiExportCount: 0,
    profileLinkCount: 0,
    kindCounts: {},
  });
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
    } catch {
      if (onboardingStepRef.current !== step) return;
      const friendly =
        step === "contacts" || step === "accessibility" || step === "screen-recording"
          ? permissionCopy(step).body
          : "I had a little trouble connecting just now — nothing to worry about, your data stays on this Mac. Let's keep going.";
      setOnboardingMessages((prev) => [...prev, { role: "assistant", content: friendly }]);
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
      void fetchSources({ limit: 500 })
        .then((out) => setSourceOverview(sourceOverviewFromSources(out.sources, out.counts.sources, out.counts.chunks)))
        .catch(() => {});
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
  const contactsLoaded = permissionStatus.contacts === "granted" || (bundle?.graph?.totals?.person ?? 0) > 0;
  const dataChecklist = [
    {
      label: "Contacts",
      value: bundle?.graph?.totals?.person ?? 0,
      detail: contactsLoaded ? "people remembered" : "add your contacts",
      done: contactsLoaded,
    },
    {
      label: "Files",
      value: sourceOverview.sourceCount,
      detail: sourceOverview.sourceCount > 0 ? "ready to search" : "add files",
      done: sourceOverview.sourceCount > 0,
    },
    {
      label: "Projects",
      value: bundle?.graph?.totals?.project ?? 0,
      detail: "things you're working on",
      done: (bundle?.graph?.totals?.project ?? 0) > 0,
    },
    {
      label: "Follow-ups",
      value: bundle?.graph?.totals?.obligation ?? 0,
      detail: "things to come back to",
      done: (bundle?.graph?.totals?.obligation ?? 0) > 0,
    },
    {
      label: "Screen",
      value: sourceOverview.kindCounts["screen-event"] ?? 0,
      detail: "moments saved",
      done: (sourceOverview.kindCounts["screen-event"] ?? 0) > 0,
    },
  ];
  const userChatted = conversationRows.some((item) => itemRole(item) === "user");
  const identityChecklist = useMemo(
    () =>
      buildIdentityChecklist({
        displayName,
        contactsLoaded,
        sourceOverview,
        permissionStatus,
        graphTotals: bundle?.graph?.totals ?? {},
        userChatted,
      }),
    [displayName, contactsLoaded, sourceOverview, permissionStatus, bundle?.graph?.totals, userChatted],
  );
  const bootingSidecar = !bundle && sidecarStatus.state !== "error";
  const sidecarLabel =
    sidecarStatus.state === "ready"
      ? "Ready"
      : sidecarStatus.state === "error"
        ? "Offline"
        : sidecarStatus.state === "installing"
          ? "Setting up"
          : "Starting";
  const composerOnboardingSteps: OnboardingStep[] = ["name", "connector"];
  const canComposeOnboarding = onboardingActive && composerOnboardingSteps.includes(onboardingStep);

  async function ensureThread(): Promise<string | null> {
    if (activeThreadRef.current) return activeThreadRef.current;
    const next = await agentNext();
    if (!next.thread) {
      const companionThread = await startIdentityCompanion();
      const tid = companionThread.thread?.thread_id ?? null;
      activeThreadRef.current = tid;
      await load();
      return tid;
    }
    const tid = next.thread?.thread_id ?? null;
    activeThreadRef.current = tid;
    await load();
    return tid;
  }

  async function startCompanionPrompt(prompt?: string) {
    if (busy || streaming) return;
    const out = await startIdentityCompanion();
    activeThreadRef.current = out.thread?.thread_id ?? activeThreadRef.current;
    await load();
    if (prompt) setDraft(prompt);
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
      try {
        await recordConnectorIntent({ source_text: source });
      } catch {
        /* connector intent is best-effort; setup should still finish */
      }
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

  async function addPathsToInbox(paths: string[]) {
    if (paths.length === 0 || quickBusy) return;
    setQuickBusy(true);
    setQuickStatus("");
    try {
      await Promise.all(paths.map((path) => ingestPath(path, false, true)));
      setQuickStatus(
        `Queued ${paths.length} item${paths.length === 1 ? "" : "s"} for temporary embedding. Original files stay in place.`,
      );
      await load();
    } catch (e) {
      setQuickStatus(e instanceof Error ? e.message : String(e));
    } finally {
      setQuickBusy(false);
    }
  }

  async function addFiles() {
    if (quickBusy) return;
    try {
      const selected = await open({
        multiple: true,
        directory: false,
        title: "Add files to Minion",
      });
      const paths = Array.isArray(selected) ? selected : selected ? [selected] : [];
      await addPathsToInbox(paths);
    } catch (e) {
      setQuickStatus(e instanceof Error ? e.message : String(e));
    }
  }

  async function addFolder() {
    if (quickBusy) return;
    try {
      const selected = await open({
        multiple: false,
        directory: true,
        title: "Add a folder to Minion",
      });
      const paths = Array.isArray(selected) ? selected : selected ? [selected] : [];
      await addPathsToInbox(paths);
    } catch (e) {
      setQuickStatus(e instanceof Error ? e.message : String(e));
    }
  }

  function pushDropNotice(text: string) {
    setDropNotices((prev) => [...prev, text]);
  }

  async function handleDroppedFiles(files: FileList | null) {
    if (!files || files.length === 0) return;
    const arr = Array.from(files);
    const names = arr.map((f) => f.name).filter(Boolean);
    const nameList =
      names.length === 0
        ? "your files"
        : `${names.slice(0, 3).join(", ")}${names.length > 3 ? ` +${names.length - 3} more` : ""}`;
    const them = arr.length === 1 ? "it" : "them";
    // Acknowledge instantly so the drop always gets a response in the stream.
    pushDropNotice(
      `Got ${nameList} — saving ${arr.length} file${arr.length === 1 ? "" : "s"} to memory now. Ask me about ${them} anytime.`,
    );
    const nativePaths = arr
      .map((f) => (f as File & { path?: string }).path)
      .filter((p): p is string => typeof p === "string" && p.length > 0);
    if (nativePaths.length > 0) {
      try {
        await addPathsToInbox(nativePaths);
      } catch (e) {
        pushDropNotice(`Actually, that didn't save: ${e instanceof Error ? e.message : String(e)}`);
      }
      return;
    }
    setQuickBusy(true);
    setQuickStatus("");
    try {
      let count = 0;
      for (const f of arr) {
        const text = await f.text();
        if (!text.trim()) continue;
        await ingestText({ title: f.name || "Dropped file", text });
        count += 1;
      }
      if (count > 0) {
        setQuickStatus(`Saved ${count} dropped file${count === 1 ? "" : "s"}. Minion is indexing.`);
      } else {
        pushDropNotice("Hmm — I couldn't read those as text. Try the “Add files” button so I can index them.");
        setQuickStatus("I couldn't read those as text — try Add files instead.");
      }
      await load();
    } catch (e) {
      const msg = e instanceof Error ? e.message : String(e);
      pushDropNotice(`Actually, that didn't save: ${msg}`);
      setQuickStatus(msg);
    } finally {
      setQuickBusy(false);
    }
  }

  dropFilesRef.current = handleDroppedFiles;

  useEffect(() => {
    const hasFiles = (e: DragEvent) => Array.from(e.dataTransfer?.types ?? []).includes("Files");
    const overZone = (e: DragEvent) => {
      const el = dropZoneRef.current;
      if (!el) return false;
      const r = el.getBoundingClientRect();
      return e.clientX >= r.left && e.clientX <= r.right && e.clientY >= r.top && e.clientY <= r.bottom;
    };
    let depth = 0;
    const onEnter = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      depth += 1;
      setFileDragging(true);
    };
    const onOver = (e: DragEvent) => {
      if (!hasFiles(e)) return;
      e.preventDefault();
      setFileOverZone(overZone(e));
    };
    const onLeave = () => {
      depth = Math.max(0, depth - 1);
      if (depth === 0) {
        setFileDragging(false);
        setFileOverZone(false);
      }
    };
    const onDrop = (e: DragEvent) => {
      e.preventDefault();
      depth = 0;
      setFileDragging(false);
      setFileOverZone(false);
      dropFilesRef.current(e.dataTransfer?.files ?? null);
    };
    window.addEventListener("dragenter", onEnter);
    window.addEventListener("dragover", onOver);
    window.addEventListener("dragleave", onLeave);
    window.addEventListener("drop", onDrop);
    return () => {
      window.removeEventListener("dragenter", onEnter);
      window.removeEventListener("dragover", onOver);
      window.removeEventListener("dragleave", onLeave);
      window.removeEventListener("drop", onDrop);
    };
  }, []);

  function runChecklistAction(id: IdentityChecklistId) {
    switch (id) {
      case "name":
        setDraft(displayName.trim() ? `I'm ${displayName.trim()}. ` : "My name is ");
        if (onboardingStep === "name") return;
        break;
      case "contacts":
        void runPermissionStep("contacts");
        return;
      case "ai_exports":
        void startCompanionPrompt("Help me import my ChatGPT, Claude, Cursor, and other AI conversation exports.");
        return;
      case "profile_links":
        setQuickTitle("Profile links");
        setQuickText("LinkedIn:\nGitHub:\nFacebook:\nWebsite:\nOther profiles:");
        return;
      case "personal_sources":
        void addFiles();
        return;
      case "capture":
        if (!permissionSettled(permissionStatus.accessibility)) {
          void runPermissionStep("accessibility");
          return;
        }
        if (!permissionSettled(permissionStatus["screen-recording"])) {
          void runPermissionStep("screen-recording");
          return;
        }
        break;
      case "people":
        void startCompanionPrompt("Help me organize the most important people in my world.");
        return;
      case "projects_obligations":
        void startCompanionPrompt("Ask me what projects and obligations matter right now.");
        return;
      case "preferences":
        void startCompanionPrompt("Learn how I like decisions, writing, and reminders to work.");
        return;
      case "companion_chat":
        void startCompanionPrompt();
        return;
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
          <div className="space-y-3">
            <h1 className="font-serif text-4xl leading-[0.95] tracking-[-0.04em]">
              Your memory stays yours.
            </h1>
            <p className="max-w-[24ch] text-sm leading-6 text-muted-foreground">
              I help you remember people, files, and answers on this Mac — one useful question at a time.
            </p>
          </div>
        </div>

        {onboardingActive ? (
          <OnboardingProgress step={onboardingStep} />
        ) : (
        <>
        <div className="relative z-10 mt-8 rounded-[2rem] border border-primary/15 bg-background/80 p-4 shadow-xl shadow-slate-950/5">
          <div className="mb-4 flex items-start justify-between gap-3">
            <div>
              <p className="text-[11px] font-bold uppercase tracking-[0.16em] text-primary">Your memory</p>
              <p className="mt-1 text-xs leading-5 text-muted-foreground">
                {identityScoreLabel(identityChecklist.score)} · {identityChecklist.doneCount} of 10 done · {sidecarLabel}
              </p>
            </div>
            <span className={cn("rounded-full px-2 py-1 text-xs", bootingSidecar ? "bg-amber-500/10 text-amber-700" : "bg-primary/10 text-primary")}>
              {bootingSidecar ? "Starting" : "Ready"}
            </span>
          </div>

          <div className="mb-4 grid grid-cols-2 gap-2">
            {dataChecklist.map((row) => (
              <div key={row.label} className="rounded-2xl border border-border bg-card/80 p-3">
                <div className="flex items-center justify-between gap-2">
                  <div className="font-serif text-2xl leading-none tracking-[-0.04em]">{row.value}</div>
                  {row.done ? <CheckCircle2 className="size-4 text-primary" /> : <Circle className="size-4 text-muted-foreground" />}
                </div>
                <div className="mt-1 text-[10px] font-semibold uppercase tracking-[0.12em] text-muted-foreground">{row.label}</div>
                <div className="mt-1 truncate text-[11px] text-muted-foreground">{row.detail}</div>
              </div>
            ))}
          </div>

          <div className="mb-3 h-2 overflow-hidden rounded-full bg-muted">
            <div className="h-full rounded-full bg-primary transition-all" style={{ width: `${Math.max(4, identityChecklist.score)}%` }} />
          </div>

          <div className="space-y-2">
            {identityChecklist.items.slice(0, 6).map((item) => (
              <div
                key={item.id}
                className={cn(
                  "flex w-full min-w-0 items-center gap-3 rounded-2xl border p-3 transition",
                  item.done ? "border-primary/20 bg-primary/5" : "border-border bg-card/75",
                  identityChecklist.next?.id === item.id && !item.done ? "ring-1 ring-primary/30" : "",
                )}
              >
                {item.done ? <CheckCircle2 className="size-4 shrink-0 text-primary" /> : <Circle className="size-4 shrink-0 text-muted-foreground" />}
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-xs font-semibold">{item.label}</span>
                  <span className="block truncate text-[11px] text-muted-foreground">{item.detail}</span>
                </span>
                {item.done ? (
                  <span className="shrink-0 text-[11px] font-semibold text-primary">Done</span>
                ) : (
                  <Button
                    type="button"
                    size="sm"
                    variant="outline"
                    className="h-7 shrink-0 rounded-full px-3 text-xs"
                    disabled={quickBusy || permissionBusy === "contacts"}
                    onClick={() => runChecklistAction(item.id)}
                  >
                    {CHECKLIST_CTA[item.id]}
                  </Button>
                )}
              </div>
            ))}
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
        </>
        )}
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
                      : "Let's organize your world."}
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
                      <AssistantMessage>One moment — I'm waking up on this Mac.</AssistantMessage>
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
                      <AssistantMessage>{sessionOpen.briefing_md}</AssistantMessage>
                    ) : null}
                    {conversationRows.map((item) =>
                      itemRole(item) === "assistant" ? (
                        <AssistantMessage key={itemKey(item)}>
                          <div>{itemText(item)}</div>
                          <div className="mt-2 text-[11px] opacity-55">{timeLabel(item.ts)}</div>
                        </AssistantMessage>
                      ) : (
                        <Message key={itemKey(item)} from="user">
                          <MessageContent variant="contained" className="whitespace-pre-wrap leading-6">
                            <div>{itemText(item)}</div>
                            <div className="mt-2 text-[11px] opacity-55">{timeLabel(item.ts)}</div>
                          </MessageContent>
                        </Message>
                      ),
                    )}
                  </>
                )}
                {dropNotices.map((notice, i) => (
                  <AssistantMessage key={`drop-${i}`} stream={notice} />
                ))}
                {streaming && streamBuf ? <AssistantMessage speaking>{streamBuf}</AssistantMessage> : null}
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
                          ? "Tap a button above to continue..."
                        : awaitingAnswer
                      ? "Answer Minion — who is this, how do you know them, or skip for now…"
                      : "Ask Minion to organize people, projects, obligations, or anything on your mind…"
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
                      (onboardingActive && !canComposeOnboarding)
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

      <div
        ref={dropZoneRef}
        aria-hidden
        className={cn(
          "pointer-events-none fixed right-6 bottom-28 z-50 flex flex-col items-center justify-center gap-1.5 rounded-2xl border-2 border-dashed text-center font-semibold shadow-xl transition-all duration-200",
          fileOverZone
            ? "size-40 scale-105 border-emerald-500 bg-emerald-500/20 text-emerald-700"
            : fileDragging
              ? "size-40 border-primary/60 bg-card/95 text-primary"
              : "size-24 border-border/70 bg-card/85 text-[11px] text-muted-foreground backdrop-blur",
        )}
      >
        <Upload
          className={cn(
            fileOverZone ? "size-7 text-emerald-600" : fileDragging ? "size-7" : "size-5",
          )}
        />
        {fileOverZone ? "Drop to remember" : fileDragging ? "Drop files here" : "Drop files"}
      </div>
    </main>
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
  let lastAssistantIdx = -1;
  for (let i = messages.length - 1; i >= 0; i -= 1) {
    if (messages[i].role === "assistant") {
      lastAssistantIdx = i;
      break;
    }
  }
  return (
    <>
      {messages.map((message, idx) =>
        message.role === "user" ? (
          <Message key={`${message.role}-${idx}`} from="user">
            <MessageContent variant="contained" className="whitespace-pre-wrap leading-6">
              {message.content}
            </MessageContent>
          </Message>
        ) : idx === lastAssistantIdx ? (
          <AssistantMessage key={`${message.role}-${idx}`} stream={message.content} />
        ) : (
          <AssistantMessage key={`${message.role}-${idx}`}>{message.content}</AssistantMessage>
        ),
      )}
      {prompting ? <TypingIndicator /> : null}
      {permissionNote ? (
        <AssistantMessage>
          <div className="mb-1 flex items-center gap-2 text-primary">
            <CheckCircle2 className="size-4" />
            <span className="text-[11px] font-bold uppercase tracking-[0.14em]">Permission update</span>
          </div>
          {permissionNote}
        </AssistantMessage>
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
      <AssistantMessage contentClassName="space-y-4">
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
      </AssistantMessage>
    );
  }
  if (step === "done") {
    return (
      <AssistantMessage contentClassName="flex items-center justify-between gap-3">
        <span>You're all set. Ask me anything.</span>
        <Button type="button" size="sm" onClick={onFinish}>
          Start using Minion
        </Button>
      </AssistantMessage>
    );
  }
  if (step === "connector") {
    return (
      <AssistantMessage contentClassName="space-y-3">
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
      </AssistantMessage>
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
    <AssistantMessage contentClassName="space-y-3">
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
    </AssistantMessage>
  );
}
