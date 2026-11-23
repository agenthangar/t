"""Local recovery menus for t's terminal clients; no model or network dependency."""

import fcntl
import array
import hashlib
import json
import os
from pathlib import Path
import re
import runpy
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import textwrap
import termios
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
RUNTIME_PROTOCOL = 1
UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
BACKGROUND = "Cannot use the background server"
DEAD = "Agent pane has exited"
STALLED = "Agent is not reading terminal input"
REQUESTED = "Recovery requested"
STALL_SECONDS = 12
INVALID_CWD = "Failed to start turn: turn/start failed in TUI: turn/start failed: invalid cwd: No such file or directory (os error 2) (code -32600)"
# Match final CLI error lines, not generic mentions of errors or retry counters.
FAILURES = {
    "codex": re.compile(r"^(?:■\s*)?(?P<message>Server connection could not be restored|Automatic reconnect could not restore this session\.|Reconnect failed — check the endpoint, then relaunch|" + re.escape(INVALID_CWD) + r"$)"),
    "claude": re.compile(r"^(?:[⎿✻●]\s*)?API Error:\s*(?:Connection error|Unable to connect|Failed to connect|Network error|Request timed out)(?:[. :]|$)", re.I),
    "cursor": re.compile(r"^(?:[✗×●]\s*)?(?:Connection failed(?: repeatedly)?|Connection error|Unable to connect to the server|The connection failed)(?:[. :]|$)", re.I),
}


def run(argv):
    try:
        # The agent's original worktree may disappear while this monitor runs.
        return subprocess.run(argv, text=True, capture_output=True, stdin=subprocess.DEVNULL,
                              timeout=15, cwd="/")
    except (OSError, subprocess.TimeoutExpired) as error:
        return subprocess.CompletedProcess(argv, 1, "", str(error))


def tmux(socket, *args):
    return run(["tmux", "-S", socket, *args])


def cache(socket):
    root = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache")))
    directory = root / "t" / "recovery" / hashlib.sha256(socket.encode()).hexdigest()[:20]
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    return directory


def write_json(path, data):
    fd, name = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(data, file)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_json(path):
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def failure(agent, screen):
    pattern = FAILURES.get(agent)
    if not pattern:
        return ""
    # capture-pane omits ANSI by default. Ignore quoted lines and fenced examples;
    # the caller supplies the visible pane, never scrollback.
    fenced = False
    visible = []
    for line in screen.splitlines():
        if line.strip().startswith(("```", "~~~")):
            fenced = not fenced
            continue
        if fenced:
            continue
        visible.append(line.strip())
        match = pattern.match(line.strip())
        if match:
            return match.groupdict().get("message") or match.group(0)
    unavailable = any(cause in line for line in visible for cause in
                      ("background server is not running", "background server socket is stale or unreachable"))
    incompatible = ("Experimental feature request failed" in visible and
                    any("Restart cannot resolve this compatibility check." in line for line in visible) and
                    any(re.fullmatch(r"[>›❯]?\s*2\. Cancel", line) for line in visible))
    feature_mismatch = ("Background server has incompatible feature settings" in visible and
                        re.search(r"(?:^| )This session requires [a-z_]+ to be (?:enabled|disabled)(?: |$)",
                                  " ".join(visible)) and
                        any(re.fullmatch(r"[>›❯]?\s*[23]\. Cancel", line) for line in visible))
    if (agent == "codex" and (feature_mismatch or (BACKGROUND in visible and (incompatible or unavailable)))
            and any(re.fullmatch(r"[>›❯]?\s*1\. Run without daemon this time", line)
                    for line in visible)):
        return BACKGROUND
    return ""


def panes(socket):
    keys = ("session", "pane", "pane_pid", "pane_dead", "pane_dead_time", "cwd")
    # Some tmux builds rewrite literal tabs in format strings. The path is
    # last so separators in a workspace name do not change the field count.
    result = tmux(socket, "list-panes", "-a", "-F", "#{session_name}|#{pane_id}|#{pane_pid}|#{pane_dead}|#{pane_dead_time}|#{session_path}")
    groups = {}
    for line in result.stdout.splitlines():
        fields = line.split("|", 5)
        if len(fields) == len(keys) and fields[0].startswith(("dev-", "t-cursor-")):
            groups.setdefault(fields[0], []).append(dict(zip(keys, fields)))
    return [rows[0] for rows in groups.values() if len(rows) == 1]


def process(pid):
    if not str(pid).isdigit():
        return {}
    result = run(["ps", "-ww", "-p", str(pid), "-o", "ppid=,lstart=,args="])
    fields = result.stdout.strip().split(None, 6)
    if result.returncode or len(fields) != 7 or not fields[0].isdigit():
        return {}
    return {"ppid": fields[0], "start": " ".join(fields[1:6]), "args": fields[6]}


def zombie(pid):
    status = run(["ps", "-p", str(pid), "-o", "stat="])
    return status.returncode == 0 and status.stdout.strip().startswith("Z")


def cursor_process(info):
    args = info.get("args", "")
    return any(".app/Contents/" not in token and re.search(r"(?:^|/)cursor-agent(?:/|$)", token)
               for token in args.split())


def descendant(pid, parent):
    for _ in range(64):
        if str(pid) == str(parent):
            return True
        info = process(pid)
        if not info or int(info["ppid"]) <= 1:
            return False
        pid = info["ppid"]
    return False


