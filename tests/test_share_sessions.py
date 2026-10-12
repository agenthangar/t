"""Conversation discovery uses synthetic provider stores and exact identities."""

import json
import datetime
from pathlib import Path
import runpy
import sqlite3

import pytest


SID_A = "11111111-1111-4111-8111-111111111111"
SID_B = "22222222-2222-4222-8222-222222222222"
SID_C = "33333333-3333-4333-8333-333333333333"


@pytest.fixture
def sessions(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    claude = tmp_path / "other-claude"
    codex = tmp_path / "other-codex"
    claude.mkdir()
    codex.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(Path, "home", lambda: home)
    for name in ("CLAUDE_CODE_SESSION_ID", "TMUX_PANE", "TMUX"):
        monkeypatch.delenv(name, raising=False)
    cpath = claude / "projects" / "-wt-1" / (SID_A + ".jsonl")
    cpath.parent.mkdir(parents=True)
    cpath.write_text(json.dumps({"type": "user", "cwd": "/wt/1",
                                 "message": {"content": "Fix cache\nnow"}}) + "\n")
    db = sqlite3.connect(codex / "state_5.sqlite")
    db.execute("create table threads (id,cwd,source,name,title,first_user_message,archived)")
    db.executemany("insert into threads values (?,?,?,?,?,?,?)", [
        (SID_B, "/wt/2", "cli", "Codex task", "", "", 1),
        (SID_A, "/wt/2", '{"subagent":{}}', "Ignore helper", "", "", 0),
    ])
    db.commit()
    db.close()
    cursor = home / ".cursor" / "chats" / "hash" / SID_C
    cursor.mkdir(parents=True)
    (cursor / "meta.json").write_text(json.dumps({"cwd": "/wt/3", "title": "Cursor task"}))
    return runpy.run_path(str(Path(__file__).parents[1] / "libexec/t_share_sessions.py")), tmp_path


def test_targets_and_exact_resolve(sessions):
    module, _ = sessions
    rows = module["targets"]()
    assert [r["address"] for r in rows] == ["claude:" + SID_A, "codex:" + SID_B, "cursor:" + SID_C]
    assert [r["title"] for r in rows] == ["Fix cache now", "Codex task", "Cursor task"]
    assert module["resolve"]("codex:" + SID_B)["cwd"] == "/wt/2"
    with pytest.raises(ValueError, match="does not exist"):
        module["resolve"]("codex:" + SID_A)
    with pytest.raises(ValueError, match="does not exist"):
        module["resolve"]("codex:" + SID_B[:8])


def test_claude_self_requires_saved_exact_session(sessions, monkeypatch):
    module, _ = sessions
    monkeypatch.setitem(module["self_address"].__globals__, "_ancestors", lambda: iter(()))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID_A)
    assert module["self_address"]() == "claude:" + SID_A
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID_B)
    with pytest.raises(ValueError, match="--from"):
        module["self_address"]()


def test_codex_registry_requires_matching_ancestor(sessions, monkeypatch):
    module, root = sessions
    registry = root / "cache" / "claude-sessions"
    registry.mkdir(parents=True)
    (registry / "42").write_text(SID_B + "\t/wt/2")
    start = datetime.datetime.fromtimestamp((registry / "42").stat().st_mtime - 10).strftime("%a %b %d %H:%M:%S %Y")
    monkeypatch.setitem(module["self_address"].__globals__, "_ancestors", lambda: iter([(42, {"args": "/opt/bin/codex", "start": start})]))
    assert module["self_address"]() == "codex:" + SID_B
    monkeypatch.setitem(module["self_address"].__globals__, "_ancestors", lambda: iter([(43, {"args": "/opt/bin/codex", "start": start})]))
    with pytest.raises(ValueError, match="--for"):
        module["self_address"]()


