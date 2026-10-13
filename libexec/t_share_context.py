"""Bounded, read-only selected history for an exact local share source.

This is an excerpt of recorded conversation, not a semantic or model summary.
Only user and assistant text is selected; provider instructions and tool records
are excluded. The caller supplies a row already resolved by t_share_sessions.
"""

from collections import deque
from contextlib import closing
import json
import os
from pathlib import Path
import re
import sqlite3
import stat


DEFAULT_CHARS = 24_000
DEFAULT_MESSAGES = 40
DEFAULT_SCAN_BYTES = 8 * 1024 * 1024
MAX_CHARS = 100_000
MAX_MESSAGES = 200
MAX_SCAN_BYTES = 32 * 1024 * 1024
UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
OPAQUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")


class ContextError(ValueError):
    pass


def _source(row):
    if not isinstance(row, dict):
        raise ContextError("source must be a verified local conversation")
    agent, sid, cwd = row.get("agent"), row.get("id"), row.get("cwd")
    if (agent not in ("claude", "codex", "cursor") or not isinstance(sid, str)
            or not (OPAQUE.fullmatch(sid) if agent == "cursor" else UUID.fullmatch(sid))
            or row.get("address") != agent + ":" + sid
            or row.get("host") != "local" or not isinstance(cwd, str)
            or not (os.path.isabs(cwd) or agent == "cursor" and cwd == "")):
        raise ContextError("source must be an exact verified local conversation")
    return agent, sid, cwd


def _clean(value):
    if not isinstance(value, str):
        return ""
    return re.sub(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]", " ", value).strip()


def _text(content, role, agent):
    if isinstance(content, str):
        return _clean(content) if agent == "claude" and role == "user" else ""
    if not isinstance(content, list):
        return ""
    expected = {"claude": {"user": "text", "assistant": "text"},
                "codex": {"user": "input_text", "assistant": "output_text"}}[agent][role]
    return _clean("\n".join(block.get("text", "") for block in content
                            if isinstance(block, dict) and block.get("type") == expected
                            and isinstance(block.get("text"), str)))


def _injected_user(value):
    start = value.lstrip()
    return (bool(re.match(
        r"<(?:system-reminder|command-name|command-message|local-command-stdout|"
        r"local-command-caveat|environment_context|permissions|skills_instructions|"
        r"multi_agent_role|multi_agent_mode)(?:\s|>)", start))
        or start.startswith(("# AGENTS.md instructions", "# Skills", "# Instructions")))


def _message(row, agent, sid):
    if not isinstance(row, dict):
        return None
    if agent == "claude":
        role = row.get("type")
        if role not in ("user", "assistant") or row.get("isMeta") or row.get("isSidechain"):
            return None
        recorded = row.get("sessionId")
        if recorded and recorded != sid:
            raise ContextError("Claude transcript changed conversation identity")
        message = row.get("message")
        if not isinstance(message, dict) or message.get("role", role) != role:
            return None
        value = _text(message.get("content"), role, agent)
    else:
        if row.get("type") != "response_item":
            return None
        message = row.get("payload")
        if not isinstance(message, dict) or message.get("type") != "message":
            return None
        role = message.get("role")
        if role not in ("user", "assistant"):
            return None
        value = _text(message.get("content"), role, agent)
    if not value or role == "user" and _injected_user(value):
        return None
    return role, value


def _read_tail(path, scan_bytes):
    """Return complete recent JSONL lines without reading the whole transcript."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ContextError("source transcript is not a regular file")
            size = info.st_size
            first = stream.readline(65537)
            if len(first) > 65536:
                raise ContextError("source transcript header is too large")
            offset = max(0, size - scan_bytes)
            stream.seek(offset)
            data = stream.read(min(scan_bytes, size - offset))
    except (OSError, ValueError) as error:
        raise ContextError("source transcript cannot be read") from error
    if offset:
        data = data.partition(b"\n")[2]
    if data and not data.endswith(b"\n"):
        data = data.rpartition(b"\n")[0] + b"\n" if b"\n" in data else b""
    return first, data.splitlines(), bool(offset)


def _first_record(path):
    first, _, _ = _read_tail(path, 1)
    try:
        return json.loads(first)
    except (UnicodeError, ValueError) as error:
        raise ContextError("source transcript has no valid identity header") from error


def _claude_path(sid, cwd):
    root = Path(os.environ.get("CLAUDE_CONFIG_DIR") or Path.home() / ".claude") / "projects"
    candidates = []
    for path in root.glob("*/" + sid + ".jsonl"):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    continue
                found_cwd = ""
                wrong_id = False
                for _ in range(80):
                    raw = stream.readline(65537)
                    if not raw or len(raw) > 65536:
                        break
                    try:
                        record = json.loads(raw)
                    except (UnicodeError, ValueError):
                        continue
                    if not isinstance(record, dict):
                        continue
                    recorded_id = record.get("sessionId")
                    if recorded_id is not None and recorded_id != sid:
                        wrong_id = True
                        break
                    if not found_cwd and isinstance(record.get("cwd"), str):
                        found_cwd = record["cwd"]
        except OSError:
            continue
        if found_cwd == cwd and not wrong_id:
            candidates.append(path)
    if not candidates:
        raise ContextError("verified Claude transcript is unavailable; provide a summary file")
    return max(candidates, key=lambda path: (path.stat().st_size, str(path)))


def _codex_path(sid, cwd):
    home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
    db = home / "state_5.sqlite"
    candidates = []
    if db.is_file():
        try:
            with closing(sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=.2)) as conn:
                record = conn.execute("select cwd,rollout_path,source from threads where id=?", (sid,)).fetchone()
            if record and record[0] == cwd and '"subagent"' not in (record[2] or "") and record[1]:
                candidates.append((Path(record[1]), True))
        except (OSError, sqlite3.Error):
            pass
    candidates.extend((path, False) for path in (home / "sessions").glob("*/*/*/rollout-*-" + sid + ".jsonl"))
    for path, indexed in candidates:
        try:
            first = _first_record(path)
        except ContextError:
            continue
        if (not isinstance(first, dict) or first.get("type") != "session_meta"
                or not isinstance(first.get("payload"), dict)):
            continue
        payload = first["payload"]
        if payload.get("id") != sid or payload.get("thread_source") == "subagent":
            continue
        if not indexed and payload.get("cwd") != cwd:
            continue
        return path
    raise ContextError("verified Codex rollout is unavailable; provide a summary file")


def _clip(value, limit):
    if len(value) <= limit:
        return value, False
    return "[beginning omitted]\n" + value[-max(1, limit - 20):], True


def _clip_original(value, limit):
    if len(value) <= limit:
        return value, False
    marker = "\n[middle omitted]\n"
    room = max(2, limit - len(marker))
    head = max(1, room * 2 // 3)
    return value[:head] + marker + value[-(room - head):], True


def _first_user(path, agent, sid, limit=1024 * 1024):
    """Preserve the original task when recent history has rolled past it."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            data = stream.read(limit + 1)
    except OSError:
        return None
    if len(data) > limit:
        data = data.rpartition(b"\n")[0]
    for raw in data.splitlines():
        try:
            row = json.loads(raw)
        except (UnicodeError, ValueError):
            continue
        item = _message(row, agent, sid)
        if item and item[0] == "user":
            return item
    return None


