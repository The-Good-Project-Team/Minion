import { Component, type ErrorInfo, type ReactNode } from "react";
import { reportClientError } from "./lib/api";

type Props = { children: ReactNode };
type State = { error: Error | null };

/**
 * Last-resort net so a render-time throw doesn't unmount the whole tree into a
 * blank white window. Shows a recoverable error card instead of nothing, and
 * auto-reloads once after a short delay (the common case is a transient state
 * glitch during heavy ingest). A reload counter in sessionStorage prevents a
 * crash loop from reloading forever.
 */
export class ErrorBoundary extends Component<Props, State> {
  private reloadTimer: ReturnType<typeof setTimeout> | null = null;

  constructor(props: Props) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    // Surface for diagnostics/log scraping; the white-screen bug was invisible.
    console.error("Minion UI crashed:", error, info.componentStack);
    void reportClientError(error.message || "render crash", error.stack, {
      kind: "react.boundary",
      componentStack: (info.componentStack || "").slice(0, 1500),
    });
    let tries = 0;
    try {
      tries = Number(sessionStorage.getItem("minion:reload-tries") ?? "0");
    } catch {
      /* ignore */
    }
    if (tries < 2) {
      try {
        sessionStorage.setItem("minion:reload-tries", String(tries + 1));
      } catch {
        /* ignore */
      }
      this.reloadTimer = setTimeout(() => window.location.reload(), 1200);
    }
  }

  componentWillUnmount() {
    if (this.reloadTimer) clearTimeout(this.reloadTimer);
  }

  render() {
    if (this.state.error) {
      return (
        <div className="flex min-h-screen items-center justify-center bg-background p-8 text-foreground">
          <div className="w-full max-w-md rounded-2xl border border-border bg-card p-8 text-center shadow-sm">
            <h1 className="font-serif text-2xl">Minion hit a snag</h1>
            <p className="mt-2 text-sm text-muted-foreground">
              The screen ran into an error and is reloading. Your memory and indexed files are safe.
            </p>
            <button
              onClick={() => window.location.reload()}
              className="mt-5 rounded-lg bg-primary px-4 py-2 text-sm font-medium text-primary-foreground"
            >
              Reload now
            </button>
          </div>
        </div>
      );
    }
    return this.props.children;
  }
}

export default ErrorBoundary;
