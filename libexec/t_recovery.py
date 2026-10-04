"""Local recovery menus for t's terminal clients; no model or network dependency."""

import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

ROOT = Path(__file__).resolve().parent.parent
UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
BACKGROUND = "Cannot use the background server"
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
    if (agent == "codex" and BACKGROUND in visible and (incompatible or unavailable)
            and any(re.fullmatch(r"[>›❯]?\s*1\. Run without daemon this time", line)
                    for line in visible)):
        return BACKGROUND
    return ""


def panes(socket):
    result = tmux(socket, "list-panes", "-a", "-F", "#{session_name}\t#{pane_id}\t#{pane_pid}\t#{session_path}")
    groups = {}
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) == 4 and fields[0].startswith(("dev-", "t-cursor-")):
            groups.setdefault(fields[0], []).append(dict(zip(("session", "pane", "pane_pid", "cwd"), fields)))
    return [rows[0] for rows in groups.values() if len(rows) == 1]


def process(pid):
    if not str(pid).isdigit():
        return {}
    result = run(["ps", "-ww", "-p", str(pid), "-o", "ppid=,lstart=,args="])
    fields = result.stdout.strip().split(None, 6)
    if result.returncode or len(fields) != 7 or not fields[0].isdigit():
        return {}
    return {"ppid": fields[0], "start": " ".join(fields[1:6]), "args": fields[6]}


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
        result = read_json(cache(socket) / (pane["pane"].replace("%", "pane-") + ".json"))
        info = process(result.get("pid", ""))
        if (not UUID.fullmatch(result.get("sid", "")) or result.get("cwd") != pane["cwd"]
                or result.get("start") != info.get("start") or not cursor_process(info)
                or not descendant(result.get("pid"), pane["pane_pid"])):
            return {}
    else:
        # Login zsh is the shared local/remote integration contract. Resolve the
        # current thread, not the newest transcript in this worktree.
        snippet = 'setopt no_monitor no_notify; s=%s; d=%s; print -r -- "$(_dev_agent_of_session "$s")"$\'\\t\'"$(_dev_session_sid "$s" "$d")"$\'\\t\'"$(_dev_session_claude_pid "$s")"' % (
            shlex.quote(pane["session"]), shlex.quote(pane["cwd"]))
        result = run(["zsh", "-lic", "export TMUX=" + shlex.quote(socket + ",0,0") + "; " + snippet])
        fields = result.stdout.strip().split("\t")
        if result.returncode or len(fields) != 3 or fields[0] not in ("codex", "claude"):
            return {}
        result = dict(zip(("agent", "sid", "pid"), fields))
        info = process(result["pid"])
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
            keys = ("agent", "sid", "pid", "start", "session", "pane", "cwd")
            return all(actual.get(key) == target.get(key) for key in keys)
    return False


def clients(socket, pane):
    result = tmux(socket, "list-clients", "-F", "#{client_name}\t#{pane_id}")
    return [fields[0] for line in result.stdout.splitlines()
            if len(fields := line.split("\t")) == 2 and fields[1] == pane]


def offer(target, error):
    socket = target["socket"]
    viewers = clients(socket, target["pane"])
    if not viewers:
        return False
    token = uuid.uuid4().hex
    path = cache(socket) / (token + ".offer")
    write_json(path, {**target, "error": error, "created": time.time()})
    callback = shlex.join([sys.executable, str(Path(__file__).resolve()), "accept", socket, token])
    dismiss = shlex.join([sys.executable, str(Path(__file__).resolve()), "dismiss", socket, token])
    # One menu, on a client actually viewing this pane. No keystrokes enter the
    # agent and no action runs until the user chooses Restart.
    if target.get("startup"):
        title, choice = "t: background server unavailable", "Run without daemon this time"
    elif error == INVALID_CWD:
        title, choice = "t: workspace unavailable — recover this conversation?", "Save visible draft and restart here"
    else:
        title, choice = "t: connection failed — recover this conversation?", "Save visible draft and restart"
    menu = ["tmux", "-S", socket, "display-menu", "-c", viewers[0], "-t", target["pane"],
            "-T", title, "-x", "C", "-y", "C",
            choice, "r", "run-shell -b " + shlex.quote(callback),
            "Dismiss", "q", "run-shell -b " + shlex.quote(dismiss)]
    # display-menu waits for input. Let tmux own that wait so one unanswered
    # offer neither stalls other panes nor hits our subprocess timeout.
    result = tmux(socket, "run-shell", "-b", shlex.join(menu))
    if result.returncode:
        path.unlink(missing_ok=True)
    return result.returncode == 0


