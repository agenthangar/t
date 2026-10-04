"""Recovery offers are offline, scoped, opt-in, and bound to the current owner."""

import io
import json
import os
from pathlib import Path
import shlex
import subprocess

import pytest

from conftest import load_script, REPO_ROOT

SID = "01234567-89ab-cdef-0123-456789abcdef"
ERROR = "Server connection could not be restored"
INVALID_CWD = ("Failed to start turn: turn/start failed in TUI: turn/start failed: "
               "invalid cwd: No such file or directory (os error 2) (code -32600)")


def result(stdout="", rc=0, stderr=""):
    return subprocess.CompletedProcess([], rc, stdout, stderr)


@pytest.fixture
def recovery(tmp_path, monkeypatch):
    mod = load_script(REPO_ROOT / "libexec/t_recovery.py", "t_recovery")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    monkeypatch.setattr(mod, "run", lambda a: result(rc=1))
    monkeypatch.setattr(mod, "tmux", lambda *a: result(rc=1))
    return mod


@pytest.fixture
def target():
    return dict(socket="/tmp/test-socket", session="dev-api-1", pane="%4", pane_pid="40",
                cwd="/tmp/project", pid="42", start="Sat Oct 3 12:00:00 2026", agent="codex", sid=SID)


@pytest.mark.parametrize("agent,screen,expected", [
    ("codex", "■ Server connection could not be restored", True),
    ("codex", "■ Automatic reconnect could not restore this session. Copy your draft.", True),
    ("codex", "• Reconnecting... 1/5", False),
    ("codex", '> "Server connection could not be restored"', False),
    ("codex", "■ " + INVALID_CWD, True),
    ("codex", "■ " + INVALID_CWD + " and another error", False),
    ("codex", "> " + INVALID_CWD, False),
    ("codex", "```text\n" + INVALID_CWD + "\n```", False),
    ("codex", INVALID_CWD.replace("invalid cwd", "invalid request"), False),
    ("claude", "⎿ API Error: Connection error.", True),
    ("claude", "API Error: Request timed out", True),
    ("claude", "API Error: 429 rate limited", False),
    ("claude", "API Error: 401 unauthorized", False),
    ("cursor", "Connection failed", True),
    ("cursor", "Connection failed repeatedly", True),
    ("cursor", "Discuss how Connection failed appears", False),
    ("unknown", "Connection failed", False),
    ("cursor", "```text\nConnection failed\n```", False),
])
def test_failure_recognizes_only_final_error_lines(recovery, agent, screen, expected):
    assert bool(recovery.failure(agent, screen)) == expected


def test_invalid_cwd_failure_has_stable_discriminator(recovery):
    assert recovery.failure("codex", "■ " + INVALID_CWD) == recovery.INVALID_CWD


def test_private_state_and_invalid_json(recovery, monkeypatch):
    directory = recovery.cache("/tmp/test")
    assert directory.stat().st_mode & 0o777 == 0o700
    path = directory / "state"
    assert recovery.read_json(path) == {}
    recovery.write_json(path, {"sid": SID})
    assert recovery.read_json(path) == {"sid": SID}
    assert path.stat().st_mode & 0o777 == 0o600
    for data in ("[]", "broken"):
        path.write_text(data)
        assert recovery.read_json(path) == {}
    monkeypatch.setattr(recovery.os, "replace", lambda *a: (_ for _ in ()).throw(OSError("failed")))
    with pytest.raises(OSError):
        recovery.write_json(path, {})
    assert list(directory.iterdir()) == [path]


def test_run_errors_and_normal(recovery, monkeypatch):
    # Restore the real helper's subprocess boundary without launching anything.
    run = load_script(REPO_ROOT / "libexec/t_recovery.py", "t_recovery_run").run
    for error in (OSError("missing"), subprocess.TimeoutExpired("ps", 15)):
        monkeypatch.setattr(subprocess, "run", lambda *a, **kw: (_ for _ in ()).throw(error))
        assert run(["missing"]).returncode == 1
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: result("ok"))
    assert run(["ok"]).stdout == "ok"