def owner(socket, pane, startup=False):
    if pane["session"].startswith("t-cursor-"):
        # Cursor's legacy hook record is not bound to a pane incarnation. A
        # dead Cursor pane gets the attention notice until that can be proven.
        if pane.get("pane_dead") == "1":
            return {}
        result = read_json(cache(socket) / (pane["pane"].replace("%", "pane-") + ".json"))
        info = process(result.get("pid", ""))
        if (not UUID.fullmatch(result.get("sid", "")) or result.get("cwd") != pane["cwd"]
                or result.get("start") != info.get("start") or not cursor_process(info)
                or not descendant(result.get("pid"), pane["pane_pid"])):
            return {}
    else:
        # Login zsh is the shared local/remote integration contract. Resolve the
        # current thread, not the newest transcript in this worktree.
        # Warm the shared process snapshot in the parent shell. Each command
        # substitution otherwise rebuilds it in a subshell and discards it.
        snippet = (
            'setopt no_monitor no_notify; (( $+functions[_dev_ps_snapshot] )) && _dev_ps_snapshot; '
            's=%s; d=%s; print -r -- "$(_dev_agent_of_session "$s")"$\'\\t\''
            '"$(_dev_session_sid "$s" "$d")"$\'\\t\'"$(_dev_session_claude_pid "$s")"'
        ) % (shlex.quote(pane["session"]), shlex.quote(pane["cwd"]))
        result = run(["zsh", "-lic", "export TMUX=" + shlex.quote(socket + ",0,0") + "; " + snippet])
        fields = result.stdout.rstrip("\r\n").split("\t")
        if result.returncode or len(fields) != 3 or fields[0] not in ("codex", "claude"):
            return {}
        result = dict(zip(("agent", "sid", "pid"), fields))
        if pane.get("pane_dead") == "1":
            # The shared resolver validates the exact thread's transcript even
            # after its process exits. Never guess by transcript recency, and
            # never treat an unresolved live process as an exited owner.
            if (result["pid"] or not UUID.fullmatch(result["sid"])
                    or not pane.get("pane_dead_time", "").isdigit()):
                return {}
            return {**result, **pane, "socket": socket, "start": ""}
        info = process(result["pid"])
        if "app-server" in info.get("args", "").split():
            return {}  # Shared infrastructure is never a recoverable client.
        result["start"] = info.get("start", "")
        if not UUID.fullmatch(result["sid"]):
            # Startup can fail before SessionStart assigns a thread. Only a
            # demonstrably fresh Codex launch may be restarted without an ID.
            if not (startup and result["agent"] == "codex" and not result["sid"] and fresh_codex(info)):
                return {}
    if not result.get("start"):
        return {}
    return {**result, **pane, "socket": socket, **({"startup": True} if startup else {})}


def fresh_codex(info):
    # ps flattens argv; accept only t's generated fresh-launch grammar. Unknown
    # arguments fail closed, especially resume/fork/prompt/remote launches.
    try:
        words = shlex.split(info.get("args", ""))
    except ValueError:
        return False
    if not words or not re.fullmatch(r"codex(?:-(?:aarch64|x86_64)-[\w-]+)?", Path(words[0]).name):
        return False
    args = iter(words[1:])
    for arg in args:
        if arg not in ("--model", "-c", "--enable", "--disable") or next(args, None) is None:
            return False
    return True


def current(target):
    for pane in panes(target["socket"]):
        if pane["pane"] == target["pane"]:
            actual = owner(target["socket"], pane, startup=True) if target.get("startup") else owner(target["socket"], pane)
            keys = ("agent", "sid", "pid", "start", "session", "pane", "cwd",
                    "pane_pid", "pane_dead", "pane_dead_time")
            return all(actual.get(key) == target.get(key) for key in keys)
    return False


def clients(socket, pane):
    result = tmux(socket, "list-clients", "-F", "#{client_name}|#{pane_id}")
    return [fields[0] for line in result.stdout.splitlines()
            if len(fields := line.split("|")) == 2 and fields[1] == pane]


def input_state(socket, pane, viewer=None):
    """Observe tmux input routing; absent/unsupported fields stay unknown."""
    result = tmux(socket, "display-message", "-p", "-t", pane,
                  "#{pane_tty}|#{pane_input_off}|#{pane_in_mode}|#{pane_mode}")
    fields = result.stdout.rstrip("\r\n").split("|")
    if result.returncode or len(fields) != 4:
        return {}
    state = dict(zip(("tty", "input_off", "in_mode", "mode"), fields))
    result = tmux(socket, "list-clients", "-F",
                  "#{client_name}|#{pane_id}|#{client_readonly}|#{client_control_mode}|#{client_tty}|#{client_key_table}")
    if result.returncode:
        return state
    viewers = []
    for line in result.stdout.splitlines():
        row = line.split("|")
        if len(row) != 6 or row[1] != pane or (viewer is not None and row[0] != viewer):
            continue
        if row[2] == "1":
            state["readonly"] = True
        if row[2:4] == ["0", "0"] and row[4].startswith("/dev/") and row[5] == "root":
            viewers.append(row[0])
    state["viewers"] = viewers
    return state


def unread_input(tty):
    """Read only a raw PTY's pending-byte count, never its input contents."""
    if not isinstance(tty, str) or not tty.startswith("/dev/"):
        return None
    fd = None
    try:
        fd = os.open(tty, os.O_RDONLY | os.O_NONBLOCK | os.O_NOCTTY)
        if termios.tcgetattr(fd)[3] & termios.ICANON:
            return None
        count = array.array("i", [0])
        fcntl.ioctl(fd, termios.FIONREAD, count, True)
        return count[0] if count[0] >= 0 else None
    except (OSError, ValueError, AttributeError, termios.error):
        return None
    finally:
        if fd is not None:
            os.close(fd)