class Watcher:
    def __init__(self, socket):
        self.socket = socket
        self.seen = {}

    def poll(self):
        for path in cache(self.socket).glob("*.offer"):
            if time.time() - read_json(path).get("created", 0) > 300:
                path.unlink(missing_ok=True)
        rows = panes(self.socket)
        active = {row["pane"] for row in rows}
        self.seen = {key: value for key, value in self.seen.items() if key in active}
        for pane in rows:
            # Agent stamps are only a detection hint; owner() verifies them before
            # any offer. Cursor owners come from its SessionStart hook.
            if pane["session"].startswith("t-cursor-"):
                agent = "cursor"
            else:
                stamp = tmux(self.socket, "show-environment", "-t", "=" + pane["session"], "DEV_AGENT")
                agent = stamp.stdout.strip().removeprefix("DEV_AGENT=")
            screen = tmux(self.socket, "capture-pane", "-p", "-J", "-t", pane["pane"])
            error = failure(agent, screen.stdout) if screen.returncode == 0 else ""
            old_error, offered = self.seen.get(pane["pane"], ("", False))
            if error and error == old_error and not offered:
                target = owner(self.socket, pane, startup=True) if error == BACKGROUND else owner(self.socket, pane)
                if target:
                    offered = offer(target, error)
            self.seen[pane["pane"]] = (error, offered if error == old_error else False)
        return bool(rows)


def source_version():
    try:
        stat = Path(__file__).stat()
    except OSError:
        return None
    if not stat.st_size:
        return None
    return stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size


def watch(socket):
    loaded = source_version()
    with (cache(socket) / "watch.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        watcher = Watcher(socket)
        while True:
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


def start(session):
    if os.environ.get("T_RECOVERY_DISABLE"):
        return 0
    result = run(["tmux", "display-message", "-p", "-t", "=" + session + ":", "#{socket_path}"])
    socket = result.stdout.strip()
    if result.returncode or not socket.startswith("/"):
        return 1
    env = {key: value for key, value in os.environ.items() if not key.startswith("COV_CORE_")}
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), "watch", socket],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, env=env, close_fds=True, cwd="/")
    return 0


def cursor_hook(data):
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
        launch = "exec cursor-agent --resume=" + shlex.quote(sid)
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
    if tmux(socket, "set-option", "-p", "-t", pane, "remain-on-exit", "on").returncode:
        raise ValueError("could not reserve Cursor's pane")
    os.kill(pid, signal.SIGTERM)
    for _ in range(200):
        if not process(pid) and tmux(socket, "display-message", "-p", "-t", pane, "#{pane_dead}").stdout.strip() == "1":
            launch = "exec cursor-agent --resume=" + shlex.quote(target["sid"])
            result = tmux(socket, "respawn-pane", "-t", pane, "-c", target["cwd"], "zsh", "-lic", launch)
            if result.returncode:
                raise ValueError(result.stderr.strip())
            tmux(socket, "set-option", "-p", "-t", pane, "remain-on-exit", previous.stdout.strip())
            return path
        time.sleep(0.05)
    raise ValueError("the old Cursor client has not exited; no second client was started")


def respond(socket, token, accept):
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        return 1
    path = cache(socket) / (token + ".offer")
    claimed = path.with_suffix(".claimed")
    try:
        os.rename(path, claimed)  # A menu response is single-use, across clients.
    except FileNotFoundError:
        return 1
    target = read_json(claimed)
    claimed.unlink(missing_ok=True)
    if not accept:
        return 0
    try:
        if target.get("socket") != socket or time.time() - target.get("created", 0) > 300 or not current(target):
            raise ValueError("the recovery offer expired or the conversation changed")
        screen = tmux(socket, "capture-pane", "-p", "-J", "-t", target["pane"])
        if failure(target["agent"], screen.stdout) != target["error"]:
            raise ValueError("the connection failure is no longer visible")
        if target["agent"] == "cursor":
            saved = cursor_restart(target)
            message = "Restarted Cursor. Saved visible draft: " + saved
        else:
            mode = ("restart-no-daemon" if target.get("startup") else
                    "restart-invalid-cwd" if target["error"] == INVALID_CWD else "restart")
            args = ["_t_restart_slot", target["session"], target["cwd"], target["sid"], target["agent"], mode, target["pid"]]
            env = dict(os.environ, TMUX=socket + ",0,0")
            result = subprocess.run(["zsh", "-lic", "setopt no_monitor no_notify; " + shlex.join(args)],
                                    env=env, text=True, capture_output=True, timeout=30,
                                    stdin=subprocess.DEVNULL, cwd="/")
            if result.returncode:
                raise ValueError(result.stderr.strip() or "restart failed")
            message = result.stdout.strip()
    except (ValueError, OSError, subprocess.TimeoutExpired) as error:
        message = "t recovery: " + str(error)
        tmux(socket, "display-message", "-d", "10000", "-t", target.get("pane", ""), message)
        return 1
    tmux(socket, "display-message", "-d", "10000", "-t", target["pane"], message)
    return 0


def main(argv):
    try:
        action, *args = argv
        if action == "start":
            return start(*args)
        if action == "watch":
            return watch(*args)
        if action == "cursor-hook":
            return cursor_hook(json.load(sys.stdin))
        if action == "cursor-resume":
            return cursor_resume(*args)
        if action in ("accept", "dismiss"):
            return respond(*args, accept=action == "accept")
        raise ValueError("unknown recovery action")
    except (OSError, ValueError, TypeError) as error:
        print("t recovery: " + str(error), file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