def test_panes_and_clients(recovery, monkeypatch):
    monkeypatch.setattr(recovery, "tmux", lambda *a: result(
        "dev-api-1\t%1\t11\t/tmp/api\nother\t%2\t12\t/tmp/no\n"
        "dev-split-1\t%3\t13\t/tmp/split\ndev-split-1\t%4\t14\t/tmp/split\n"
        "t-cursor-chat\t%5\t15\t/tmp/cursor\nbad\n"))
    assert [p["pane"] for p in recovery.panes("socket")] == ["%1", "%5"]
    monkeypatch.setattr(recovery, "tmux", lambda *a: result("client1\t%1\nclient2\t%2\nbad\n"))
    assert recovery.clients("socket", "%2") == ["client2"]


def test_process_parsing_and_ancestry(recovery, monkeypatch):
    assert recovery.process("not a pid") == {}
    for response in (result(rc=1), result("bad"), result("x Sat Oct 3 12:00:00 2026 a")):
        monkeypatch.setattr(recovery, "run", lambda a: response)
        assert recovery.process(42) == {}
    monkeypatch.setattr(recovery, "run", lambda a: result("40 Sat Oct 3 12:00:00 2026 /bin/codex resume thread"))
    assert recovery.process(42)["args"] == "/bin/codex resume thread"
    assert recovery.descendant(42, 40)
    monkeypatch.setattr(recovery, "process", lambda p: {"ppid": "1"})
    assert not recovery.descendant(42, 40)
    monkeypatch.setattr(recovery, "process", lambda p: {})
    assert not recovery.descendant(42, 40)
    monkeypatch.setattr(recovery, "process", lambda p: {"ppid": str(p)})
    assert not recovery.descendant(42, 40)


@pytest.mark.parametrize("args,expected", [
    ("/home/me/.local/bin/cursor-agent --resume=x", True),
    ("node /home/me/.local/share/cursor-agent/versions/v/index.js", True),
    ("/Applications/Cursor.app/Contents/cursor-agent", False),
    ("node unrelated.js", False),
])
def test_cursor_process(recovery, args, expected):
    assert recovery.cursor_process({"args": args}) == expected


def test_owner_and_revalidation(recovery, target, monkeypatch):
    pane = {k: target[k] for k in ("session", "pane", "pane_pid", "cwd")}
    monkeypatch.setattr(recovery, "run", lambda a: result(f"codex\t{SID}\t42\n"))
    monkeypatch.setattr(recovery, "process", lambda p: {"start": target["start"]})
    assert recovery.owner(target["socket"], pane) == target
    monkeypatch.setattr(recovery, "panes", lambda s: [pane])
    assert recovery.current(target)
    assert not recovery.current(dict(target, sid="changed"))
    monkeypatch.setattr(recovery, "panes", lambda s: [])
    assert not recovery.current(target)
    for output in ("bad", "claude\tinvalid\t42", f"other\t{SID}\t42"):
        monkeypatch.setattr(recovery, "run", lambda a: result(output))
        assert recovery.owner(target["socket"], pane) == {}
    monkeypatch.setattr(recovery, "run", lambda a: result(f"claude\t{SID}\t42\n"))
    monkeypatch.setattr(recovery, "process", lambda p: {})
    assert recovery.owner(target["socket"], pane) == {}


def test_cursor_owner_requires_current_hook_and_process(recovery, target, monkeypatch):
    target.update(session="t-cursor-" + SID, agent="cursor")
    pane = {k: target[k] for k in ("session", "pane", "pane_pid", "cwd")}
    monkeypatch.setattr(recovery, "process", lambda p: {"start": target["start"], "args": "cursor-agent --resume=" + SID})
    monkeypatch.setattr(recovery, "descendant", lambda *a: True)
    assert recovery.owner(target["socket"], pane) == {}
    recovery.write_json(recovery.cache(target["socket"]) / "pane-4.json", target)
    assert recovery.owner(target["socket"], pane) == target
    monkeypatch.setattr(recovery, "descendant", lambda *a: False)
    assert recovery.owner(target["socket"], pane) == {}


