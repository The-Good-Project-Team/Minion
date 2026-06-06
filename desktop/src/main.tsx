import React from "react";
import { createRoot } from "react-dom/client";
import "./index.css";
import { App } from "./App";
import { ErrorBoundary } from "./ErrorBoundary";
import { reportClientError } from "./lib/api";

// Catch errors that never reach a React boundary (async, event handlers, the
// renderer about to die). Forwarded only if the user opted into monitoring.
window.addEventListener("error", (e) => {
  void reportClientError(e.message || "window.onerror", e.error?.stack, {
    kind: "window.error",
    src: e.filename ? `${e.filename}:${e.lineno}:${e.colno}` : undefined,
  });
});
window.addEventListener("unhandledrejection", (e) => {
  const reason = (e.reason ?? "unhandledrejection") as { message?: string; stack?: string } | string;
  const msg = typeof reason === "string" ? reason : reason.message || "unhandledrejection";
  const stack = typeof reason === "string" ? undefined : reason.stack;
  void reportClientError(msg, stack, { kind: "unhandledrejection" });
});

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ErrorBoundary>
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
);
