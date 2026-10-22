import type { Snapshot, Session } from "./protocol";
export function target(s?: Session) {
  const combined = String(s?.slot || "");
  return {
    repo: s?.repo || combined.replace(/-\d+$/, ""),
    slot: combined.match(/(\d+)$/)?.[1] || combined,
  };
}
export function CoreForm({
  method,
  snapshot,
  values,
  setValues,
}: {
  method: string;
  snapshot: Snapshot;
  values: Record<string, string>;
  setValues: (v: Record<string, string>) => void;
}) {
  const field = (name: string, label: string, placeholder = "") => (
    <label key={name}>
      {label}
      <input
        value={values[name] || ""}
        placeholder={placeholder}
        onChange={(e) => setValues({ ...values, [name]: e.target.value })}
      />
    </label>
  );
  const repository = (
    <label>
      Repository
      <select
        value={values.repo || ""}
        onChange={(e) => setValues({ ...values, repo: e.target.value })}
      >
        <option value="">Select a repository</option>
        {snapshot.repositories.map((r) => (
          <option key={r.alias} value={r.alias}>
            {r.alias}
          </option>
        ))}
      </select>
    </label>
  );
  if (method === "mcp.call")
    return (
      <>
        <label>
          MCP tool
          <select
            value={values.tool || "find_sessions"}
            onChange={(e) => setValues({ ...values, tool: e.target.value })}
          >
            <option value="find_sessions">Find sessions</option>
            <option value="list_sessions">List sessions</option>
            <option value="session_detail">Session details</option>
          </select>
        </label>
        {values.tool === "session_detail"
          ? field(
              "session",
              "Session reference",
              "project-16 or exact conversation ID",
            )
          : field("query", "Search query (find only)", "task words")}
        {field("repo", "Repository (optional)", "project")}
      </>
    );
  if (method === "sessions.search")
    return (
      <>
        {field("terms", "Search session history", "task words or exact phrase")}
        {field("repo", "Repository (optional)", "project")}
      </>
    );
  if (method === "sessions.list")
    return (
      <p className="form-note">Read sessions from all configured hosts.</p>
    );
  if (method.startsWith("sessions."))
    return (
      <>
        {repository}
        <label>
          Location
          <select
            value={values.host || "local"}
            onChange={(e) => setValues({ ...values, host: e.target.value })}
          >
            <option value="local">This Mac</option>
            {snapshot.hosts.map((h) => (
              <option key={h.alias} value={h.alias}>
                {h.alias} — {h.target}
              </option>
            ))}
          </select>
        </label>
        {method === "sessions.open" && (
          <>
            <label>
              Session
              <select
                value={values.new || "true"}
                onChange={(e) => setValues({ ...values, new: e.target.value })}
              >
                <option value="true">Create a new worktree</option>
                <option value="false">Use an existing slot</option>
              </select>
            </label>
            <label>
              Agent
              <select
                value={values.agent || ""}
                onChange={(e) =>
                  setValues({ ...values, agent: e.target.value })
                }
              >
                <option value="">
                  Configured default (
                  {String(
                    snapshot.config[`DEV_AGENT[${values.repo}]`] ||
                      snapshot.config.DEV_AGENT_DEFAULT ||
                      "claude",
                  )}
                  )
                </option>
                <option value="codex">Codex</option>
                <option value="claude">Claude</option>
              </select>
            </label>
            {field(
              "model",
              "Model (optional override)",
              "Use configured default",
            )}
            {field(
              "effort",
              "Reasoning effort (optional override)",
              "Use configured default",
            )}
          </>
        )}
        {(method !== "sessions.open" || values.new === "false") &&
          field("slot", "Slot number", "16")}
        {method === "sessions.resume" &&
          field(
            "sid",
            "Conversation ID (optional)",
            "Resume newest if left empty",
          )}
        <p className="form-note">
          Lifecycle operations use the same ownership and worktree guards as the
          CLI. Remote hosts require t installed and configured over SSH.
        </p>
      </>
    );
  if (method === "repositories.register")
    return (
      <>
        {field("alias", "Repository alias", "project")}
        {field("path", "Repository folder", "/Users/you/code/project")}
      </>
    );
  if (method === "hosts.change")
    return (
      <>
        <label>
          Host action
          <select
            value={values.action || "add"}
            onChange={(e) => setValues({ ...values, action: e.target.value })}
          >
            <option value="add">Add host</option>
            <option value="remove">Remove host</option>
            <option value="default">Set default host</option>
          </select>
        </label>
        {field("alias", "Host alias", "build")}
        {(values.action || "add") === "add" &&
          field("target", "SSH target", "user@example.com")}
        <label className="checkbox">
          <input
            type="checkbox"
            checked={values.default === "true"}
            onChange={(e) =>
              setValues({ ...values, default: String(e.target.checked) })
            }
          />
          Use as default host
        </label>
      </>
    );
  if (method.startsWith("issues."))
    return (
      <>
        {method === "issues.list" ? (
          field("repo", "Repository alias or owner/repository", "project")
        ) : (
          <>
            {field(
              "repo",
              "Repository alias or owner/repository (optional for URLs)",
              "project",
            )}
            <label>
              Issue numbers or URLs
              <textarea
                placeholder="12, 34, or https://github.com/owner/repo/issues/56"
                value={values.targets || ""}
                onChange={(e) =>
                  setValues({ ...values, targets: e.target.value })
                }
              />
              <span>
                Separate independent issues with spaces, commas, or new lines.
              </span>
            </label>
          </>
        )}
      </>
    );
  if (method === "config.set")
    return (
      <>
        <label>
          Default agent
          <select
            value={
              values.DEV_AGENT_DEFAULT ||
              String(snapshot.config.DEV_AGENT_DEFAULT || "claude")
            }
            onChange={(e) =>
              setValues({ ...values, DEV_AGENT_DEFAULT: e.target.value })
            }
          >
            <option value="claude">Claude</option>
            <option value="codex">Codex</option>
          </select>
        </label>
        <label>
          Default interface
          <select
            value={
              values.DEV_OPEN_MODE_DEFAULT ||
              String(snapshot.config.DEV_OPEN_MODE_DEFAULT || "cli")
            }
            onChange={(e) =>
              setValues({ ...values, DEV_OPEN_MODE_DEFAULT: e.target.value })
            }
          >
            <option value="cli">Terminal</option>
            <option value="app">External agent app</option>
          </select>
        </label>
        {field(
          "DEV_BRANCH",
          "Default branch",
          String(snapshot.config.DEV_BRANCH || "main"),
        )}
        {field(
          "DEV_WORKTREE_ROOT",
          "Worktree folder",
          String(snapshot.config.DEV_WORKTREE_ROOT || ""),
        )}
        <p className="form-note">
          Agent model and reasoning effort are available through the dedicated
          configuration commands in the palette.
        </p>
      </>
    );
  return (
    <p className="form-note">
      This reads current state directly from the shared core.
    </p>
  );
}
export function coreParams(method: string, v: Record<string, string>) {
  if (method === "mcp.call")
    return {
      name: v.tool || "find_sessions",
      arguments: { query: v.query || "", repo: v.repo, session: v.session },
    };
  if (method === "sessions.search")
    return { query: v.terms || "", ...(v.repo ? { repo: v.repo } : {}) };
  if (method === "sessions.list") return { all_hosts: true };
  if (method.startsWith("sessions."))
    return {
      repo: v.repo,
      slot: v.slot,
      ...(v.sid ? { sid: v.sid } : {}),
      ...(v.host && v.host !== "local" ? { host: v.host } : {}),
      ...(method === "sessions.open"
        ? {
            new: v.new !== "false",
            ...(v.agent ? { agent: v.agent } : {}),
            ...(v.model ? { model: v.model } : {}),
            ...(v.effort ? { effort: v.effort } : {}),
          }
        : {}),
    };
  if (method === "hosts.change")
    return { ...v, action: v.action || "add", default: v.default === "true" };
  if (method === "config.set")
    return {
      changes: Object.fromEntries(
        Object.entries(v).filter(
          ([key, value]) => key.startsWith("DEV_") && value,
        ),
      ),
    };
  if (method === "issues.list") return { repo: v.repo };
  if (method.startsWith("issues."))
    return {
      targets: [
        ...(v.repo ? [v.repo] : []),
        ...(v.targets || "").split(/[\s,]+/).filter(Boolean),
      ],
    };
  return v;
}