def pending_input(socket, pane, viewer=None):
    state = input_state(socket, pane, viewer)
    if state.get("input_off") != "0" or state.get("in_mode") != "0" or not state.get("viewers"):
        return None, None
    count = unread_input(state.get("tty"))
    return count, state["viewers"][0]


def recovery_menu(socket, pane, viewer, title, lines, actions, cleanup=None, closed=None):
    """A prominent, client-scoped panel that also fits a narrow SSH terminal."""
    size = tmux(socket, "display-message", "-p", "-c", viewer, "#{client_width}")
    columns = int(size.stdout.strip()) if size.stdout.strip().isdigit() else 80
    width = max(20, min(58, columns - 6))
    items = [("-#[nodim]" + " " * width, "", "")]
    for line in lines:
        for part in textwrap.wrap(line, width=width - 4):
            items.append(("-#[nodim]  " + part.ljust(width - 4) + "  ", "", ""))
    items.append(("-#[nodim]" + " " * width, "", ""))
    # Last action is Dismiss. An incidental Enter must not restart the agent.
    selected = len(items) + len(actions) - 1
    for label, key, command in actions:
        if cleanup is not None:
            dismissed = "run-shell -b " + shlex.quote(shlex.join(["rm", "-f", "--", str(cleanup)]))
            command = command + "; " + dismissed if command else dismissed
        items.append((textwrap.shorten(label, width=width - 6, placeholder="…"), key, command))
    menu = ["tmux", "-S", socket, "display-menu", "-c", viewer, "-t", pane,
            "-T", title, "-x", "C", "-y", "C", "-b", "double",
            "-s", "fg=colour234,bg=colour230",
            "-S", "fg=colour214,bg=colour230,bold",
            "-H", "fg=colour232,bg=colour214,bold", "-C", str(selected), "--"]
    for item in items:
        menu.extend(item)
    # tmux owns the input wait so an unanswered panel never stalls the monitor.
    command = shlex.join(menu)
    # tmux 3.8 made display-menu nonblocking. Only older versions provide a
    # close receipt here. Dismiss always clears a notice through its action;
    # on newer versions Escape leaves an unresolved notice until explicit reopen.
    version = re.match(r"tmux (\d+)\.(\d+)", tmux(socket, "-V").stdout)
    waits_for_close = version and tuple(map(int, version.groups())) < (3, 8)
    if waits_for_close and cleanup is not None:
        command += "; " + shlex.join(["rm", "-f", "--", str(cleanup)])
    if waits_for_close and closed is not None:
        command += "; " + shlex.join(["touch", "--", str(closed)])
    return tmux(socket, "run-shell", "-b", command).returncode == 0


def offer(target, error, viewer=None, force=False):
    socket = target["socket"]
    viewers = clients(socket, target["pane"])
    if not viewers or (viewer is not None and viewer not in viewers):
        return False
    viewer = viewer or viewers[0]
    token = uuid.uuid4().hex
    path = cache(socket) / (token + ".offer")
    write_json(path, {**target, "viewer": viewer, "error": error, "created": time.time(), "force": force})
    callback = shlex.join([sys.executable, str(Path(__file__).resolve()), "accept", socket, token,
                          target["pane"], viewer])
    dismiss = shlex.join([sys.executable, str(Path(__file__).resolve()), "dismiss", socket, token])
    # One menu, on a client actually viewing this pane. No keystrokes enter the
    # agent and no action runs until the user chooses Restart.
    if force:
        title, choice = "t: AGENT STILL RUNNING", "Force stop and restart"
        lines = ["This agent did not stop after the restart request.",
                 "Force stop this terminal client and restart the same conversation?",
                 "An unsaved turn may be lost.",
                 "The visible draft was saved. Other sessions stay running."]
    elif error in (STALLED, REQUESTED):
        title = "t: TERMINAL INPUT WAITING" if error == STALLED else "t: SESSION RECOVERY"
        choice = "Save draft and restart"
        lines = ["The agent may have stopped reading terminal input." if error == STALLED else
                 "Restart this conversation if its input is not responding.",
                 "Your visible draft will be saved first."]
    elif error == DEAD:
        title, choice = "t: AGENT EXITED", "Save draft and restart"
        lines = ["The agent's terminal process has exited.",
                 "Restart to continue this conversation.", "Your visible draft will be saved first."]
    elif target.get("startup"):
        title, choice = "t: BACKGROUND SERVER", "Run without daemon this time"
        lines = ["The background server cannot serve this session.",
                 "Continue with a standalone client for this session.",
                 "The shared server and other sessions stay running."]
    elif error == INVALID_CWD:
        title, choice = "t: WORKSPACE UNAVAILABLE", "Save draft and restart here"
        lines = ["The agent cannot access its workspace.",
                 "Restart this conversation in its worktree.", "Your visible draft will be saved first."]
    else:
        title, choice = "t: CONNECTION FAILED", "Save draft and restart"
        lines = ["The agent connection has stopped.",
                 "Restart to continue this conversation.", "Your visible draft will be saved first."]
    shown = recovery_menu(socket, target["pane"], viewer, title, lines, [
        (choice, "" if error == STALLED else "r", "run-shell -b " + shlex.quote(callback)),
        ("Cancel" if force else "Dismiss", "q", "run-shell -b " + shlex.quote(dismiss)),
    ], closed=path.with_suffix(".closed"))
    if not shown:
        path.unlink(missing_ok=True)
    return shown