def test_offer_never_types_into_agent_and_targets_viewer(recovery, target, monkeypatch):
    events = []
    monkeypatch.setattr(recovery, "clients", lambda *a: [])
    assert not recovery.offer(target, ERROR)
    monkeypatch.setattr(recovery, "clients", lambda *a: ["/dev/pts/4"])
    monkeypatch.setattr(recovery, "tmux", lambda *a: events.append(a) or result())
    assert recovery.offer(target, ERROR)
    assert events[0][1:3] == ("run-shell", "-b")
    menu = shlex.split(events[0][3])
    assert menu[3:8] == ["display-menu", "-c", "/dev/pts/4", "-t", "%4"]
    assert "Save visible draft and restart" in menu
    assert not any("send-keys" in e for e in events)
    offers = list(recovery.cache(target["socket"]).glob("*.offer"))
    assert len(offers) == 1 and recovery.read_json(offers[0])["sid"] == SID
    offers[0].unlink()
    monkeypatch.setattr(recovery, "tmux", lambda *a: result(rc=1))
    assert not recovery.offer(target, ERROR)
    assert not list(recovery.cache(target["socket"]).glob("*.offer"))


def test_invalid_cwd_offer_and_exact_restart_mode(recovery, target, monkeypatch):
    monkeypatch.setattr(recovery, "panes", lambda s: [target])
    monkeypatch.setattr(recovery, "owner", lambda *a: target)
    monkeypatch.setattr(recovery, "clients", lambda *a: ["/dev/pts/4"])
    calls = []
    monkeypatch.setattr(recovery, "tmux", lambda *a: calls.append(a) or result(
        "DEV_AGENT=codex" if a[1] == "show-environment" else "■ " + INVALID_CWD))
    watcher = recovery.Watcher(target["socket"])
    watcher.poll(); watcher.poll()
    menu_call = next(a for a in calls if a[1] == "run-shell")
    menu = shlex.split(menu_call[3])
    assert "t: workspace unavailable — recover this conversation?" in menu
    assert "Save visible draft and restart here" in menu
    offer = next(recovery.cache(target["socket"]).glob("*.offer"))
    monkeypatch.setattr(recovery, "current", lambda t: True)
    launched = []
    def execute(argv, **kw):
        launched.append((argv, kw))
        return result("Saved draft")
    monkeypatch.setattr(recovery.subprocess, "run", execute)
    assert recovery.respond(target["socket"], offer.stem, True) == 0
    assert shlex.split(launched[0][0][-1].split("; ", 1)[1]) == [
        "_t_restart_slot", target["session"], target["cwd"], SID, "codex",
        "restart-invalid-cwd", target["pid"]]
    assert launched[0][1]["cwd"] == "/"


def test_watcher_waits_for_stable_error_and_offers_once(recovery, target, monkeypatch):
    monkeypatch.setattr(recovery, "panes", lambda s: [target])
    screen = ["normal"]
    monkeypatch.setattr(recovery, "tmux", lambda s, cmd, *a: result("DEV_AGENT=codex" if cmd == "show-environment" else screen[0]))
    monkeypatch.setattr(recovery, "owner", lambda *a: target)
    offered = []
    monkeypatch.setattr(recovery, "offer", lambda *a: offered.append(a) or True)
    watcher = recovery.Watcher(target["socket"])
    watcher.poll()
    screen[0] = ERROR
    watcher.poll()
    assert offered == []
    watcher.poll(); watcher.poll()
    assert len(offered) == 1
    screen[0] = "normal"
    watcher.poll()
    screen[0] = ERROR
    watcher.poll(); watcher.poll()
    assert len(offered) == 2
    monkeypatch.setattr(recovery, "panes", lambda s: [])
    assert not watcher.poll() and watcher.seen == {}


def test_watcher_cursor_and_missing_owner(recovery, target, monkeypatch):
    target.update(session="t-cursor-" + SID, agent="cursor")
    monkeypatch.setattr(recovery, "panes", lambda s: [target])
    monkeypatch.setattr(recovery, "tmux", lambda *a: result("Connection failed"))
    monkeypatch.setattr(recovery, "owner", lambda *a: {})
    watcher = recovery.Watcher(target["socket"])
    watcher.poll(); watcher.poll()
    assert watcher.seen[target["pane"]] == ("Connection failed", False)