def snapshot(source, *, max_chars=DEFAULT_CHARS, max_messages=DEFAULT_MESSAGES,
             scan_bytes=DEFAULT_SCAN_BYTES):
    """Extract recent selected text from one exact Claude/Codex conversation.

    Returns a dict with source address, labeled text, selected_count, truncated,
    and truncation_reasons. Never calls a model or writes a provider store.
    """
    agent, sid, cwd = _source(source)
    if agent == "cursor":
        raise ContextError("Cursor chat text has no verified exporter; use --file with a summary or paste context")
    if (type(max_chars) is not int or not 200 <= max_chars <= MAX_CHARS
            or type(max_messages) is not int or not 1 <= max_messages <= MAX_MESSAGES
            or type(scan_bytes) is not int or not 1024 <= scan_bytes <= MAX_SCAN_BYTES):
        raise ContextError("invalid context bounds")
    path = _claude_path(sid, cwd) if agent == "claude" else _codex_path(sid, cwd)
    _, lines, tail_cut = _read_tail(path, scan_bytes)
    selected = deque(maxlen=max_messages)
    eligible = 0
    for raw in lines:
        try:
            row = json.loads(raw)
        except (UnicodeError, ValueError):
            continue
        item = _message(row, agent, sid)
        if item:
            eligible += 1
            selected.append(item)
    if not selected:
        raise ContextError("no user or assistant text was found in the selected transcript window")
    reasons = []
    if tail_cut:
        reasons.append("earlier transcript bytes omitted")
    if eligible > len(selected):
        reasons.append("earlier messages omitted")
    header = ("Selected conversation history from " + source["address"] + ".\n"
              "This is recorded user and assistant text, not a generated summary.\n")
    budget = max_chars - len(header) - 80
    if budget < 1:
        raise ContextError("context bound is too small for the source address")
    initial = _first_user(path, agent, sid) if max_messages > 1 else None
    recent = list(selected)
    first_block = None
    if initial and initial not in recent:
        clipped, did_clip = _clip_original(initial[1], min(2000, max(40, budget // 4)))
        first_block = "USER (original request):\n" + clipped
        budget -= len(first_block) + 2
        recent = recent[-(max_messages - 1):]
        reasons.append("middle messages omitted")
        if did_clip:
            reasons.append("original request was shortened")
    blocks = []
    for role, value in reversed(recent):
        allowance = min(4000, budget)
        if allowance < 50:
            reasons.append("earlier messages omitted by output limit")
            break
        clipped, did_clip = _clip(value, allowance - 20)
        if did_clip:
            reasons.append("a long message was shortened")
        block = role.upper() + ":\n" + clipped
        blocks.append(block)
        budget -= len(block) + 2
    blocks.reverse()
    if len(blocks) < len(recent):
        reasons.append("earlier messages omitted by output limit")
    if first_block:
        blocks.insert(0, first_block)
    if not blocks:
        raise ContextError("context bound leaves no room for a conversation message")
    reasons = list(dict.fromkeys(reasons))
    note = "Some earlier history or message parts were omitted to fit the limit.\n" if reasons else ""
    output = header + note + "\n" + "\n\n".join(blocks)
    if len(output) > max_chars:
        # The note is bounded, but keep this as a hard guarantee if labels grow.
        marker = "\n[output shortened]"
        output = output[:max_chars - len(marker)] + marker
        if "output shortened" not in reasons:
            reasons.append("output shortened")
    return {"source": source["address"], "text": output, "selected_count": len(blocks),
            "truncated": bool(reasons), "truncation_reasons": reasons}