def notice_path(socket, pane):
    return cache(socket) / (pane.replace("%", "pane-") + ".notice")


def unverified_notice(socket, pane, viewer=None):
    viewers = clients(socket, pane)
    if not viewers or (viewer is not None and viewer not in viewers):
        return False
    path = notice_path(socket, pane)
    write_json(path, {})
    shown = recovery_menu(socket, pane, viewer or viewers[0], "t: RECOVERY NEEDS ATTENTION", [
        "This pane needs recovery.",
        "t cannot verify this conversation.",
        "Copy your draft before quitting, then relaunch.",
    ], [("Dismiss", "q", "")], cleanup=path)
    if not shown:
        path.unlink(missing_ok=True)
    return shown


def pane_failure(socket, pane):
    if pane.get("pane_dead") == "1":
        return DEAD
    # Agent stamps are detection hints; owner() verifies them before any offer.
    if pane["session"].startswith("t-cursor-"):
        agent = "cursor"
    else:
        stamp = tmux(socket, "show-environment", "-t", "=" + pane["session"], "DEV_AGENT")
        agent = stamp.stdout.strip().removeprefix("DEV_AGENT=")
    screen = tmux(socket, "capture-pane", "-p", "-J", "-t", pane["pane"])
    return failure(agent, screen.stdout) if screen.returncode == 0 else ""


def recovery_key(socket):
    """Reserve a free prefix key without replacing the user's bindings."""
    command = (shlex.join([sys.executable, str(Path(__file__).resolve()), "reopen"])
               + " #{q:socket_path} #{q:pane_id} #{q:client_name}")
    bindings = tmux(socket, "list-keys", "-T", "prefix")
    if bindings.returncode:
        return ""
    occupied = set(re.findall(r"-T prefix\s+(\S+)\s", bindings.stdout))
    notes = tmux(socket, "list-keys", "-N", "-T", "prefix").stdout
    for key in ("R", "M-r", "M-R", "F12"):
        if key in occupied and not re.search(r"\s" + re.escape(key) + r"\s+t: reopen recovery$", notes, re.M):
            continue
        if tmux(socket, "bind-key", "-N", "t: reopen recovery", "-T", "prefix", key,
                "run-shell", "-b", command).returncode == 0:
            return key
    return ""


def hint_path(socket, pane):
    return cache(socket) / (pane.replace("%", "pane-") + ".status")


def status_hint(socket, pane, key):
    path = hint_path(socket, pane["pane"])
    if path.exists():
        return
    session = "=" + pane["session"] + ":"
    prefix = tmux(socket, "show-options", "-v", "-A", "-t", session, "prefix").stdout.strip()
    label = lambda value: value.replace("C-", "Ctrl-").replace("M-", "Alt-")
    hint = "Recovery: " + label(prefix or "prefix") + " " + label(key) if key else "Recovery needs attention"
    before = {}
    for option in ("status-left", "status-left-length"):
        value = tmux(socket, "show-options", "-A", "-v", "-t", session, option)
        if value.returncode:
            return
        before[option] = {"value": value.stdout.rstrip("\n"),
                          "local": bool(tmux(socket, "show-options", "-q", "-t", session, option).stdout)}
    length = before["status-left-length"]["value"]
    if not length.isdigit():
        return
    after = {"status-left": "#[reverse] " + hint + " #[default] " + before["status-left"]["value"],
             "status-left-length": str(int(length) + len(hint) + 3)}
    write_json(path, {"session": pane["session"], "pane": pane["pane"], "before": before, "after": after})
    for option, value in after.items():
        tmux(socket, "set-option", "-t", session, option, value)


def clear_hint(socket, pane):
    path = hint_path(socket, pane)
    state = read_json(path)
    if state:
        session = "=" + state["session"] + ":"
        for option, installed in state["after"].items():
            value = tmux(socket, "show-options", "-A", "-v", "-t", session, option)
            # A user edit made while the hint was visible takes precedence.
            if value.returncode == 0 and value.stdout.rstrip("\n") == installed:
                before = state["before"][option]
                args = ["-t", session, option, before["value"]] if before["local"] else ["-u", "-t", session, option]
                tmux(socket, "set-option", *args)
    path.unlink(missing_ok=True)


def clear_callback_view(socket, pane):
    """Dismiss our stale error buffer; return True if tmux cannot inspect it."""
    mode = tmux(socket, "display-message", "-p", "-t", pane, "#{pane_mode}")
    if mode.returncode or mode.stdout.strip() != "view-mode":
        return
    screen = tmux(socket, "capture-pane", "-p", "-J", "-M", "-t", pane)
    if screen.returncode:
        # Older tmux releases cannot capture mode output. Let the user close
        # it rather than guessing that an unrelated command's output is ours.
        return True
    # View-mode wraps long commands at the terminal width. These callbacks
    # emitted no output beyond tmux's quoted command and exit status.
    match = re.fullmatch(r"'(.*)' returned [1-9]\d*", screen.stdout.replace("\n", "").strip())
    if not match:
        return
    try:
        args = shlex.split(match[1])
    except ValueError:
        return
    if (len(args) not in (5, 6, 7) or Path(args[1]).name != "t_recovery.py"
            or args[3] != socket):
        return
    if args[2] in ("accept", "dismiss"):
        if (len(args) not in (5, 7) or not re.fullmatch(r"[0-9a-f]{32}", args[4])
                or (len(args) == 7 and args[5] != pane)):
            return
    elif args[2] != "reopen" or len(args) != 6 or args[4] != pane:
        return
    tmux(socket, "send-keys", "-t", pane, "-X", "cancel")