def test_start_and_singleton(recovery, target, monkeypatch):
    assert recovery.start("dev-api-1") == 0
    monkeypatch.delenv("T_RECOVERY_DISABLE")
    assert recovery.start("dev-api-1") == 1
    monkeypatch.setattr(recovery, "run", lambda a: result(target["socket"]))
    calls = []
    monkeypatch.setenv("COV_CORE_SOURCE", "never propagate")
    monkeypatch.setattr(recovery.subprocess, "Popen", lambda *a, **kw: calls.append((a, kw)))
    assert recovery.start("dev-api-1") == 0
    assert calls[0][1]["start_new_session"]
    assert "COV_CORE_SOURCE" not in calls[0][1]["env"]
    polls = iter([True, False])
    monkeypatch.setattr(recovery.Watcher, "poll", lambda self: next(polls))
    monkeypatch.setattr(recovery.time, "sleep", lambda t: None)
    assert recovery.watch(target["socket"]) == 0
    with (recovery.cache(target["socket"]) / "watch.lock").open("w") as lock:
        recovery.fcntl.flock(lock, recovery.fcntl.LOCK_EX)
        assert recovery.watch(target["socket"]) == 0


def test_watcher_reexecs_after_source_update(recovery, target, monkeypatch):
    versions = iter([(1,), None, (1,), (2,), (2,)])
    monkeypatch.setattr(recovery, "source_version", lambda: next(versions))
    monkeypatch.setattr(recovery.Watcher, "poll", lambda self: True)
    monkeypatch.setattr(recovery.time, "sleep", lambda t: None)
    calls = []
    def reexec(binary, argv):
        calls.append((binary, argv))
        raise SystemExit(0)
    monkeypatch.setattr(recovery.os, "execv", reexec)
    with pytest.raises(SystemExit):
        recovery.watch(target["socket"])
    assert calls == [(recovery.sys.executable,
                      [recovery.sys.executable, str(Path(recovery.__file__)), "watch", target["socket"]])]
    with (recovery.cache(target["socket"]) / "watch.lock").open("w") as lock:
        recovery.fcntl.flock(lock, recovery.fcntl.LOCK_EX | recovery.fcntl.LOCK_NB)


def test_source_version_ignores_missing_and_empty_file(recovery, tmp_path, monkeypatch):
    source = tmp_path / "temporarily-unavailable.py"
    monkeypatch.setattr(recovery, "__file__", str(source))
    assert recovery.source_version() is None
    source.touch()
    assert recovery.source_version() is None
    source.write_text("pass\n")
    assert recovery.source_version()


def make_offer(recovery, target):
    token = "a" * 32
    recovery.write_json(recovery.cache(target["socket"]) / (token + ".offer"),
                        {**target, "created": recovery.time.time(), "error": ERROR})
    return token


@pytest.mark.parametrize("scenario", ["accept", "dismiss", "changed", "cleared", "expired", "failure", "timeout", "cursor"])
def test_menu_response_is_single_use_and_revalidates(recovery, target, monkeypatch, scenario):
    if scenario == "cursor":
        target["agent"] = "cursor"
    token = make_offer(recovery, target)
    monkeypatch.setattr(recovery, "current", lambda t: scenario != "changed")
    monkeypatch.setattr(recovery, "failure", lambda *a: "" if scenario == "cleared" else ERROR)
    events = []
    monkeypatch.setattr(recovery, "tmux", lambda *a: events.append(a) or result(ERROR))
    if scenario == "expired":
        now = recovery.time.time()
        monkeypatch.setattr(recovery.time, "time", lambda: now + 301)
    launches = []
    def execute(*a, **kw):
        launches.append(a)
        assert kw["env"]["TMUX"].startswith(target["socket"])
        if scenario == "timeout":
            raise subprocess.TimeoutExpired("restart", 30)
        return result("Saved draft", 1 if scenario == "failure" else 0, "refused")
    monkeypatch.setattr(recovery.subprocess, "run", execute)
    monkeypatch.setattr(recovery, "cursor_restart", lambda t: launches.append(t) or "/private/draft")
    rc = recovery.respond(target["socket"], token, scenario != "dismiss")
    assert (rc == 0) == (scenario in ("accept", "dismiss", "cursor"))
    assert bool(launches) == (scenario in ("accept", "failure", "timeout", "cursor"))
    assert recovery.respond(target["socket"], token, True) == 1
    assert recovery.respond(target["socket"], "../other", True) == 1


