"""Read-only, exact conversation discovery for ``t share``.

Display metadata never establishes ownership. Automatic identity requires an
exact provider record tied to this process; callers can supply an address when
the provider does not expose one.
"""

import hashlib
import datetime
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess


UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
AGENTS = ("claude", "codex", "cursor")


def _home():
    return Path.home()


def _claude_home():
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or _home() / ".claude").expanduser()


def _codex_home():
    return Path(os.environ.get("CODEX_HOME") or _home() / ".codex").expanduser()


def _cache():
    return Path(os.environ.get("XDG_CACHE_HOME") or _home() / ".cache")


def _clean(value):
    return " ".join(re.sub(r"[\x00-\x1f\x7f-\x9f]", " ", value if isinstance(value, str) else "").split())[:160]


def _row(agent, sid, cwd, title):
    if (not isinstance(sid, str) or not OPAQUE.fullmatch(sid) or not isinstance(cwd, str)
            or (not os.path.isabs(cwd) and not (agent == "cursor" and cwd == ""))):
        return None
    return {"agent": agent, "id": sid, "address": agent + ":" + sid,
            "cwd": cwd, "title": _clean(title) or "(untitled)", "host": "local"}


def _first_json(path, limit=65536):
    try:
        with path.open("rb") as stream:
            return json.loads(stream.readline(limit + 1)[:limit])
    except (OSError, UnicodeError, ValueError):
        return {}


def _read_json(path, limit=65536):
    try:
        with path.open("rb") as stream:
            data = stream.read(limit + 1)
        if len(data) > limit:
            return {}
        return json.loads(data)
    except (OSError, UnicodeError, ValueError):
        return {}


def _claude_targets():
    root = _claude_home() / "projects"
    if not root.is_dir():
        return []
    found = []
    for path in root.glob("*/*.jsonl"):
        if not UUID.fullmatch(path.stem) or not path.is_file():
            continue
        cwd, title = "", ""
        try:
            with path.open("rb") as stream:
                for _ in range(80):
                    line = stream.readline(65537)
                    if not line or len(line) > 65536:
                        break
                    try:
                        item = json.loads(line)
                    except (UnicodeError, ValueError):
                        continue
                    if not isinstance(item, dict):
                        continue
                    cwd = cwd or item.get("cwd", "")
                    if item.get("type") == "user":
                        message = item.get("message") or {}
                        content = message.get("content") if isinstance(message, dict) else None
                        if isinstance(content, str):
                            title = content
                        elif isinstance(content, list):
                            title = next((part.get("text", "") for part in content
                                          if isinstance(part, dict) and part.get("type") == "text"), "")
                    if cwd and title:
                        break
        except OSError:
            continue
        row = _row("claude", path.stem, cwd, title)
        if row:
            found.append(row)
    return found


def _codex_targets():
    db = _codex_home() / "state_5.sqlite"
    records = []
    if db.is_file():
        try:
            connection = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=.2)
            try:
                columns = {entry[1] for entry in connection.execute("pragma table_info(threads)")}
                if {"id", "cwd", "source"} <= columns:
                    title = "coalesce(nullif(name,''),nullif(title,''),nullif(first_user_message,''),'')" if {
                        "name", "title", "first_user_message"} <= columns else "''"
                    records = connection.execute("select id,cwd," + title + " from threads "
                                                 "where instr(source, '\"subagent\"')=0").fetchall()
            finally:
                connection.close()
        except (OSError, sqlite3.Error):
            pass
    found = {row["id"]: row for sid, cwd, title in records
             if (row := _row("codex", sid, cwd, title))}
    # A copied-in rollout may be resumable before Codex adds it to its index.
    # Its own session_meta is the only authority for its ID and starting cwd.
    for path in (_codex_home() / "sessions").glob("*/*/*/rollout-*.jsonl"):
        sid = path.stem[-36:]
        if sid in found or not UUID.fullmatch(sid):
            continue
        item = _first_json(path)
        if not isinstance(item, dict) or item.get("type") != "session_meta":
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict) or payload.get("id") != sid or payload.get("thread_source") == "subagent":
            continue
        row = _row("codex", sid, payload.get("cwd"), "")
        if row:
            found[sid] = row
    return list(found.values())


def _cursor_targets():
    root = _home() / ".cursor" / "chats"
    if not root.is_dir():
        return []
    found = []
    for directory in root.glob("*/*"):
        if not directory.is_dir():
            continue
        path = directory / "meta.json"
        data = _read_json(path)
        if not data and not (directory / "store.db").is_file():
            continue
        if not isinstance(data, dict):
            continue
        row = _row("cursor", directory.name, data.get("cwd") or "", data.get("title"))
        if row:
            found.append(row)
    return found


def targets():
    """Saved conversations, including closed and archived chats, by exact ID."""
    found = {}
    for row in _claude_targets() + _codex_targets() + _cursor_targets():
        found.setdefault(row["address"], row)
    return sorted(found.values(), key=lambda row: (row["agent"], row["address"]))