def reopen(socket, pane_id, viewer):
    pane = next((row for row in panes(socket) if row["pane"] == pane_id), None)
    if pane is None or viewer not in clients(socket, pane_id):
        return 1
    notice_path(socket, pane_id).unlink(missing_ok=True)
    unreadable_view = clear_callback_view(socket, pane_id)
    state = input_state(socket, pane_id, viewer)
    reason = ("Input to this pane is disabled in tmux." if state.get("input_off") == "1" else
              "This tmux client is read-only." if state.get("readonly") else
              "Close tmux copy or output mode with q to return to the agent."
              if state.get("in_mode") == "1" or unreadable_view else "")
    if reason:
        return 0 if recovery_menu(socket, pane_id, viewer, "t: INPUT PAUSED", [reason],
                                  [("Dismiss", "q", "")]) else 1
    error = pane_failure(socket, pane)
    if not error:
        # The shortcut is the user's report. A screen error is not required.
        error = REQUESTED
    target = owner(socket, pane, startup=True) if error == BACKGROUND else owner(socket, pane)
    shown = offer(target, error, viewer=viewer) if target else unverified_notice(socket, pane_id, viewer=viewer)
    return 0 if shown else 1


class Watcher:
    def __init__(self, socket):
        self.socket = socket
        self.seen = {}
        self.unverified = set()
        self.generations = {}
        self.key = None
        self.stalled = {}

    def input_failure(self, pane):
        pane_id = pane["pane"]
        count, viewer = pending_input(self.socket, pane_id)
        if not count:
            self.stalled.pop(pane_id, None)
            return "", None, None
        target = owner(self.socket, pane)
        if (not target or target.get("agent") != "codex" or target.get("pane_dead") == "1"
                or not descendant(target["pid"], pane["pane_pid"])):
            self.stalled.pop(pane_id, None)
            return "", None, None
        identity = tuple(target.get(key) for key in ("pid", "start", "sid", "cwd", "pane_pid"))
        previous, since = self.stalled.get(pane_id, (None, time.monotonic()))
        if previous != identity:
            since = time.monotonic()
            if previous is not None:
                self.seen.pop(pane_id, None)
        self.stalled[pane_id] = (identity, since)
        return (STALLED if time.monotonic() - since >= STALL_SECONDS else ""), target, viewer

    def poll(self):
        rows = panes(self.socket)
        active = {row["pane"] for row in rows}
        # A visible menu can wait indefinitely. Its single-use offer is checked
        # against the exact live owner on acceptance, not expired while on screen.
        # Allow async menu callbacks time to claim it after the menu closes.
        for closed in cache(self.socket).glob("*.closed"):
            if time.time() - closed.stat().st_mtime > 300:
                closed.with_suffix(".offer").unlink(missing_ok=True)
                closed.unlink(missing_ok=True)
        for path in cache(self.socket).glob("*.offer"):
            if read_json(path).get("pane") not in active:
                path.unlink(missing_ok=True)
        for path in cache(self.socket).glob("*.status"):
            pane_id = read_json(path).get("pane")
            if pane_id and pane_id not in active:
                clear_hint(self.socket, pane_id)
        self.seen = {key: value for key, value in self.seen.items() if key in active}
        self.unverified.intersection_update(active)
        self.generations = {key: value for key, value in self.generations.items() if key in active}
        self.stalled = {key: value for key, value in self.stalled.items() if key in active}
        # Make the existing prefix shortcut available even before an error.
        if rows and self.key is None:
            self.key = recovery_key(self.socket)
        for pane in rows:
            generation = (pane["pane_pid"], pane.get("pane_dead_time", ""))
            if self.generations.get(pane["pane"]) != generation:
                self.seen.pop(pane["pane"], None)
                self.unverified.discard(pane["pane"])
                self.stalled.pop(pane["pane"], None)
            self.generations[pane["pane"]] = generation
            error = pane_failure(self.socket, pane)
            input_target, input_viewer = None, None
            if not error:
                error, input_target, input_viewer = self.input_failure(pane)
            else:
                self.stalled.pop(pane["pane"], None)
            old_error, offered = self.seen.get(pane["pane"], ("", False))
            if not error:
                clear_hint(self.socket, pane["pane"])
            elif error == old_error:
                if self.key is None:
                    self.key = recovery_key(self.socket)
                status_hint(self.socket, pane, self.key)
            if error != old_error:
                self.unverified.discard(pane["pane"])
            if error and (error == old_error or error == STALLED) and not offered:
                target = (input_target or (owner(self.socket, pane, startup=True) if error == BACKGROUND
                                          else owner(self.socket, pane)))
                if target:
                    # tmux will not open a second menu over the notice. Wait for
                    # its close callback, including Escape, then offer recovery.
                    if pane["pane"] not in self.unverified or not notice_path(self.socket, pane["pane"]).exists():
                        offered = offer(target, error, viewer=input_viewer) if input_viewer else offer(target, error)
                elif pane["pane"] not in self.unverified and unverified_notice(self.socket, pane["pane"]):
                    self.unverified.add(pane["pane"])
            self.seen[pane["pane"]] = (error, offered if error == old_error or error == STALLED else False)
        return bool(rows)