def test_cursor_hook_requires_ancestry_pane_and_incarnation(sessions, monkeypatch):
    module, root = sessions
    socket = "/tmp/test-socket"
    monkeypatch.setenv("TMUX", socket + ",1,0")
    monkeypatch.setenv("TMUX_PANE", "%9")
    import hashlib
    path = root / "cache" / "t" / "recovery" / hashlib.sha256(socket.encode()).hexdigest()[:20]
    path.mkdir(parents=True)
    (path / "pane-9.json").write_text(json.dumps({"pid": "42", "start": "date", "sid": SID_C, "cwd": "/wt/3"}))
    ancestry = [(42, {"args": "/opt/cursor-agent", "start": "date"}),
                (30, {"args": "zsh", "start": "date"})]
    monkeypatch.setitem(module["self_address"].__globals__, "_ancestors", lambda: iter(ancestry))

    class Result:
        stdout = "%9\t30\tt-cursor-" + SID_C + "\t/wt/3\t0\n"

    monkeypatch.setattr(module["subprocess"], "run", lambda *a, **k: Result())
    assert module["self_address"]() == "cursor:" + SID_C
    ancestry[0][1]["start"] = "reused pid"
    with pytest.raises(ValueError, match="--from"):
        module["self_address"]()


def test_malformed_provider_records_do_not_create_targets(sessions):
    module, root = sessions
    bogus = root / "home" / ".cursor" / "chats" / "hash" / "unknown"
    bogus.mkdir()
    (bogus / "meta.json").write_text('{"cwd":"relative", "title":"ignored"}')
    assert len(module["targets"]()) == 3


def test_copied_codex_rollout_is_discoverable_before_indexing(sessions):
    module, root = sessions
    path = root / "other-codex" / "sessions" / "2026" / "10" / "11" / ("rollout-now-" + SID_C + ".jsonl")
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": SID_C, "cwd": "/wt/4"}}) + "\n")
    assert module["resolve"]("codex:" + SID_C)["cwd"] == "/wt/4"
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": SID_A, "cwd": "/wt/4"}}) + "\n")
    with pytest.raises(ValueError, match="does not exist"):
        module["resolve"]("codex:" + SID_C)


def test_untrusted_provider_metadata_is_ignored(sessions):
    module, root = sessions
    claude = root / "other-claude" / "projects" / "-wt-1"
    (claude / "not-an-id.jsonl").write_text("{}\n")
    (claude / (SID_C + ".jsonl")).write_text("bad json\n[]\n" + json.dumps({
        "type": "user", "cwd": "/wt/5", "message": {"content": [
            {"type": "text", "text": "List\nitem"}]}}) + "\n")
    assert module["resolve"]("claude:" + SID_C)["title"] == "List item"
    cursor = root / "home" / ".cursor" / "chats" / "hash" / "opaque-id"
    cursor.mkdir()
    (cursor / "meta.json").write_text("bad json")
    assert len(module["targets"]()) == 4
    assert module["_row"]("codex", None, "/wt/2", "") is None
    with pytest.raises(ValueError, match="address"):
        module["resolve"](None)
    for address in ["none:" + SID_A, "claude:../../abc", "claude:"]:
        with pytest.raises(ValueError, match="address"):
            module["resolve"](address)


def test_missing_provider_stores_and_partial_codex_schema(sessions):
    module, root = sessions
    import shutil
    shutil.rmtree(root / "other-claude" / "projects")
    shutil.rmtree(root / "home" / ".cursor" / "chats")
    (root / "other-codex" / "state_5.sqlite").unlink()
    assert module["targets"]() == []
    db = sqlite3.connect(root / "other-codex" / "state_5.sqlite")
    db.execute("create table threads (id,cwd,source)")
    db.execute("insert into threads values (?,?,?)", (SID_B, "/wt/2", "cli"))
    db.commit(); db.close()
    assert module["targets"]()[0]["title"] == "(untitled)"
    (root / "other-codex" / "state_5.sqlite").write_bytes(b"not sqlite")
    assert module["targets"]() == []


