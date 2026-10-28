import { listen } from "@tauri-apps/api/event";
import React, { useCallback, useEffect, useState } from "react";
import { createRoot } from "react-dom/client";
import {
  Command,
  Plus,
  RefreshCw,
  Settings,
  ChevronRight,
  FolderGit2,
  Monitor,
  PanelRight,
  X,
  TerminalSquare,
  Search,
  Check,
  AlertCircle,
  Play,
  Server,
} from "lucide-react";
import { request, reconnect } from "./transport";
import { TerminalPane } from "./TerminalPane";
import {
  emptySnapshot,
  mcpCapabilities,
  title,
  key,
  parseArgs,
  type Snapshot,
  type Session,
  type Capability,
  type Operation,
} from "./protocol";
import "./style.css";
import { ResultView } from "./ResultView";
import { SessionList } from "./SessionList";
import { sessionStatus, enrichSession } from "./sessionView";
import { CoreForm, coreParams, target } from "./CoreForm";
type Tab = {
  id: string;
  token: string;
  name: string;
  session?: Session;
  operationId?: string;
  ended?: boolean;
};
function savedPending(): string[] {
  try {
    const value: unknown = JSON.parse(
      localStorage.getItem("t.pending") || "[]",
    );
    return Array.isArray(value)
      ? value.filter((id): id is string => typeof id === "string").slice(0, 100)
      : [];
  } catch {
    return [];
  }
}
function App() {
  const [snapshot, setSnapshot] = useState<Snapshot>(emptySnapshot);
  const [status, setStatus] = useState<"loading" | "ready" | "error">(
    "loading",
  );
  const [error, setError] = useState("");
  const [scope, setScope] = useState("all");
  const [host, setHost] = useState("all");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<Session>();
  const [tabs, setTabs] = useState<Tab[]>([]);
  const [active, setActive] = useState("");
  const [inspector, setInspector] = useState(true);
  const [palette, setPalette] = useState(false);
  const [settings, setSettings] = useState(false);
  const [action, setAction] = useState<Capability>();
  const [args, setArgs] = useState("");
  const [cwd, setCwd] = useState("");
  const [prepared, setPrepared] = useState<Operation>();
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<unknown>();
  const [theme, setTheme] = useState(
    localStorage.getItem("t.theme") || "system",
  );
  const [paletteQuery, setPaletteQuery] = useState("");
  const [coreValues, setCoreValues] = useState<Record<string, string>>({});
  const [pending, setPending] = useState<string[]>(savedPending);
  const [recovery, setRecovery] = useState(false);
  const [terminalHeight, setTerminalHeight] = useState(37);
  const [progress, setProgress] = useState("");
  const dismissAction = useCallback(() => {
    if (prepared?.state === "prepared")
      setPending((ids) => ids.filter((id) => id !== prepared.operationId));
    setAction(undefined);
    setPrepared(undefined);
  }, [prepared]);
  const [remoteLoading, setRemoteLoading] = useState(false);
  const refresh = useCallback(async () => {
    const install = (data: Snapshot) =>
      setSnapshot({
        ...emptySnapshot,
        ...data,
        capabilities: [...data.capabilities, ...mcpCapabilities],
      });
    try {
      const local = await request<Snapshot>("workspace.snapshot", {
        all_hosts: false,
      });
      install(local);
      setStatus("ready");
      if (host !== "local" && local.hosts.length) {
        setRemoteLoading(true);
        try {
          install(
            await request<Snapshot>("workspace.snapshot", { all_hosts: true }),
          );
        } finally {
          setRemoteLoading(false);
        }
      }
    } catch (e) {
      setError(String(e));
      setStatus("error");
    }
  }, [host]);

  useEffect(() => {
    let disposed = false;
    let unlisten: (() => void) | undefined;
    void listen<{ stage: string; operationId?: string }>(
      "workspace-progress",
      (event) => {
        if (!disposed)
          setProgress(
            event.payload.stage === "completed"
              ? "Operation complete"
              : "Working through shared core…",
          );
      },
    )
      .then((fn) => {
        if (disposed) fn();
        else unlisten = fn;
      })
      .catch(() => {});
    return () => {
      disposed = true;
      unlisten?.();
    };
  }, []);
  useEffect(() => {
    void refresh();
    const interval = setInterval(() => void refresh(), 15000);
    return () => clearInterval(interval);
  }, [refresh]);
  useEffect(() => {
    localStorage.setItem("t.pending", JSON.stringify(pending));
  }, [pending]);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    localStorage.setItem("t.theme", theme);
  }, [theme]);
  useEffect(() => {
    const handler = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === "k") {
        e.preventDefault();
        setPalette((v) => !v);
      }
      if (e.key === "Escape") {
        setPalette(false);
        setSettings(false);
        if (!busy) dismissAction();
      }
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [busy, dismissAction]);
  useEffect(() => {
    if (!palette && !action && !settings && !recovery) return;
    const previous = document.activeElement as HTMLElement;
    const dialog = document.querySelector<HTMLElement>(
      ".overlay:last-of-type [role=dialog]",
    );
    const first =
      dialog?.querySelector<HTMLElement>("input,select,textarea") ||
      dialog?.querySelector<HTMLElement>("button");
    first?.focus();
    const trap = (e: KeyboardEvent) => {
      if (e.key !== "Tab" || !dialog) return;
      const controls = Array.from(
        dialog.querySelectorAll<HTMLElement>(
          'button:not(:disabled),input,select,textarea,[tabindex="0"]',
        ),
      );
      if (!controls.length) return;
      if (e.shiftKey && document.activeElement === controls[0]) {
        e.preventDefault();
        controls.at(-1)?.focus();
      } else if (!e.shiftKey && document.activeElement === controls.at(-1)) {
        e.preventDefault();
        controls[0]?.focus();
      }
    };
    document.addEventListener("keydown", trap);
    return () => {
      document.removeEventListener("keydown", trap);
      previous?.focus();
    };
  }, [palette, action, settings, recovery]);
  const guarded = async (work: () => Promise<void>) => {
    setBusy(true);
    setError("");
    try {
      await work();
    } catch (e) {
      setError(String(e));
    } finally {
      setBusy(false);
    }
  };
  const addTab = (operation: Operation, name: string, session?: Session) => {
    if (!operation.attachment)
      throw new Error("The helper did not provide a terminal attachment.");
    const tab = {
      id: operation.attachment.token,
      token: operation.attachment.token,
      name,
      session,
      operationId:
        operation.state === "ready" ? operation.operationId : undefined,
    };
    setTabs((v) => [...v, tab]);
    setActive(tab.id);
    if (!tabs.length) setTerminalHeight(55);
  };
  const attach = (session: Session) =>
    guarded(async () => {
      if (session.state === "app")
        throw new Error(
          "This session runs in an external app; embedded terminal attachment is unavailable.",
        );
      if (sessionStatus(session, snapshot.remoteStatus).label === "Unverified")
        throw new Error(
          "Reconnect and refresh this host before opening its terminal.",
        );
      setSelected(session);
      const existing = tabs.find(
        (t) => t.session && key(t.session) === key(session) && !t.ended,
      );
      if (existing) {
        setActive(existing.id);
        return;
      }
      const descriptor = await request<Operation>("sessions.attach", {
        ...target(session),
        cwd: session.cwd,
        host: session.host,
      });
      addTab(descriptor, title(session), session);
    });
  const chooseAction = (capability: Capability, arguments_: string[] = []) => {
    if (capability.method === "session.pop") {
      setPalette(false);
      if (selected) void attach(selected);
      else setError("Select a session to attach.");
      return;
    }
    if (capability.method === "session.push") {
      setPalette(false);
      const tab =
        tabs.find(
          (t) => selected && t.session && key(t.session) === key(selected),
        ) || current;
      if (tab) {
        setTabs((v) => v.filter((t) => t.id !== tab.id));
        if (active === tab.id)
          setActive(tabs.find((t) => t.id !== tab.id)?.id || "");
      } else setError("Attach a session before detaching its terminal.");
      return;
    }
    setAction(capability);
    setArgs(
      arguments_.map((a) => (/\s/.test(a) ? JSON.stringify(a) : a)).join(" "),
    );
    setPrepared(undefined);
    setResult(undefined);
    const creating =
      capability.coreMethod === "sessions.open" ||
      capability.method === "session.open";
    setCwd(
      (!creating ? selected?.cwd : undefined) ||
        snapshot.repositories.find((r) => r.alias === scope)?.path ||
        "",
    );
    setCoreValues({
      ...target(creating ? undefined : selected),
      sid: String(!creating ? selected?.sid || "" : ""),
      repo:
        (!creating ? selected?.repo : undefined) ||
        (scope === "all" ? snapshot.repositories[0]?.alias : scope) ||
        "",
      new: "true",
      agent: "",
      targets: "",
      host:
        (creating ? (host === "all" ? "local" : host) : selected?.host) ||
        "local",
    });
    setPalette(false);
  };
  const capability = (noun: string, verb: string) =>
    snapshot.capabilities.find(
      (c) => c.command?.[0] === noun && c.command?.[1] === verb,
    );
  const interactiveReview = () => {
    if (!action) return;
    let words: string[] = [];
    if (action.coreMethod?.startsWith("issues."))
      words = [
        ...(coreValues.repo ? [coreValues.repo] : []),
        ...(coreValues.targets || "").split(/[\s,]+/).filter(Boolean),
      ];
    else if (action.coreMethod === "sessions.search")
      words = (coreValues.terms || "").split(/\s+/).filter(Boolean);
    else if (action.coreMethod?.startsWith("sessions.")) {
      words = [
        ...(coreValues.repo ? [coreValues.repo] : []),
        ...(coreValues.new === "true" && action.coreMethod === "sessions.open"
          ? ["new"]
          : coreValues.slot
            ? [coreValues.slot]
            : []),
        ...[],
        ...(coreValues.agent ? ["--" + coreValues.agent] : []),
        ...(coreValues.host && coreValues.host !== "local"
          ? ["--host", coreValues.host]
          : []),
      ];
    }
    if (action.coreMethod === "sessions.resume" && coreValues.sid)
      words = [
        ...(coreValues.repo ? [coreValues.repo] : ["--all"]),
        ...(coreValues.slot ? [coreValues.slot] : []),
        ...(coreValues.host && coreValues.host !== "local"
          ? ["--host", coreValues.host]
          : []),
      ];
    setAction({ ...action, coreMethod: undefined });
    setArgs(words.map((a) => (/\s/.test(a) ? JSON.stringify(a) : a)).join(" "));
    setPrepared(undefined);
    setResult(undefined);
  };
  const prepare = () =>
    guarded(async () => {
      if (!action) return;
      const read = [
        "sessions.list",
        "sessions.search",
        "issues.list",
        "issues.status",
        "repositories.list",
        "hosts.list",
        "config.get",
        "mcp.call",
        "mcp.capabilities",
      ].includes(action.coreMethod || "");
      if (read) {
        setResult(
          await request(
            action.coreMethod!,
            coreParams(action.coreMethod!, coreValues),
          ),
        );
        return;
      }
      const op = action.coreMethod
        ? await request<Operation>(
            action.coreMethod,
            coreParams(action.coreMethod, coreValues),
          )
        : await request<Operation>("operations.prepare", {
            command: [...action.command, ...parseArgs(args)],
            ...(cwd ? { cwd } : {}),
          });
      setPrepared(op);
      setPending((v) => [...new Set([...v, op.operationId])]);
    });
  const apply = () =>
    guarded(async () => {
      if (!prepared) return;
      const submitted = prepared;
      setPrepared({ ...submitted, state: "applying" });
      let applied: Operation;
      try {
        applied = await request<Operation>("operations.apply", {
          operationId: submitted.operationId,
        });
        setPrepared(applied);
      } catch (error) {
        setPrepared({ ...submitted, state: "unknown" });
        throw error;
      }
      if (applied.attachment) {
        addTab(
          applied,
          action?.label || action?.command.join(" ") || "Command",
        );
        setAction(undefined);
      } else {
        const attachments = (
          applied.result as {
            attachments?: { token: string; kind: "terminal" }[];
          }
        )?.attachments;
        if (attachments?.length) {
          for (const attachment of attachments)
            addTab({ ...applied, attachment }, "Issue session");
          setAction(undefined);
        } else {
          setResult(applied.result ?? applied);
          setPrepared(undefined);
        }
      }
      const errors = (
        applied.result as { errors?: { target: string; message: string }[] }
      )?.errors;
      if (errors?.length)
        setError(errors.map((e) => `${e.target}: ${e.message}`).join(" · "));
      if (["completed", "partial", "rejected"].includes(applied.state))
        setPending((v) => v.filter((id) => id !== applied.operationId));
      await refresh();
    });
  const filtered = snapshot.sessions.filter(
    (s) =>
      (scope === "all" ||
        s.repo === scope ||
        s.cwd?.startsWith(
          snapshot.repositories.find((r) => r.alias === scope)?.path || "\0",
        )) &&
      (host === "all" || (s.host || "local") === host) &&
      `${title(s)} ${s.repo} ${s.branch} ${s.agent} ${s.host || "local"}`
        .toLowerCase()
        .includes(query.trim().toLowerCase()),
  );
  const current = tabs.find((t) => t.id === active);
  const details = selected || current?.session;
  const detailsUnavailable =
    !!details &&
    sessionStatus(details, snapshot.remoteStatus).label === "Unverified";
  const detailsExternal = details?.state === "app";
  return (
    <div className="workspace">
      <aside className="sidebar">
        <div className="brand" data-tauri-drag-region>
          <span className="brand-mark">
            t<span>.</span>
          </span>
          <span>Sessions</span>
          <span className="version">Beta</span>
        </div>
        <button className="palette-trigger" onClick={() => setPalette(true)}>
          <Search size={15} />
          Find or run a command <kbd>⌘ K</kbd>
        </button>
        <div className="nav-heading">Workspace</div>
        <button
          className={`nav-row ${scope === "all" ? "selected" : ""}`}
          onClick={() => setScope("all")}
        >
          <Monitor size={16} />
          All sessions<span className="count">{snapshot.sessions.length}</span>
        </button>
        <div className="nav-heading">
          Repositories
          <button
            aria-label="Register repository"
            onClick={() => {
              chooseAction({
                label: "Register repository",
                method: "repositories.register",
                coreMethod: "repositories.register",
                command: ["repo", "register"],
                kind: "core",
                description:
                  "Register an existing Git checkout with the shared core.",
              });
            }}
          >
            <Plus size={14} />
          </button>
        </div>
        {snapshot.repositories.map((repo) => (
          <button
            key={repo.alias}
            className={`nav-row ${scope === repo.alias ? "selected" : ""}`}
            onClick={() => setScope(repo.alias)}
          >
            <FolderGit2 size={16} />
            <span>{repo.alias}</span>
            <span className="count">
              {snapshot.sessions.filter((s) => s.repo === repo.alias).length}
            </span>
          </button>
        ))}
        {!snapshot.repositories.length && (
          <p className="sidebar-note">
            Register a repository in configuration to keep your projects here.
          </p>
        )}
        <div className="nav-heading">
          Hosts
          <button
            aria-label="Manage hosts"
            onClick={() =>
              chooseAction({
                label: "Manage host",
                method: "hosts.change",
                coreMethod: "hosts.change",
                command: ["hosts", "change"],
                kind: "core",
              })
            }
          >
            <Plus size={14} />
          </button>
        </div>
        <button
          className={`nav-row ${host === "local" ? "selected" : ""}`}
          onClick={() => setHost(host === "local" ? "all" : "local")}
        >
          <Monitor size={16} />
          This Mac
          <span className="host-dot" />
        </button>
        {snapshot.hosts.map((h) => (
          <button
            key={h.alias}
            className={`nav-row ${host === h.alias ? "selected" : ""}`}
            onClick={() => setHost(host === h.alias ? "all" : h.alias)}
          >
            <Server size={16} />
            {h.alias}
            <span className="host-label">SSH</span>
          </button>
        ))}
        <div className="sidebar-bottom">
          <div className="connection">
            <i className={status} />
            {status === "ready"
              ? "Shared core connected"
              : status === "loading"
                ? "Connecting to shared core"
                : "Core disconnected"}
          </div>
          <button className="nav-row" onClick={() => setSettings(true)}>
            <Settings size={16} />
            Settings & diagnostics
          </button>
        </div>
      </aside>
      <main>
        <header className="toolbar">
          <div className="breadcrumb" data-tauri-drag-region>
            Workspace
            <ChevronRight size={14} />
            <strong>{scope === "all" ? "All sessions" : scope}</strong>
          </div>
          <div className="toolbar-actions">
            <button
              className="icon-button"
              aria-label="Refresh workspace"
              onClick={() => void refresh()}
            >
              <RefreshCw size={16} />
            </button>
            <button
              className="icon-button"
              aria-label="Toggle inspector"
              aria-pressed={inspector}
              onClick={() => setInspector((v) => !v)}
            >
              <PanelRight size={17} />
            </button>
            <button
              className="primary"
              disabled={!capability("session", "open")}
              onClick={() => {
                const c = capability("session", "open");
                if (c) chooseAction(c, scope === "all" ? [] : [scope]);
              }}
            >
              <Plus size={16} />
              New session
            </button>
          </div>
        </header>
        {error && (
          <div className="error-banner" role="alert">
            <AlertCircle size={17} />
            <span>{error}</span>
            {status === "error" && (
              <button
                onClick={() =>
                  void guarded(async () => {
                    await reconnect();
                    await refresh();
                  })
                }
              >
                Reconnect
              </button>
            )}
            <button aria-label="Dismiss error" onClick={() => setError("")}>
              <X size={15} />
            </button>
          </div>
        )}
        <div className="main-body">
          <section className="sessions-panel">
            <div className="section-title">
              <div>
                <h1>{scope === "all" ? "Session manager" : scope}</h1>
                <p>
                  {host === "all"
                    ? "Across your registered hosts"
                    : host === "local"
                      ? "On this Mac"
                      : `On ${host}`}{" "}
                  · Select a task to inspect; open its terminal to continue.
                </p>
              </div>
              <span className="total">
                {filtered.length}{" "}
                {filtered.length === 1 ? "session" : "sessions"}
              </span>
            </div>
            {snapshot.remoteStatus?.some((r) => r.status === "incomplete") && (
              <p className="remote-notice" role="status">
                Some hosts could not be verified:{" "}
                {snapshot.remoteStatus
                  .filter((r) => r.status === "incomplete")
                  .map((r) => r.host)
                  .join(", ")}
                . Their session state may be incomplete.
              </p>
            )}
            {status === "loading" && !snapshot.sessions.length ? (
              <div className="empty">
                <RefreshCw size={24} className="spin" />
                <h2>Reading your sessions</h2>
                <p>Checking the shared core.</p>
              </div>
            ) : (
              <SessionList
                sessions={filtered}
                snapshot={snapshot}
                selected={selected}
                attached={
                  new Set(
                    tabs
                      .filter((t) => t.session && !t.ended)
                      .map((t) => key(t.session!)),
                  )
                }
                busy={busy}
                query={query}
                onQuery={setQuery}
                onOpen={(s) => void attach(s)}
                onSelect={(s) => {
                  setSelected(s);
                  if (s.host && s.host !== "local") return;
                  void request<Record<string, unknown>>("sessions.detail", {
                    session: s.sid || `${target(s).repo}-${target(s).slot}`,
                  })
                    .then((detail) =>
                      setSelected((current) =>
                        current && key(current) === key(s)
                          ? enrichSession(current, detail)
                          : current,
                      ),
                    )
                    .catch(() => {});
                }}
              />
            )}
            {tabs.length > 0 && (
              <>
                <div
                  className="panel-divider"
                  role="separator"
                  aria-label="Resize terminal panel"
                  aria-orientation="horizontal"
                  aria-valuenow={terminalHeight}
                  tabIndex={0}
                  onKeyDown={(e) => {
                    if (e.key === "ArrowUp" || e.key === "ArrowDown") {
                      e.preventDefault();
                      setTerminalHeight((v) =>
                        Math.max(
                          25,
                          Math.min(65, v + (e.key === "ArrowUp" ? 5 : -5)),
                        ),
                      );
                    }
                  }}
                  onPointerDown={(e) => {
                    const start = e.clientY;
                    const initial = terminalHeight;
                    const height = e.currentTarget.parentElement!.clientHeight;
                    const move = (event: PointerEvent) =>
                      setTerminalHeight(
                        Math.max(
                          25,
                          Math.min(
                            65,
                            initial + ((start - event.clientY) / height) * 100,
                          ),
                        ),
                      );
                    const stop = () => {
                      window.removeEventListener("pointermove", move);
                      window.removeEventListener("pointerup", stop);
                    };
                    window.addEventListener("pointermove", move);
                    window.addEventListener("pointerup", stop);
                  }}
                />
                <div
                  className="terminal-area"
                  style={{ height: `${terminalHeight}%` }}
                >
                  <div
                    className="terminal-tabs"
                    role="tablist"
                    aria-label="Session terminals"
                  >
                    <span className="terminal-heading">
                      <TerminalSquare size={14} />
                      Terminal
                    </span>
                    {tabs.map((tab) => (
                      <div
                        key={tab.id}
                        className={`terminal-tab ${active === tab.id ? "active" : ""}`}
                      >
                        <button
                          id={`tab-${tab.id}`}
                          role="tab"
                          aria-selected={active === tab.id}
                          onClick={() => setActive(tab.id)}
                        >
                          {tab.ended && <i />}
                          {tab.name}
                        </button>
                        <button
                          aria-label={`Detach ${tab.name}`}
                          onClick={() => {
                            setTabs((v) => v.filter((t) => t.id !== tab.id));
                            if (active === tab.id)
                              setActive(
                                tabs.find((t) => t.id !== tab.id)?.id || "",
                              );
                          }}
                        >
                          <X size={13} />
                        </button>
                      </div>
                    ))}
                    {current?.ended && current.session && (
                      <button
                        className="reconnect-button"
                        onClick={() => void attach(current.session!)}
                      >
                        Reconnect
                      </button>
                    )}
                  </div>
                  <div className="terminal-content">
                    {tabs.map((tab) => (
                      <div
                        key={tab.id}
                        role="tabpanel"
                        aria-labelledby={`tab-${tab.id}`}
                        className="terminal-wrapper"
                        style={{ display: active === tab.id ? "flex" : "none" }}
                      >
                        <TerminalPane
                          token={tab.token}
                          onExit={(code) => {
                            setTabs((v) =>
                              v.map((t) =>
                                t.id === tab.id ? { ...t, ended: true } : t,
                              ),
                            );
                            if (tab.operationId)
                              void request<Operation>("operations.reconcile", {
                                operationId: tab.operationId,
                              })
                                .then((op) => {
                                  if (
                                    op.state === "completed" ||
                                    op.state === "partial"
                                  )
                                    setPending((v) =>
                                      v.filter((id) => id !== tab.operationId),
                                    );
                                })
                                .catch((e) => setError(String(e)));
                            void refresh();
                          }}
                        />
                      </div>
                    ))}
                    {!tabs.length && (
                      <div className="terminal-empty">
                        <TerminalSquare size={25} />
                        <div>
                          <strong>A terminal, right where you work</strong>
                          <p>
                            Select a session and attach to continue. Closing a
                            tab only detaches this window.
                          </p>
                        </div>
                        {selected && (
                          <button
                            className="terminal-attach"
                            disabled={busy}
                            onClick={() => void attach(selected)}
                          >
                            <Play size={14} />
                            Attach session
                          </button>
                        )}
                      </div>
                    )}
                  </div>
                </div>
              </>
            )}
          </section>
          {inspector && details && (
            <aside className="inspector">
              <div className="inspector-title">
                Session details
                <span>{details ? "Selected" : "No selection"}</span>
              </div>
              {details ? (
                <>
                  <div className="detail-task">
                    <span className="agent-badge">
                      {details.agent || "agent"}
                    </span>
                    <h2>{title(details)}</h2>
                    <span className="state">
                      <i />
                      {sessionStatus(details, snapshot.remoteStatus).label}
                    </span>
                  </div>
                  <dl>
                    {[
                      ["Repository", details.repo],
                      ["Runtime state", details.state],
                      ["Context", details.context],
                      ["Branch", details.branch],
                      ["Worktree", details.cwd],
                      ["Slot", details.slot],
                      ["Host", details.host || "local"],
                      ["Agent", details.agent],
                      ["Model", details.model],
                      ["Issue", details.issue],
                      ["Pull request", details.pr],
                    ].map(([label, value]) => (
                      <React.Fragment key={String(label)}>
                        <dt>{String(label)}</dt>
                        <dd>{String(value || "—")}</dd>
                      </React.Fragment>
                    ))}
                  </dl>
                  <button
                    className="primary full"
                    disabled={busy || detailsUnavailable || detailsExternal}
                    onClick={() => void attach(details)}
                  >
                    <TerminalSquare size={15} />
                    Attach terminal
                  </button>
                  {detailsExternal && (
                    <p className="attachment-note">
                      This session runs in an external app. Embedded terminal
                      attachment is unavailable.
                    </p>
                  )}
                  <div className="detail-actions">
                    {[
                      "resume",
                      "restart",
                      "close",
                      "move",
                      "search",
                      "paste",
                      "view-plan",
                    ].map((verb) => {
                      const c = capability("session", verb);
                      return c ? (
                        <button
                          key={verb}
                          disabled={detailsUnavailable || busy}
                          onClick={() =>
                            chooseAction(c, [
                              ...(details.repo ? [details.repo] : []),
                              String(details.slot),
                            ])
                          }
                        >
                          {verb[0].toUpperCase() + verb.slice(1)}
                          <ChevronRight size={13} />
                        </button>
                      ) : null;
                    })}
                  </div>
                  <p className="attachment-note">
                    {tabs.some(
                      (t) =>
                        t.session &&
                        key(t.session) === key(details) &&
                        !t.ended,
                    )
                      ? "Terminal attached in this window."
                      : "Terminal detached. Opening a terminal does not create a new session."}
                  </p>
                  <p className="safety-note">
                    Session ownership and worktree safety are checked by the
                    shared core before every action.
                  </p>
                </>
              ) : (
                <div className="inspector-empty">
                  <PanelRight size={24} />
                  <h3>The context you need</h3>
                  <p>
                    Select a session to see its branch, worktree, agent, and
                    available actions.
                  </p>
                </div>
              )}
            </aside>
          )}
        </div>
        <footer>
          <span>t workspace</span>
          {pending.length > 0 && (
            <button onClick={() => setRecovery(true)}>
              {pending.length}{" "}
              {pending.length === 1 ? "operation" : "operations"} to reconcile
            </button>
          )}
          <span>Shared sessions · Local + SSH</span>
          <span>
            {remoteLoading
              ? "Checking SSH hosts…"
              : snapshot.remoteStatus?.some((r) => r.status === "incomplete")
                ? "Some hosts are unreachable"
                : status === "ready"
                  ? "Synced with CLI"
                  : "Waiting for core"}
          </span>
        </footer>
      </main>
      {recovery && (
        <div className="overlay">
          <section
            className="modal"
            role="dialog"
            aria-modal="true"
            aria-label="Operation recovery"
          >
            <div className="modal-title">
              <h2>Operation recovery</h2>
              <button
                className="icon-button"
                aria-label="Close recovery"
                onClick={() => setRecovery(false)}
              >
                <X size={18} />
              </button>
            </div>
            <p>
              Check the current outcome of interrupted operations.
              Reconciliation never executes a command again.
            </p>
            {pending.map((id) => (
              <div className="recovery-row" key={id}>
                <code>{id}</code>
                <button
                  className="secondary"
                  onClick={() =>
                    void guarded(async () => {
                      const state = await request<Operation>(
                        "operations.reconcile",
                        { operationId: id },
                      );
                      setResult(state);
                      if (
                        [
                          "completed",
                          "partial",
                          "prepared",
                          "rejected",
                        ].includes(state.state)
                      )
                        setPending((v) => v.filter((item) => item !== id));
                    })
                  }
                >
                  Inspect outcome
                </button>
                <button
                  className="secondary"
                  onClick={() =>
                    setPending((v) => v.filter((item) => item !== id))
                  }
                >
                  Dismiss
                </button>
              </div>
            ))}
            {result !== undefined && (
              <div className="result">
                <ResultView
                  value={result}
                  onResume={(row) => {
                    const c = capability("session", "resume");
                    if (c) {
                      const sid = String(row.sid || row.id || "");
                      const resolved = target(row as Session);
                      if (
                        !row.repo ||
                        !resolved.slot ||
                        !/^[0-9]+$/.test(resolved.slot)
                      ) {
                        chooseAction(
                          {
                            ...c,
                            coreMethod: undefined,
                            description: `This conversation (${sid}) has no resolvable numbered slot. Review saved conversations in the terminal picker and select this exact conversation before resuming.`,
                          },
                          ["--all", "--days", "all"],
                        );
                      } else {
                        chooseAction(c);
                        setCoreValues({
                          ...resolved,
                          sid,
                          host: String(row.host || "local"),
                        });
                      }
                    }
                  }}
                  onIssuesStart={(targets) => {
                    const c = capability("issue", "start");
                    if (c) {
                      chooseAction(c);
                      setCoreValues({ targets: targets.join("\n") });
                    }
                  }}
                />
              </div>
            )}
          </section>
        </div>
      )}
      {palette && (
        <div className="overlay" onMouseDown={() => setPalette(false)}>
          <section
            className="modal palette"
            onKeyDown={(e) => {
              const buttons = Array.from(
                e.currentTarget.querySelectorAll<HTMLButtonElement>(
                  ".palette-results button",
                ),
              );
              if (e.key === "ArrowDown" || e.key === "ArrowUp") {
                e.preventDefault();
                const index = buttons.indexOf(
                  document.activeElement as HTMLButtonElement,
                );
                buttons[
                  (index + (e.key === "ArrowDown" ? 1 : -1) + buttons.length) %
                    buttons.length
                ]?.focus();
              }
              if (
                e.key === "Enter" &&
                document.activeElement?.tagName === "INPUT"
              ) {
                e.preventDefault();
                buttons[0]?.click();
              }
            }}
            role="dialog"
            aria-modal="true"
            aria-label="Command palette"
            onMouseDown={(e) => e.stopPropagation()}
          >
            <div className="palette-search">
              <Search size={20} />
              <input
                autoFocus
                placeholder="What would you like to do?"
                value={paletteQuery}
                onChange={(e) => setPaletteQuery(e.target.value)}
              />
              <kbd>ESC</kbd>
            </div>
            <div className="palette-results">
              {snapshot.capabilities
                .filter((c) =>
                  `${c.label || ""} ${c.command?.join(" ")} ${c.description || c.summary || ""}`
                    .toLowerCase()
                    .includes(paletteQuery.toLowerCase()),
                )
                .map((c) => (
                  <button key={c.method} onClick={() => chooseAction(c)}>
                    <Command size={17} />
                    <div>
                      <strong>{c.label || `t ${c.command?.join(" ")}`}</strong>
                      <span>{c.description || c.summary || c.kind}</span>
                    </div>
                    <ChevronRight size={15} />
                  </button>
                ))}
              {!snapshot.capabilities.length && (
                <p>
                  The helper has not advertised commands yet. Reconnect in
                  settings.
                </p>
              )}
            </div>
            <div className="modal-footnote">
              Commands come from your installed t core. Interactive commands
              open in a terminal.
            </div>
          </section>
        </div>
      )}
      {action && (
        <div className="overlay">
          <section
            className="modal action-dialog"
            role="dialog"
            aria-modal="true"
            aria-label={action.label || `Run t ${action.command.join(" ")}`}
          >
            <div className="modal-title">
              <div>
                <p className="eyebrow">WORKSPACE COMMAND</p>
                <h2>{action.label || `t ${action.command.join(" ")}`}</h2>
              </div>
              <button
                className="icon-button"
                disabled={busy}
                aria-label="Close command"
                onClick={dismissAction}
              >
                <X size={18} />
              </button>
            </div>
            <p>
              {action.description ||
                action.summary ||
                "Run this command through the shared t core."}
            </p>
            <fieldset
              disabled={busy || (!!prepared && prepared.state !== "prepared")}
              className="command-fields"
            >
              {action.coreMethod ? (
                <CoreForm
                  method={action.coreMethod}
                  snapshot={snapshot}
                  values={coreValues}
                  setValues={(v) => {
                    setCoreValues(v);
                    setPrepared(undefined);
                  }}
                />
              ) : (
                <>
                  <label>
                    Working directory
                    <select
                      value={cwd}
                      onChange={(e) => {
                        setCwd(e.target.value);
                        setPrepared(undefined);
                      }}
                    >
                      <option value="">Default workspace context</option>
                      {snapshot.repositories.map((r) => (
                        <option key={r.path} value={r.path}>
                          {r.alias} — {r.path}
                        </option>
                      ))}
                      {cwd &&
                        !snapshot.repositories.some((r) => r.path === cwd) && (
                          <option value={cwd}>{cwd}</option>
                        )}
                    </select>
                  </label>
                  <label>
                    Command arguments{" "}
                    <span>Individual arguments, with quotes for spaces</span>
                    <textarea
                      spellCheck={false}
                      value={args}
                      onChange={(e) => {
                        setArgs(e.target.value);
                        setPrepared(undefined);
                      }}
                      placeholder={"repository --codex"}
                    />
                  </label>
                  <p className="form-note">
                    Interactive command route. Closing its tab interrupts this
                    command; sessions already running in tmux remain
                    independent.
                  </p>
                </>
              )}
            </fieldset>
            {busy && (
              <p className="form-note" role="status">
                {progress || "Working through shared core…"}
              </p>
            )}
            {error && (
              <p className="dialog-error" role="alert">
                {error}
              </p>
            )}
            {prepared && (
              <div className="review">
                <Check size={17} />
                <div>
                  <strong>Review before running</strong>
                  <p>
                    {prepared.command
                      ? prepared.summary
                      : action.label || "Apply the reviewed changes"}
                  </p>
                  {prepared.command ? (
                    <code>t {prepared.command.join(" ")}</code>
                  ) : (
                    <ResultView value={prepared.arguments} />
                  )}
                  <p>
                    This operation will run once. If interrupted, its outcome
                    must be checked before retrying.
                  </p>
                </div>
              </div>
            )}
            {result !== undefined && (
              <div className="result">
                <ResultView
                  value={result}
                  onResume={(row) => {
                    const c = capability("session", "resume");
                    if (c) {
                      const sid = String(row.sid || row.id || "");
                      const resolved = target(row as Session);
                      if (
                        !row.repo ||
                        !resolved.slot ||
                        !/^[0-9]+$/.test(resolved.slot)
                      ) {
                        chooseAction(
                          {
                            ...c,
                            coreMethod: undefined,
                            description: `This conversation (${sid}) has no resolvable numbered slot. Review saved conversations in the terminal picker and select this exact conversation before resuming.`,
                          },
                          ["--all", "--days", "all"],
                        );
                      } else {
                        chooseAction(c);
                        setCoreValues({
                          ...resolved,
                          sid,
                          host: String(row.host || "local"),
                        });
                      }
                    }
                  }}
                  onIssuesStart={(targets) => {
                    const c = capability("issue", "start");
                    if (c) {
                      chooseAction(c);
                      setCoreValues({ targets: targets.join("\n") });
                    }
                  }}
                />
              </div>
            )}
            <div className="modal-actions">
              {action.coreMethod &&
                !action.label &&
                !busy &&
                (!prepared || prepared.state === "prepared") && (
                  <button className="secondary" onClick={interactiveReview}>
                    Review in terminal
                  </button>
                )}
              <button
                className="secondary"
                disabled={busy}
                onClick={dismissAction}
              >
                Cancel
              </button>
              {prepared && prepared.state !== "prepared" && !busy ? (
                <button
                  className="primary"
                  onClick={() => {
                    setAction(undefined);
                    setRecovery(true);
                  }}
                >
                  Inspect outcome
                </button>
              ) : prepared ? (
                <button
                  className="primary"
                  disabled={busy || prepared.state !== "prepared"}
                  onClick={() => void apply()}
                >
                  {busy ? "Starting…" : "Run command"}
                  <Play size={14} />
                </button>
              ) : (
                <button
                  className="primary"
                  disabled={busy}
                  onClick={() => void prepare()}
                >
                  {busy
                    ? "Preparing…"
                    : action.coreMethod &&
                        [
                          "sessions.list",
                          "sessions.search",
                          "issues.list",
                          "issues.status",
                          "repositories.list",
                          "hosts.list",
                          "config.get",
                          "mcp.call",
                          "mcp.capabilities",
                        ].includes(action.coreMethod)
                      ? "Read current state"
                      : "Review command"}
                  <ChevronRight size={14} />
                </button>
              )}
            </div>
          </section>
        </div>
      )}
      {settings && (
        <div className="overlay">
          <section
            className="modal settings-dialog"
            role="dialog"
            aria-modal="true"
            aria-label="Settings and diagnostics"
          >
            <div className="modal-title">
              <h2>Settings & diagnostics</h2>
              <button
                className="icon-button"
                aria-label="Close settings"
                onClick={() => setSettings(false)}
              >
                <X size={18} />
              </button>
            </div>
            <label>
              Appearance
              <select value={theme} onChange={(e) => setTheme(e.target.value)}>
                <option value="system">Follow system</option>
                <option value="light">Light</option>
                <option value="dark">Dark</option>
              </select>
            </label>
            <h3>Shared core</h3>
            <button
              className="secondary"
              onClick={() => {
                setSettings(false);
                chooseAction({
                  label: "Edit settings",
                  method: "config.set",
                  coreMethod: "config.set",
                  command: ["config", "set"],
                  kind: "core",
                });
              }}
            >
              Edit shared settings
            </button>
            <p>
              Configuration is shared with the CLI. Use configuration commands
              to update repositories, hosts, profiles, and policy.
            </p>
            <div className="settings-actions">
              {snapshot.capabilities
                .filter((c) =>
                  [
                    "config",
                    "profile",
                    "policy",
                    "system",
                    "agent",
                    "mcp",
                  ].includes(c.command?.[0]),
                )
                .map((c) => (
                  <button
                    className="secondary"
                    key={c.method}
                    onClick={() => {
                      setSettings(false);
                      chooseAction(c);
                    }}
                  >
                    {c.label || c.command.join(" ")}
                  </button>
                ))}
            </div>
            <h3>Runtime & dependencies</h3>
            <p>
              The app bundles its Python core. Git, tmux, zsh, SSH, and agents
              are checked by the core.
            </p>
            <button
              className="secondary"
              onClick={() =>
                void guarded(async () =>
                  setResult(await request("system.diagnostics")),
                )
              }
            >
              Run dependency diagnostics
            </button>
            {result !== undefined && (
              <div className="result">
                <ResultView
                  value={result}
                  onResume={(row) => {
                    const c = capability("session", "resume");
                    if (c) {
                      const sid = String(row.sid || row.id || "");
                      const resolved = target(row as Session);
                      if (
                        !row.repo ||
                        !resolved.slot ||
                        !/^[0-9]+$/.test(resolved.slot)
                      ) {
                        chooseAction(
                          {
                            ...c,
                            coreMethod: undefined,
                            description: `This conversation (${sid}) has no resolvable numbered slot. Review saved conversations in the terminal picker and select this exact conversation before resuming.`,
                          },
                          ["--all", "--days", "all"],
                        );
                      } else {
                        chooseAction(c);
                        setCoreValues({
                          ...resolved,
                          sid,
                          host: String(row.host || "local"),
                        });
                      }
                    }
                  }}
                  onIssuesStart={(targets) => {
                    const c = capability("issue", "start");
                    if (c) {
                      chooseAction(c);
                      setCoreValues({ targets: targets.join("\n") });
                    }
                  }}
                />
              </div>
            )}
            <button
              className="secondary"
              onClick={() =>
                void guarded(async () => {
                  await reconnect();
                  await refresh();
                })
              }
            >
              <RefreshCw size={14} />
              Reconnect helper
            </button>
            <p className="modal-footnote">
              Reconnect never repeats an interrupted command. Existing terminal
              attachments remain independent.
            </p>
          </section>
        </div>
      )}
    </div>
  );
}
createRoot(document.getElementById("root")!).render(<App />);