def source_version():
    try:
        stat = Path(__file__).stat()
    except OSError:
        return None
    if not stat.st_size:
        return None
    return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size


def watch(socket, token=None):
    loaded = source_version()
    with (cache(socket) / "watch.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        watcher = Watcher(socket)
        try:
            runtime = runpy.run_path(str(ROOT / 'libexec/t_runtime.py'))
            runtime_path = cache(socket) / 'watch.runtime.json'
            reload_path = cache(socket) / 'watch.reload.json'
            record = runtime['publish'](runtime_path, 'recoverywatcher', socket, token=token)
            runtime['complete_migration']('recoverywatcher', socket, 'server', record,
                                          (runtime_path, socket, None))
        except (OSError, ValueError, TypeError, SyntaxError):
            runtime, record = None, None
        while True:
            if record and reload_path.exists():
                try:
                    request = runtime['replacement'](reload_path, record, 'recoverywatcher')
                    if request:
                        # Existing recovery self-exec semantics release/reacquire
                        # its singleton lock; no agent or job receives a signal.
                        os.execv(sys.executable, [sys.executable, str(runtime['dispatcher']()),
                                 request['source'], 'libexec/t_recovery.py', 'watch-resume', socket, record['token']])
                except (OSError, ValueError, KeyError, TypeError, SyntaxError):
                    reload_path.unlink(missing_ok=True)
            # t update replaces this source, but a long-lived Python process
            # otherwise keeps the old module in memory indefinitely. exec drops
            # the close-on-exec flock; the replacement reacquires it or exits if
            # another watcher won the race.
            updated = source_version()
            if updated and updated != loaded:
                # A source file being rewritten may be absent, empty, or only
                # partly written. Keep the existing monitor until it is valid.
                try:
                    compile(Path(__file__).read_bytes(), __file__, "exec")
                except (OSError, SyntaxError, UnicodeError):
                    pass
                else:
                    if source_version() == updated:
                        os.execv(sys.executable, [sys.executable, str(Path(__file__)), "watch", socket])
            if not watcher.poll():
                break
            time.sleep(3)
    return 0


def start(session, socket=None):
    if os.environ.get("T_RECOVERY_DISABLE"):
        return 0
    result = run(["tmux", "display-message", "-p", "-t", "=" + session + ":", "#{socket_path}"]) if not socket else None
    socket = result.stdout.strip() if result else socket
    if (result and result.returncode) or not socket.startswith("/"):
        return 1
    runtime = runpy.run_path(str(ROOT / 'libexec/t_runtime.py'))
    try:
        socket = runtime['canonical_socket'](socket)
    except (OSError, ValueError):
        return 1
    paths = runtime['worker_paths']('recoverywatcher', socket)
    if sum(runtime['lock_held'](runtime['worker_lock'](path, 'recoverywatcher')) for path in paths) > 1:
        runtime['defer_migration']('recoverywatcher', socket, 'server', 'multiple historical recovery owners are preserved')
        return 0
    for alias in runtime['socket_aliases'](socket):
        lockpath = cache(alias) / 'watch.lock'
        if not lockpath.exists():
            continue
        with os.fdopen(os.open(lockpath, os.O_RDWR | os.O_NOFOLLOW), 'r+') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                path = cache(alias) / 'watch.runtime.json'
                record = runtime['read'](path)
                adopted = runtime['adopt_worker_record']('recoverywatcher', socket, path)
                record = runtime['read'](path)
                if not adopted or not runtime['complete_migration']('recoverywatcher', socket, 'server', record, (path, socket, None)):
                    runtime['defer_migration']('recoverywatcher', socket, 'server',
                                             'waiting for the existing recovery watcher safe boundary')
                return 0
    env = {key: value for key, value in os.environ.items() if not key.startswith("COV_CORE_")}
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "watch", socket],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, env=env, close_fds=True, cwd="/")
    return 0


def cursor_hook(data):
    roots = data.get('workspace_roots') or []
    cwd = data.get('cwd') or (roots[0] if len(roots) == 1 else None)
    if cwd:
        try:
            context = __import__('runpy').run_path(str(ROOT / 'libexec/t_workflow_context.py'))['context']('cursor', cwd)
            if context:
                print(json.dumps({'additional_context': context}))
        except (OSError, ValueError, subprocess.SubprocessError):
            print('t workflow: Cursor context unavailable; use t workflow context --tool cursor', file=sys.stderr)
    sid = data.get("conversation_id") or data.get("session_id", "")
    pane_id = os.environ.get("TMUX_PANE", "")
    if not re.fullmatch(r"%\d+", pane_id):
        return 0
    result = run(["tmux", "display-message", "-p", "-t", pane_id, "#{socket_path}"])
    socket = result.stdout.strip()
    if result.returncode or not socket.startswith("/"):
        return 0
    pane = next((p for p in panes(socket) if p["pane"] == pane_id and p["session"].startswith("t-cursor-")), None)
    if not pane:
        return 0
    path = cache(socket) / (pane_id.replace("%", "pane-") + ".json")
    # A new SessionStart invalidates the predecessor even if this event cannot
    # be registered. Never retain a stale ID after /new or /resume.
    path.unlink(missing_ok=True)
    roots = data.get("workspace_roots") or []
    cwd = data.get("cwd") or (roots[0] if roots else pane["cwd"])
    if not UUID.fullmatch(sid) or os.path.realpath(cwd) != os.path.realpath(pane["cwd"]):
        return 0
    pid = os.getppid()
    for _ in range(64):
        info = process(pid)
        if not info:
            return 0
        if cursor_process(info) and descendant(pid, pane["pane_pid"]):
            write_json(path,
                       {"pid": str(pid), "start": info["start"], "agent": "cursor", "sid": sid, "cwd": pane["cwd"]})
            return start(pane["session"])
        pid = int(info["ppid"])
        if pid <= 1:
            return 0
    return 0