def test_process_and_registry_failure_modes(sessions, monkeypatch):
    module, root = sessions
    import subprocess
    globals_ = module["self_address"].__globals__
    assert module["_process"]("bad") is None
    monkeypatch.setattr(module["subprocess"], "run", lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("ps", 2)))
    assert module["_process"](42) is None
    monkeypatch.setattr(module["subprocess"], "run", lambda *a, **k: type("R", (), {"stdout": "bad"})())
    assert module["_process"](42) is None
    assert module["_is_agent_process"]({"args": ""}, "codex") is False
    assert module["_is_agent_process"]({"args": "/App.app/Contents/MacOS/cursor-agent"}, "cursor") is False
    registry = root / "cache" / "claude-sessions"
    registry.mkdir(parents=True)
    now = datetime.datetime.now().strftime("%a %b %d %H:%M:%S %Y")
    assert module["_registry_identity"](42, {"start": now}, registry) is None
    (registry / "42").write_text(SID_B + "\t/wrong")
    assert module["_registry_identity"](42, {"start": now}, registry) is None
    (registry / "42").write_text(SID_A + "\t/wt/2")
    assert module["_registry_identity"](42, {"start": now}, registry) is None
    monkeypatch.setitem(globals_, "_ancestors", lambda: iter([(42, {"args": "/opt/bin/codex", "start": now})]))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID_A)
    with pytest.raises(ValueError, match="--from"):
        module["self_address"]()


def test_cursor_mismatch_cannot_claim_chat(sessions, monkeypatch):
    module, root = sessions
    import hashlib
    socket = "/tmp/test-socket"
    monkeypatch.setenv("TMUX", socket + ",1,0")
    monkeypatch.setenv("TMUX_PANE", "%9")
    path = root / "cache" / "t" / "recovery" / hashlib.sha256(socket.encode()).hexdigest()[:20]
    path.mkdir(parents=True)
    record = path / "pane-9.json"
    record.write_text(json.dumps({"pid": "42", "start": "date", "sid": SID_C, "cwd": "/wt/3"}))
    ancestry = [(42, {"args": "/opt/cursor-agent", "start": "date"})]
    monkeypatch.setitem(module["self_address"].__globals__, "_ancestors", lambda: iter(ancestry))

    class Result:
        stdout = "%9\t30\tt-cursor-" + SID_C + "\t/wt/3\t0\n"

    monkeypatch.setattr(module["subprocess"], "run", lambda *a, **k: Result())
    with pytest.raises(ValueError, match="--from"):
        module["self_address"]()  # pane PID is not an ancestor
    ancestry.append((30, {"args": "zsh", "start": "date"}))
    # Cursor /new changes the hook ID but the tmux session retains its name.
    Result.stdout = "%9\t30\tt-cursor-" + SID_A + "\t/wt/3\t0\n"
    assert module["self_address"]() == "cursor:" + SID_C
    Result.stdout = "%9\t30\tdev-other\t/wt/3\t0\n"
    with pytest.raises(ValueError, match="--from"):
        module["self_address"]()


def test_codex_rollout_rejects_unverified_metadata(sessions):
    module, root = sessions
    day = root / "other-codex" / "sessions" / "2026" / "10" / "11"
    day.mkdir(parents=True)
    (day / ("rollout-a-" + SID_A + ".jsonl")).write_text("not-json\n")
    (day / ("rollout-b-" + SID_C + ".jsonl")).write_text(json.dumps({"type": "event", "payload": {}}) + "\n")
    (day / "rollout-invalid.jsonl").write_text("{}\n")
    assert len(module["targets"]()) == 3


def test_process_walk_and_registry_incarnation(sessions, monkeypatch):
    module, root = sessions
    globals_ = module["_ancestors"].__globals__
    monkeypatch.setattr(globals_["os"], "getppid", lambda: 7)
    monkeypatch.setitem(globals_, "_process", lambda pid: {"ppid": 6, "start": "today", "args": "sh"} if pid == 7 else
                        {"ppid": 1, "start": "today", "args": "init"})
    assert [pid for pid, _ in module["_ancestors"]()] == [7, 6]
    monkeypatch.setitem(globals_, "_process", lambda pid: None)
    assert list(module["_ancestors"]()) == []
    monkeypatch.setattr(module["subprocess"], "run", lambda *a, **k: type("R", (), {"stdout": "7 Thu Oct 11 12:00:00 2026 /bin/codex"})())
    assert module["_process"](42)["ppid"] == 7
    registry = root / "cache" / "claude-sessions"
    registry.mkdir(parents=True)
    (registry / "42").write_text(SID_B + "\t/wt/2")
    future = "Thu Oct 11 12:00:00 2099"
    assert module["_registry_identity"](42, {"start": future}, registry) is None