def resolve(address):
    """Require a complete address that exists in a provider store."""
    if not isinstance(address, str):
        raise ValueError("conversation address must be agent:<id>")
    agent, sep, sid = address.partition(":")
    if not sep or agent not in AGENTS or not OPAQUE.fullmatch(sid):
        raise ValueError("conversation address must be claude:<id>, codex:<id>, or cursor:<id>")
    for row in targets():
        if row["address"] == address:
            return row
    raise ValueError("conversation does not exist here: " + address)


def _process(pid):
    if not str(pid).isdigit():
        return None
    try:
        result = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "ppid=,lstart=,args="],
                                capture_output=True, text=True, timeout=2, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    parts = result.stdout.strip().split(None, 6)
    if len(parts) != 7 or not parts[0].isdigit():
        return None
    return {"ppid": int(parts[0]), "start": " ".join(parts[1:6]), "args": parts[6]}


def _ancestors():
    pid = os.getppid()
    for _ in range(64):
        item = _process(pid)
        if not item:
            break
        yield pid, item
        if item["ppid"] <= 1:
            break
        pid = item["ppid"]


def _cursor_identity(ancestors):
    pane = os.environ.get("TMUX_PANE", "")
    tmux = os.environ.get("TMUX", "")
    if not re.fullmatch(r"%\d+", pane) or not tmux.startswith("/"):
        return None
    socket = tmux.split(",", 1)[0]
    cache = (_cache() / "t" / "recovery" /
             hashlib.sha256(socket.encode()).hexdigest()[:20] /
             (pane.replace("%", "pane-") + ".json"))
    data = _read_json(cache)
    if not isinstance(data, dict) or not UUID.fullmatch(str(data.get("sid", ""))):
        return None
    pid = str(data.get("pid", ""))
    owner = next((info for ancestor, info in ancestors if str(ancestor) == pid), None)
    if not owner or owner["start"] != data.get("start") or not _is_agent_process(owner, "cursor"):
        return None
    try:
        result = subprocess.run(["tmux", "-S", socket, "display-message", "-p", "-t", pane,
                                 "#{pane_id}\t#{pane_pid}\t#{session_name}\t#{session_path}\t#{pane_dead}"],
                                capture_output=True, text=True, timeout=2, check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    fields = result.stdout.strip().split("\t")
    # The tmux session keeps its original name after Cursor /new or /resume;
    # SessionStart refreshes this pane's hook record with the current chat ID.
    if (len(fields) != 5 or fields[0] != pane or not fields[2].startswith("t-cursor-")
            or fields[3] != data.get("cwd") or fields[4] != "0"):
        return None
    if not any(str(ancestor) == fields[1] for ancestor, _ in ancestors):
        return None
    return "cursor:" + data["sid"]


def _is_agent_process(item, agent):
    args = item.get("args", "").split()
    if not args:
        return False
    executable = Path(args[0]).name
    if agent == "cursor":
        return any(".app/Contents/" not in token and re.search(r"(?:^|/)cursor-agent(?:/|$)", token)
                   for token in args)
    if agent == "codex":
        return (bool(re.fullmatch(r"codex(?:-(?:aarch64|x86_64)-[\w-]+)?", executable))
                and "app-server" not in args[1:])
    return executable == "claude"


def _registry_identity(pid, item, registry):
    path = registry / str(pid)
    try:
        sid, cwd = path.read_text(encoding="utf-8").strip().split("\t", 1)
        created = path.stat().st_mtime
        started = datetime.datetime.strptime(item["start"], "%a %b %d %H:%M:%S %Y").timestamp()
    except (OSError, ValueError, KeyError):
        return None
    if not UUID.fullmatch(sid) or created + 1 < started:
        return None
    try:
        row = resolve("codex:" + sid)
    except ValueError:
        return None
    return row["address"] if os.path.realpath(cwd) == os.path.realpath(row["cwd"]) else None


def self_address():
    """Current exact conversation or an actionable error; never choose newest."""
    ancestors = list(_ancestors())
    # A nested tool process can inherit an old Claude variable. The nearest
    # actual agent executable decides which provider may claim this shell.
    active = None
    for _, info in ancestors:
        if ("app-server" in info.get("args", "").split()[1:]
                and re.fullmatch(r"codex(?:-(?:aarch64|x86_64)-[\w-]+)?",
                                 Path(info["args"].split()[0]).name)):
            active = "shared-codex-service"
            break
        active = next((agent for agent in AGENTS if _is_agent_process(info, agent)), None)
        if active:
            break
    candidate = _cursor_identity(ancestors)
    if candidate and active in (None, "cursor"):
        try:
            return resolve(candidate)["address"]
        except ValueError:
            pass
    registry = _cache() / "claude-sessions"
    if active in (None, "codex"):
        for pid, item in ancestors:
            if not _is_agent_process(item, "codex"):
                continue
            candidate = _registry_identity(pid, item, registry)
            if candidate:
                return candidate
    claude = os.environ.get("CLAUDE_CODE_SESSION_ID", "")
    if active in (None, "claude") and UUID.fullmatch(claude):
        try:
            return resolve("claude:" + claude)["address"]
        except ValueError:
            pass
    raise ValueError("cannot verify this conversation; pass --from agent:<id> or --for agent:<id>")