def install_cursor_hook():
    path = (Path.home() / ".cursor" / "hooks.json").resolve()
    data = json.loads(path.read_text()) if path.exists() else {"version": 1, "hooks": {}}
    if not isinstance(data, dict) or data.get("version", 1) != 1 or not isinstance(data.get("hooks", {}), dict):
        raise ValueError("unsupported Cursor hooks.json; configure t's sessionStart hook before resuming")
    hooks = data.setdefault("hooks", {}).setdefault("sessionStart", [])
    if not isinstance(hooks, list):
        raise ValueError("Cursor sessionStart hooks must be a list")
    command = '"$HOME/bin/t" __recovery cursor-hook'
    if any(isinstance(h, dict) and h.get("command") == command for h in hooks):
        return
    hooks.append({"command": command})
    path.parent.mkdir(parents=True, exist_ok=True)
    write_json(path, data)


def cursor_launch(cwd, sid):
    helper = __import__('runpy').run_path(str(ROOT / 'libexec/t_workflow_cursor.py'))
    args = helper['prepare'](cwd)
    recovery = __import__('runpy').run_path(str(ROOT / 'libexec/t_auto_recovery.py'))
    return 'exec ' + shlex.join([*recovery['prefix']('cursor'), 'cursor-agent', '--resume=' + sid, *args])


def cursor_resume(sid, cwd):
    if not UUID.fullmatch(sid) or not Path(cwd).is_dir():
        raise ValueError("Cursor recovery needs an exact chat ID and existing workspace")
    if not shutil.which("cursor-agent"):
        raise ValueError("cursor-agent is not installed")
    install_cursor_hook()
    session = "t-cursor-" + sid
    known = set()
    socket = run(["tmux", "display-message", "-p", "#{socket_path}"]).stdout.strip()
    if socket.startswith("/"):
        for pane in panes(socket):
            if not pane["session"].startswith("t-cursor-"):
                continue
            actual = owner(socket, pane)
            if not actual:
                raise ValueError("a Cursor pane has no verified current chat; inspect it before opening another")
            known.add(actual["pid"])
            if actual["sid"] == sid:
                if actual["cwd"] != cwd:
                    raise ValueError("this Cursor chat is already running in another workspace")
                session = actual["session"]
    exists = run(["tmux", "has-session", "-t", "=" + session]).returncode == 0
    if not exists:
        # An outside CLI may have used /new since its argv was recorded. Without
        # a current hook record, even a different --resume ID proves nothing.
        processes = run(["ps", "-Aww", "-o", "pid=,args="])
        if processes.returncode:
            raise ValueError("could not verify existing Cursor clients")
        for line in processes.stdout.splitlines():
            fields = line.strip().split(None, 1)
            if len(fields) == 2 and fields[0] not in known and cursor_process({"args": fields[1]}):
                raise ValueError("a Cursor CLI has no verified current chat; close it before opening another")
        launch = cursor_launch(cwd, sid)
        result = run(["tmux", "new-session", "-d", "-s", session, "-c", cwd, "zsh", "-lic", launch])
        if result.returncode:
            raise ValueError(result.stderr.strip())
    else:
        socket = run(["tmux", "display-message", "-p", "-t", "=" + session + ":", "#{socket_path}"]).stdout.strip()
        rows = panes(socket)
        actual = next((owner(socket, row) for row in rows if row["session"] == session), {})
        if not actual or actual["sid"] != sid or actual["cwd"] != cwd:
            raise ValueError("the existing Cursor pane has changed; inspect it before resuming")
    start(session)
    os.execvp("tmux", ["tmux", "switch-client" if os.environ.get("TMUX") else "attach-session", "-t", "=" + session])


def cursor_restart(target):
    lock_path = cache(target["socket"]) / (target["pane"].replace("%", "pane-") + ".restart.lock")
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("Cursor recovery is already running for this pane") from None
        return _cursor_restart(target)


def _cursor_restart(target):
    socket, pane, pid = target["socket"], target["pane"], int(target["pid"])
    if not current(target) or descendant(os.getpid(), pid):
        raise ValueError("Cursor owner changed; no client was stopped")
    captured = tmux(socket, "capture-pane", "-p", "-J", "-S", "-", "-t", pane)
    if captured.returncode:
        raise ValueError("could not save the visible draft")
    fd, path = tempfile.mkstemp(prefix="cursor-draft-", suffix=".txt", dir=cache(socket))
    with os.fdopen(fd, "w") as file:
        file.write(captured.stdout)
    previous = tmux(socket, "show-options", "-A", "-p", "-v", "-t", pane, "remain-on-exit")
    if previous.returncode or not current(target):
        raise ValueError("Cursor pane changed; no client was stopped")
    launch = cursor_launch(target['cwd'], target['sid'])
    if not current(target):
        raise ValueError("Cursor owner changed while preparing workflow; no client was stopped")
    if tmux(socket, "set-option", "-p", "-t", pane, "remain-on-exit", "on").returncode:
        raise ValueError("could not reserve Cursor's pane")
    os.kill(pid, signal.SIGTERM)
    for _ in range(200):
        gone = not process(pid) or zombie(pid)
        if gone and tmux(socket, "display-message", "-p", "-t", pane, "#{pane_dead}").stdout.strip() == "1":
            result = tmux(socket, "respawn-pane", "-t", pane, "-c", target["cwd"], "zsh", "-lic", launch)
            if result.returncode:
                raise ValueError(result.stderr.strip())
            tmux(socket, "set-option", "-p", "-t", pane, "remain-on-exit", previous.stdout.strip())
            return path
        time.sleep(0.05)
    raise ValueError("the old Cursor client has not exited; no second client was started")


