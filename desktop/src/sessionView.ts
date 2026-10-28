import { type Session, type Snapshot } from "./protocol";

export type SessionFilter =
  "all" | "attention" | "working" | "idle" | "attached" | "detached" | "app";
export function sessionStatus(
  session: Session,
  remoteStatus: Snapshot["remoteStatus"] = [],
) {
  if (
    remoteStatus.some(
      (host) =>
        host.host === (session.host || "local") && host.status === "incomplete",
    )
  )
    return { label: "Unverified", tone: "unknown", group: "unknown" };
  const state = String(session.state || "unknown")
    .toLowerCase()
    .replace(/_/g, "-");
  if (["needs-input", "waiting", "blocked", "error"].includes(state))
    return {
      label:
        state === "needs-input"
          ? "Needs input"
          : state[0].toUpperCase() + state.slice(1),
      tone: "attention",
      group: "attention",
    };
  if (["working", "running"].includes(state))
    return {
      label: state === "working" ? "Working" : "Running",
      tone: "working",
      group: "working",
    };
  if (state === "idle") return { label: "Idle", tone: "idle", group: "idle" };
  if (["attached", "detached", "app"].includes(state))
    return {
      label: (
        {
          attached: "Tmux attached",
          detached: "Tmux detached",
          app: "External app",
        } as Record<string, string>
      )[state],
      tone: "unknown",
      group: state,
    };
  if (["live", "foreground"].includes(state))
    return {
      label: state === "live" ? "Live" : "Foreground",
      tone: "unknown",
      group: "unknown",
    };
  // Process existence and tmux attachment do not establish agent activity.
  return {
    label:
      state === "unknown" ? "Unknown" : state[0].toUpperCase() + state.slice(1),
    tone: "unknown",
    group: "unknown",
  };
}

// Transcript metadata may enrich presentation, never overwrite the live target.
export function enrichSession(
  session: Session,
  detail: Record<string, unknown>,
): Session {
  if (!detail || typeof detail !== "object") return session;
  const enriched = { ...session };
  for (const field of ["branch", "issue", "pr"] as const) {
    if (!session[field] && typeof detail[field] === "string")
      enriched[field] = detail[field];
  }
  return enriched;
}
