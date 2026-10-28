import { useState } from "react";
import { Search } from "lucide-react";
import { target } from "./CoreForm";
import { key, title, type Session, type Snapshot } from "./protocol";
import { sessionStatus, type SessionFilter } from "./sessionView";

type Props = {
  sessions: Session[];
  snapshot: Snapshot;
  selected?: Session;
  attached: Set<string>;
  busy: boolean;
  query: string;
  onQuery: (value: string) => void;
  onSelect: (session: Session) => void;
  onOpen: (session: Session) => void;
};
export function SessionList({
  sessions,
  snapshot,
  selected,
  attached,
  busy,
  query,
  onQuery,
  onSelect,
  onOpen,
}: Props) {
  const [filter, setFilter] = useState<SessionFilter>("all");
  const [grouped, setGrouped] = useState(false);
  const [sort, setSort] = useState("task");
  const choices: [SessionFilter, string][] = [
    ["all", "All sessions"],
    ["attached", "Tmux attached"],
    ["detached", "Tmux detached"],
    ["app", "External app"],
    ["attention", "Needs attention"],
    ["working", "Working"],
    ["idle", "Idle"],
  ];
  const filters = choices.filter(
    ([value]) =>
      value === "all" ||
      value === filter ||
      sessions.some(
        (s) => sessionStatus(s, snapshot.remoteStatus).group === value,
      ),
  );
  const status = (s: Session) => sessionStatus(s, snapshot.remoteStatus);
  const rows = sessions
    .filter((s) => filter === "all" || status(s).group === filter)
    .sort((a, b) => {
      if (grouped && a.repo !== b.repo)
        return String(a.repo || "").localeCompare(String(b.repo || ""));
      return (
        (sort === "status"
          ? status(a).label.localeCompare(status(b).label)
          : sort === "host"
            ? String(a.host || "local").localeCompare(String(b.host || "local"))
            : title(a).localeCompare(title(b))) || key(a).localeCompare(key(b))
      );
    });
  let previousRepo: string | undefined;
  return (
    <>
      <div className="session-tools">
        <div className="session-search">
          <Search size={16} />
          <input
            aria-label="Filter sessions"
            placeholder="Search task, repository, branch, or host…"
            value={query}
            onChange={(e) => onQuery(e.target.value)}
          />
        </div>
        <div className="session-view-options">
          <button
            className="secondary"
            aria-pressed={grouped}
            onClick={() => setGrouped(!grouped)}
          >
            By repository
          </button>
          <label>
            Sort{" "}
            <select
              aria-label="Sort sessions"
              value={sort}
              onChange={(e) => setSort(e.target.value)}
            >
              <option value="task">Task</option>
              <option value="status">Status</option>
              <option value="host">Host</option>
            </select>
          </label>
        </div>
      </div>
      <div
        className="session-filters"
        role="group"
        aria-label="Session status filters"
      >
        {filters.map(([value, label]) => (
          <button
            key={value}
            className={value === "attention" ? "attention-filter" : ""}
            aria-pressed={filter === value}
            onClick={() => setFilter(value)}
          >
            {label}
            <span>
              {value === "all"
                ? sessions.length
                : sessions.filter((s) => status(s).group === value).length}
            </span>
          </button>
        ))}
      </div>
      <p className="activity-note">
        Session state shows connection status. Agent activity appears only when
        reported.
      </p>
      <div
        className="session-list"
        tabIndex={0}
        role="region"
        aria-label="Session list; scroll horizontally for all columns"
      >
        {rows.length ? (
          <table className="session-register" aria-label="Sessions">
            <thead>
              <tr>
                <th>Task / session</th>
                <th>Session state</th>
                <th>Agent</th>
                <th>Host</th>
                <th>This window</th>
                <th>
                  <span className="sr-only">Open terminal</span>
                </th>
              </tr>
            </thead>
            <tbody>
              {rows.map((s) => {
                const repo = String(s.repo || "Unregistered"),
                  heading = grouped && repo !== previousRepo;
                previousRepo = repo;
                const state = status(s),
                  isAttached = attached.has(key(s)),
                  unavailable = state.label === "Unverified";
                return (
                  <FragmentRow
                    key={key(s)}
                    heading={heading ? repo : undefined}
                    session={s}
                    selected={!!selected && key(selected) === key(s)}
                    status={state}
                    attached={isAttached}
                    disabled={busy || unavailable || s.state === "app"}
                    onSelect={() => onSelect(s)}
                    onOpen={() => onOpen(s)}
                  />
                );
              })}
            </tbody>
          </table>
        ) : (
          <div className="empty">
            <h2>No matching sessions</h2>
            <p>
              {filter === "attention"
                ? "No sessions report that they need attention. Unreported activity stays unknown."
                : "Try another search or status filter. Repository and host choices also limit this view."}
            </p>
            <button
              className="secondary"
              onClick={() => {
                setFilter("all");
                onQuery("");
              }}
            >
              Clear search and status
            </button>
          </div>
        )}
      </div>
    </>
  );
}
function FragmentRow({
  heading,
  session: s,
  selected,
  status,
  attached,
  disabled,
  onSelect,
  onOpen,
}: {
  heading?: string;
  session: Session;
  selected: boolean;
  status: ReturnType<typeof sessionStatus>;
  attached: boolean;
  disabled: boolean;
  onSelect: () => void;
  onOpen: () => void;
}) {
  return (
    <>
      {heading && (
        <tr className="repo-group">
          <th colSpan={6} scope="rowgroup">
            {heading}
          </th>
        </tr>
      )}
      <tr className={selected ? "session-row selected" : "session-row"}>
        <td className="session-task">
          <button
            className="session-title"
            aria-pressed={selected}
            onClick={onSelect}
            onDoubleClick={() => !disabled && onOpen()}
          >
            {title(s)}
          </button>
          <div className="session-context">
            <span>
              {s.repo || "Unregistered"} / {target(s).slot || s.slot}
            </span>
            {s.branch && (
              <span className="session-branch" title={s.branch}>
                {s.branch}
              </span>
            )}
          </div>
        </td>
        <td>
          <span className={`session-status ${status.tone}`}>
            <i />
            {status.label}
          </span>
        </td>
        <td>{s.agent || "Unknown"}</td>
        <td>{!s.host || s.host === "local" ? "This Mac" : s.host}</td>
        <td>
          <span className="terminal-state">
            {attached ? "Attached" : "Detached"}
          </span>
        </td>
        <td>
          <button
            className="row-open"
            disabled={disabled}
            title={
              s.state === "app"
                ? "This session is in an external app; terminal attachment is unavailable."
                : undefined
            }
            aria-label={`Open terminal for ${title(s)}`}
            onClick={onOpen}
          >
            {attached ? "Focus" : "Open"}
          </button>
        </td>
      </tr>
    </>
  );
}