def response_message(socket, target, message):
    # -t supplies format context, not the recipient of display-message. Without
    # -c tmux may interrupt an unrelated terminal (and consume its next key).
    # Never fall back to another client if the original viewer left this pane,
    # or if this offer predates the recorded viewer.
    viewer = target.get("viewer")
    if viewer and viewer in clients(socket, target["pane"]):
        tmux(socket, "display-message", "-d", "10000", "-c", viewer,
             "-t", target["pane"], message)


def capture_stop(target):
    immutable = tmux(target["socket"], "display-message", "-p", "-t", target["pane"],
                     "#{session_id}").stdout.strip()
    if not re.fullmatch(r"\$[0-9]+", immutable):
        raise ValueError("could not verify the terminal session for force stop")
    result = subprocess.run([sys.executable, str(ROOT / "libexec/t_client_stop.py"), "capture",
                             target["session"], target["cwd"], immutable, target["pid"], target["agent"]],
                            env=dict(os.environ, TMUX=target["socket"] + ",0,0"),
                            capture_output=True, text=True, timeout=15, stdin=subprocess.DEVNULL, cwd="/")
    if result.returncode:
        raise ValueError(result.stderr.strip() or "could not verify the client for force stop")
    token = json.loads(result.stdout)
    if not isinstance(token, dict):
        raise ValueError("could not verify the client for force stop")
    return token


def respond(socket, token, accept, pane=None, viewer=None):
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        return 1
    path = cache(socket) / (token + ".offer")
    claimed = path.with_suffix(".claimed")
    try:
        os.rename(path, claimed)  # A menu response is single-use, across clients.
    except FileNotFoundError:
        if accept and pane and viewer:
            response_message(socket, {"pane": pane, "viewer": viewer},
                             "t recovery: this menu is no longer available; reopen recovery using the status shortcut")
        return 1
    target = read_json(claimed)
    claimed.unlink(missing_ok=True)
    if not accept:
        return 0
    try:
        if target.get("socket") != socket or not current(target):
            raise ValueError("the conversation changed; reopen recovery using the status shortcut")
        if target["error"] == STALLED and not target.get("force"):
            count, _ = pending_input(socket, target["pane"], target.get("viewer"))
            if not count:
                raise ValueError("terminal input is no longer stalled")
        elif target["error"] not in (DEAD, REQUESTED) and not target.get("force"):
            screen = tmux(socket, "capture-pane", "-p", "-J", "-t", target["pane"])
            if failure(target["agent"], screen.stdout) != target["error"]:
                raise ValueError("the connection failure is no longer visible")
        if target["agent"] == "cursor":
            saved = cursor_restart(target)
            message = "Restarted Cursor. Saved visible draft: " + saved
        else:
            mode = ("restart-dead" if target["error"] == DEAD else
                    "restart-no-daemon" if target.get("startup") else
                    "restart-invalid-cwd" if target["error"] == INVALID_CWD else "restart")
            args = ["_t_restart_slot", target["session"], target["cwd"], target["sid"], target["agent"], mode, target["pid"]]
            if mode == "restart-dead":
                args.append(" ".join(target[key] for key in ("pane", "pane_pid", "pane_dead_time", "pane_dead")))
            elif target.get("force"):
                args.extend(["", json.dumps(target["stop_token"], separators=(",", ":"))])
            env = dict(os.environ, TMUX=socket + ",0,0")
            result = subprocess.run(["zsh", "-lic", "setopt no_monitor no_notify; " + shlex.join(args)],
                                    env=env, text=True, capture_output=True, timeout=30,
                                    stdin=subprocess.DEVNULL, cwd="/")
            if result.returncode:
                if result.returncode == 75 and not target.get("force") and current(target):
                    stop_token = capture_stop(target)
                    if current(target) and offer({**target, "stop_token": stop_token}, target["error"],
                                                 viewer=target.get("viewer"), force=True):
                        return 1
                raise ValueError(result.stderr.strip() or "restart failed")
            message = result.stdout.strip()
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        message = "t recovery: " + str(error)
        response_message(socket, target, message)
        return 1
    response_message(socket, target, message)
    return 0


def main(argv):
    try:
        action, *args = argv
        if action == "start":
            return start(*args)
        if action in ("watch", "watch-resume"):
            return watch(*args)
        if action == "reopen":
            # These are tmux UI callbacks, not shell-facing success codes.
            # run-shell opens a pane-wide view-mode buffer on nonzero exit,
            # obscuring the agent even when refusal was already shown via -c.
            reopen(*args)
            return 0
        if action == "cursor-hook":
            return cursor_hook(json.load(sys.stdin))
        if action == "cursor-resume":
            return cursor_resume(*args)
        if action in ("accept", "dismiss"):
            respond(*args[:2], action == "accept", *args[2:])
            return 0  # Includes callbacks from old menus whose offer is gone.
        raise ValueError("unknown recovery action")
    except (OSError, ValueError, TypeError, SyntaxError) as error:
        print("t recovery: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