def test_cursor_hook_tracks_exact_live_owner(recovery, target, monkeypatch):
    target.update(session="t-cursor-" + SID, agent="cursor")
    monkeypatch.setenv("TMUX_PANE", "%4")
    monkeypatch.setattr(recovery, "run", lambda a: result(target["socket"]))
    monkeypatch.setattr(recovery, "panes", lambda s: [target])
    monkeypatch.setattr(recovery.os, "getppid", lambda: 43)
    monkeypatch.setattr(recovery, "process", lambda p: {"ppid": "42", "start": target["start"], "args": "hook" if p == 43 else "cursor-agent"})
    monkeypatch.setattr(recovery, "descendant", lambda *a: True)
    started = []
    monkeypatch.setattr(recovery, "start", lambda s: started.append(s) or 0)
    assert recovery.cursor_hook({"conversation_id": SID}) == 0
    assert started == [target["session"]]
    assert recovery.read_json(recovery.cache(target["socket"]) / "pane-4.json")["pid"] == "42"
    assert recovery.cursor_hook({"conversation_id": "bad"}) == 0
    monkeypatch.setattr(recovery, "process", lambda p: {})
    assert recovery.cursor_hook({"session_id": SID}) == 0
    monkeypatch.setattr(recovery, "process", lambda p: {"ppid": "1", "args": "hook"})
    assert recovery.cursor_hook({"session_id": SID}) == 0
    monkeypatch.setattr(recovery, "panes", lambda s: [])
    assert recovery.cursor_hook({"session_id": SID}) == 0
    monkeypatch.setattr(recovery, "run", lambda a: result(rc=1))
    assert recovery.cursor_hook({"session_id": SID}) == 0


def test_cursor_hook_loop_bound(recovery, target, monkeypatch):
    monkeypatch.setenv("TMUX_PANE", "%4")
    monkeypatch.setattr(recovery, "run", lambda a: result(target["socket"]))
    monkeypatch.setattr(recovery, "panes", lambda s: [dict(target, session="t-cursor-" + SID)])
    monkeypatch.setattr(recovery, "process", lambda p: {"ppid": "42", "args": "unrelated"})
    assert recovery.cursor_hook({"session_id": SID}) == 0


def test_cursor_hook_install_preserves_user_hooks(recovery):
    path = Path.home() / ".cursor/hooks.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"version": 1, "hooks": {"stop": [{"command": "my-hook"}]}}))
    recovery.install_cursor_hook()
    data = json.loads(path.read_text())
    assert data["hooks"]["stop"] == [{"command": "my-hook"}]
    assert len(data["hooks"]["sessionStart"]) == 1
    recovery.install_cursor_hook()
    assert json.loads(path.read_text()) == data
    path.unlink()
    recovery.install_cursor_hook()
    for data in ([], {"version": 2}, {"hooks": []}, {"hooks": {"sessionStart": {}}}):
        path.write_text(json.dumps(data))
        with pytest.raises(ValueError):
            recovery.install_cursor_hook()


