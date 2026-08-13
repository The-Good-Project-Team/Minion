import { useCallback, useEffect, useState } from "react";
import { CheckCircle2, Loader2, XCircle, ListTodo } from "lucide-react";
import {
  apiErrorDetail,
  fetchTasks,
  patchTask,
  type WorkItem,
} from "../lib/api";
import { formatFeedTime } from "../lib/feedUtils";

const WORK_ORIGINS = ["screen_memory", "agent", "inferred"];
const OPEN_STATUSES = ["open", "review"];

type WorkQueueProps = {
  onReveal?: (path: string) => void | Promise<void>;
};

function contextRefLabel(ref: unknown): string | null {
  if (!ref || typeof ref !== "object") return null;
  const r = ref as Record<string, unknown>;
  if (typeof r.path === "string") return r.path;
  if (typeof r.kind === "string" && typeof r.id === "string") return `${r.kind}:${r.id}`;
  if (typeof r.screen_event_id === "string") return `screen:${r.screen_event_id}`;
  return null;
}

export function WorkQueue({ onReveal }: WorkQueueProps) {
  const [tasks, setTasks] = useState<WorkItem[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetchTasks({
        origin: WORK_ORIGINS.join(","),
        status: OPEN_STATUSES.join(","),
        limit: 100,
      });
      setTasks(res.tasks);
    } catch (e) {
      setError(apiErrorDetail(e));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  async function acceptTask(taskId: string) {
    setBusyId(taskId);
    try {
      await patchTask(taskId, { status: "open" });
      setTasks((prev) =>
        prev.map((t) => (t.task_id === taskId ? { ...t, status: "open" } : t)),
      );
    } catch (e) {
      setError(apiErrorDetail(e));
    } finally {
      setBusyId(null);
    }
  }

  async function dismissTask(taskId: string) {
    setBusyId(taskId);
    try {
      await patchTask(taskId, { status: "archived" });
      setTasks((prev) => prev.filter((t) => t.task_id !== taskId));
    } catch (e) {
      setError(apiErrorDetail(e));
    } finally {
      setBusyId(null);
    }
  }

  return (
    <section className="mt-6 rounded-2xl border border-border bg-card p-4">
      <div className="mb-4 flex items-center justify-between gap-2">
        <h2 className="inline-flex items-center gap-1.5 text-lg font-medium">
          <ListTodo className="size-5 text-primary" /> Work queue
        </h2>
        <button
          type="button"
          onClick={() => void load()}
          className="text-xs text-muted-foreground hover:text-foreground"
        >
          Refresh
        </button>
      </div>
      <p className="mb-4 text-sm text-muted-foreground">
        Suggested tasks from screen memory and agents. Accept to keep them active; dismiss to archive.
      </p>

      {loading && tasks.length === 0 ? (
        <div className="flex items-center justify-center py-10">
          <Loader2 className="size-6 animate-spin text-muted-foreground" />
        </div>
      ) : error ? (
        <p className="text-sm text-red-600">{error}</p>
      ) : tasks.length === 0 ? (
        <p className="py-6 text-center text-sm text-muted-foreground">No suggested work items right now.</p>
      ) : (
        <ul className="space-y-3">
          {tasks.map((task) => {
            const busy = busyId === task.task_id;
            const refs = Array.isArray(task.context_refs) ? task.context_refs : [];
            return (
              <li
                key={task.task_id}
                className="rounded-lg border border-border bg-muted/20 p-3"
              >
                <div className="flex flex-wrap items-start justify-between gap-3">
                  <div className="min-w-0 flex-1">
                    <p className="font-medium">{task.title}</p>
                    <p className="mt-0.5 text-xs text-muted-foreground">
                      {task.origin.replace(/_/g, " ")} · {task.status} · {formatFeedTime(task.updated_at)}
                    </p>
                    {task.body_md?.trim() && (
                      <p className="mt-2 text-sm text-muted-foreground line-clamp-3">{task.body_md.trim()}</p>
                    )}
                    {refs.length > 0 && (
                      <ul className="mt-2 space-y-1 text-xs">
                        {refs.slice(0, 3).map((ref, idx) => {
                          const label = contextRefLabel(ref);
                          if (!label) return null;
                          const path =
                            ref && typeof ref === "object" && typeof (ref as { path?: string }).path === "string"
                              ? (ref as { path: string }).path
                              : null;
                          return (
                            <li key={idx}>
                              {path && onReveal ? (
                                <button
                                  type="button"
                                  onClick={() => void onReveal(path)}
                                  className="text-primary hover:underline"
                                >
                                  {label}
                                </button>
                              ) : (
                                <span className="text-muted-foreground">{label}</span>
                              )}
                            </li>
                          );
                        })}
                      </ul>
                    )}
                  </div>
                  <div className="flex shrink-0 gap-1.5">
                    {task.status === "review" ? (
                      <button
                        type="button"
                        disabled={busy}
                        onClick={() => void acceptTask(task.task_id)}
                        className="inline-flex items-center gap-1 rounded-lg bg-primary px-2.5 py-1.5 text-xs font-medium text-primary-foreground disabled:opacity-50"
                      >
                        {busy ? (
                          <Loader2 className="size-3 animate-spin" />
                        ) : (
                          <CheckCircle2 className="size-3" />
                        )}
                        Accept
                      </button>
                    ) : (
                      <span className="inline-flex items-center rounded-lg bg-emerald-100 px-2.5 py-1.5 text-xs text-emerald-800 dark:bg-emerald-950/40 dark:text-emerald-200">
                        Active
                      </span>
                    )}
                    <button
                      type="button"
                      disabled={busy}
                      onClick={() => void dismissTask(task.task_id)}
                      className="inline-flex items-center gap-1 rounded-lg border border-border px-2.5 py-1.5 text-xs hover:bg-accent disabled:opacity-50"
                    >
                      <XCircle className="size-3" /> Dismiss
                    </button>
                  </div>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}