def test_cursor_read_failures_and_stale_source(sessions, monkeypatch):
    module, root = sessions
    import hashlib
    socket = "/tmp/test-socket"
    monkeypatch.setenv("TMUX", socket + ",1,0")
    monkeypatch.setenv("TMUX_PANE", "%9")
    path = root / "cache" / "t" / "recovery" / hashlib.sha256(socket.encode()).hexdigest()[:20]
    path.mkdir(parents=True)
    record = path / "pane-9.json"
    ancestry = [(42, {"args": "/opt/cursor-agent", "start": "date"}),
                (30, {"args": "zsh", "start": "date"})]
    assert module["_cursor_identity"](ancestry) is None  # missing hook
    record.write_text(json.dumps({"pid": "42", "start": "date", "sid": SID_C, "cwd": "/wt/3"}))
    import subprocess
    monkeypatch.setattr(module["subprocess"], "run", lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired("tmux", 2)))
    assert module["_cursor_identity"](ancestry) is None
    monkeypatch.setattr(module["subprocess"], "run", lambda *a, **k: type("R", (), {
        "stdout": "%9\t30\tt-cursor-" + SID_C + "\t/wt/3\t0\n"})())
    (root / "home" / ".cursor" / "chats" / "hash" / SID_C / "meta.json").unlink()
    monkeypatch.setitem(module["self_address"].__globals__, "_ancestors", lambda: iter(ancestry))
    with pytest.raises(ValueError, match="--from"):
        module["self_address"]()


def test_pretty_cursor_metadata_and_node_launcher(sessions, monkeypatch):
    module, root = sessions
    cursor = root / "home" / ".cursor" / "chats" / "hash" / SID_C / "meta.json"
    cursor.write_text(json.dumps({"cwd": "/wt/3", "title": "Pretty Cursor"}, indent=2))
    assert module["resolve"]("cursor:" + SID_C)["title"] == "Pretty Cursor"
    launcher = {"args": "/usr/bin/node /opt/cursor-agent/versions/2026/index.js"}
    assert module["_is_agent_process"](launcher, "cursor")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID_A)
    monkeypatch.setitem(module["self_address"].__globals__, "_ancestors", lambda: iter([(42, launcher)]))
    with pytest.raises(ValueError, match="--from"):
        module["self_address"]()  # inherited Claude ID cannot claim a Cursor tool


def test_legacy_cursor_chat_without_meta_is_addressable(sessions):
    module, root = sessions
    path = root / "home" / ".cursor" / "chats" / "hash" / "legacy-chat"
    path.mkdir()
    (path / "store.db").write_bytes(b"saved conversation")
    row = module["resolve"]("cursor:legacy-chat")
    assert row["cwd"] == ""
    assert row["title"] == "(untitled)"


def test_malformed_codex_id_does_not_break_discovery(sessions):
    module, root = sessions
    db = sqlite3.connect(root / "other-codex" / "state_5.sqlite")
    db.execute("insert into threads values (?,?,?,?,?,?,?)", (None, "/wt/2", "cli", "Broken", "", "", 0))
    db.commit(); db.close()
    assert len(module["targets"]()) == 3


def test_codex_app_server_never_claims_shared_pid(sessions, monkeypatch):
    module, root = sessions
    registry = root / "cache" / "claude-sessions"
    registry.mkdir(parents=True)
    (registry / "42").write_text(SID_B + "\t/wt/2")
    daemon = {"args": "/opt/bin/codex app-server", "start": "Thu Oct 11 12:00:00 2026"}
    assert not module["_is_agent_process"](daemon, "codex")
    monkeypatch.setitem(module["self_address"].__globals__, "_ancestors", lambda: iter([(42, daemon)]))
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", SID_A)
    with pytest.raises(ValueError, match="--from"):
        module["self_address"]()