@pytest.mark.parametrize("scenario", ["new", "existing", "changed", "duplicate", "unverified", "ps_failed", "launch_failed", "no_binary", "bad_sid", "bad_dir"])
def test_cursor_resume_has_one_owner(recovery, target, tmp_path, monkeypatch, scenario):
    monkeypatch.setattr(recovery.shutil, "which", lambda n: None if scenario == "no_binary" else "/fake/cursor-agent")
    monkeypatch.setattr(recovery, "install_cursor_hook", lambda: None)
    def command(argv):
        if "has-session" in argv:
            return result(rc=0 if scenario in ("existing", "changed") else 1)
        if "display-message" in argv:
            return result(target["socket"])
        if argv[0] == "ps":
            if scenario == "ps_failed":
                return result(rc=1)
            if scenario == "unverified":
                return result("42 cursor-agent --resume=an-older-chat")
            return result("42 cursor-agent --resume=" + SID if scenario == "duplicate" else "")
        return result(rc=1 if scenario == "launch_failed" else 0, stderr="failed")
    monkeypatch.setattr(recovery, "run", command)
    monkeypatch.setattr(recovery, "panes", lambda s: [dict(target, session="t-cursor-" + SID)] if scenario in ("existing", "changed") else [])
    monkeypatch.setattr(recovery, "owner", lambda *a: dict(target, session="t-cursor-" + SID, sid="changed" if scenario == "changed" else SID, cwd=str(tmp_path)))
    monkeypatch.setattr(recovery, "start", lambda s: 0)
    monkeypatch.setattr(recovery.os, "execvp", lambda *a: (_ for _ in ()).throw(SystemExit(0)))
    sid = "bad" if scenario == "bad_sid" else SID
    cwd = "/missing" if scenario == "bad_dir" else str(tmp_path)
    with pytest.raises(SystemExit if scenario in ("new", "existing") else ValueError):
        recovery.cursor_resume(sid, cwd)


@pytest.mark.parametrize("scenario", ["normal", "changed", "self", "capture", "option", "reserve", "launch", "alive"])
def test_cursor_restart_preserves_draft_and_waits(recovery, target, monkeypatch, scenario):
    target["agent"] = "cursor"
    monkeypatch.setattr(recovery, "current", lambda t: scenario != "changed")
    monkeypatch.setattr(recovery, "descendant", lambda *a: scenario == "self")
    monkeypatch.setattr(recovery, "process", lambda p: {"start": "alive"} if scenario == "alive" else {})
    monkeypatch.setattr(recovery.time, "sleep", lambda t: None)
    calls, signals = [], []
    def command(socket, cmd, *args):
        calls.append(cmd)
        if cmd == "capture-pane":
            return result("unsent draft", 1 if scenario == "capture" else 0)
        if cmd == "show-options":
            return result("off", 1 if scenario == "option" else 0)
        if cmd == "set-option":
            return result(rc=1 if scenario == "reserve" else 0)
        if cmd == "respawn-pane":
            assert "-k" not in args
            return result(rc=1 if scenario == "launch" else 0, stderr="launch failed")
        return result("1")
    monkeypatch.setattr(recovery, "tmux", command)
    monkeypatch.setattr(recovery.os, "kill", lambda *a: signals.append(a))
    if scenario == "normal":
        saved = Path(recovery.cursor_restart(target))
        assert saved.read_text() == "unsent draft"
        assert saved.stat().st_mode & 0o777 == 0o600
    else:
        with pytest.raises(ValueError):
            recovery.cursor_restart(target)
    assert bool(signals) == (scenario in ("normal", "launch", "alive"))
    assert ("respawn-pane" in calls) == (scenario in ("normal", "launch"))


def test_main_dispatch(recovery, monkeypatch):
    for action, name in (("start", "start"), ("watch", "watch"), ("cursor-resume", "cursor_resume")):
        monkeypatch.setattr(recovery, name, lambda *a: 0)
        assert recovery.main([action, "one"]) == 0
    monkeypatch.setattr(recovery, "respond", lambda *a, **kw: 0)
    assert recovery.main(["accept", "socket", "token"]) == 0
    assert recovery.main(["dismiss", "socket", "token"]) == 0
    monkeypatch.setattr(recovery, "cursor_hook", lambda d: 0)
    monkeypatch.setattr(recovery.sys, "stdin", io.StringIO("{}"))
    assert recovery.main(["cursor-hook"]) == 0
    assert recovery.main(["bad"]) == 1
    assert recovery.main([]) == 1


def test_cursor_restart_serializes_recovery(recovery, target):
    path = recovery.cache(target["socket"]) / "pane-4.restart.lock"
    with path.open("w") as lock:
        recovery.fcntl.flock(lock, recovery.fcntl.LOCK_EX)
        with pytest.raises(ValueError, match="already running"):
            recovery.cursor_restart(target)


def test_cursor_resume_refuses_unverified_owner_or_changed_workspace(recovery, target, monkeypatch):
    monkeypatch.setattr(recovery.shutil, "which", lambda n: "/fake/cursor-agent")
    monkeypatch.setattr(recovery, "install_cursor_hook", lambda: None)
    monkeypatch.setattr(recovery, "run", lambda a: result(target["socket"]))
    monkeypatch.setattr(recovery, "panes", lambda s: [dict(target, session="t-cursor-" + SID)])
    monkeypatch.setattr(recovery, "owner", lambda *a: {})
    with pytest.raises(ValueError, match="no verified"):
        recovery.cursor_resume(SID, str(Path.home()))
    monkeypatch.setattr(recovery, "owner", lambda *a: target)
    with pytest.raises(ValueError, match="another workspace"):
        recovery.cursor_resume(SID, str(Path.home()))


STARTUP = '''Cannot use the background server
Experimental feature request failed
1. Run without daemon this time
Restart with these settings (disabled) Restart cannot resolve this compatibility check.
> 2. Cancel
'''

UNAVAILABLE = '''Cannot use the background server
background server is not running
> 1. Run without daemon this time
2. Restart with these settings
'''


@pytest.mark.parametrize('args,expected', [
    ('/usr/bin/codex', True),
    ('codex --model gpt-6 -c model_reasoning_effort=high --enable fast_mode', True),
    ('codex resume ' + SID, False),
    ('codex fork --last', False),
    ('codex --remote unix://', False),
    ('codex "unterminated', False),
    ('codex --model', False),
    ('node codex.js', False),
])
def test_startup_fresh_launch_is_proven(recovery, args, expected):
    assert recovery.fresh_codex({'args': args}) == expected


def test_startup_detection_owner_and_callback(recovery, target, monkeypatch):
    assert recovery.failure('codex', STARTUP) == recovery.BACKGROUND
    assert recovery.failure('codex', UNAVAILABLE) == recovery.BACKGROUND
    assert recovery.failure('codex', UNAVAILABLE.replace('background server is not running',
                                                        'background server socket is stale or unreachable')) == recovery.BACKGROUND
    assert recovery.failure('codex', UNAVAILABLE.replace('Run without daemon this time', 'Restart now')) == ''
    assert recovery.failure('claude', STARTUP) == ''
    assert recovery.failure('codex', '```\n' + STARTUP + '```') == ''
    assert recovery.failure('codex', '> ' + STARTUP) == ''
    assert recovery.failure('codex', STARTUP.replace('Run without daemon this time', '')) == ''
    target.update(sid='', startup=True)
    pane = {k: target[k] for k in ('session', 'pane', 'pane_pid', 'cwd')}
    monkeypatch.setattr(recovery, 'run', lambda a: result('codex\t\t42'))
    monkeypatch.setattr(recovery, 'process', lambda p: {'start': target['start'], 'args': 'codex'})
    assert recovery.owner(target['socket'], pane) == {}
    assert recovery.owner(target['socket'], pane, startup=True) == target
    monkeypatch.setattr(recovery, 'panes', lambda s: [pane])
    assert recovery.current(target)
    monkeypatch.setattr(recovery, 'tmux', lambda *a: result('DEV_AGENT=codex' if a[1] == 'show-environment' else STARTUP))
    offered = []
    monkeypatch.setattr(recovery, 'offer', lambda t, e: offered.append(t) or True)
    watcher = recovery.Watcher(target['socket'])
    watcher.poll(); watcher.poll()
    assert offered == [target]
    token = make_offer(recovery, target)
    path = recovery.cache(target['socket']) / (token + '.offer')
    recovery.write_json(path, {**recovery.read_json(path), 'error': recovery.BACKGROUND})
    def execute(argv, **kw):
        assert shlex.split(argv[-1].split('; ', 1)[1]) == [
            '_t_restart_slot', target['session'], target['cwd'], '', 'codex', 'restart-no-daemon', '42']
        return result('Saved draft')
    monkeypatch.setattr(recovery.subprocess, 'run', execute)
    assert recovery.respond(target['socket'], token, True) == 0
    monkeypatch.setattr(recovery, 'process', lambda p: {'start': target['start'], 'args': 'codex resume ' + SID})
    assert recovery.owner(target['socket'], pane, startup=True) == {}
