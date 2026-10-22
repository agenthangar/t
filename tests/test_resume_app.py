"""Saved conversation selection must reach the guarded, exact desktop route."""

import json
import hashlib
import os
import shlex
import subprocess

import pytest

from test_agent_seam import zsh  # noqa: F401
from test_app import SID, app_command, app_slot  # noqa: F401


OTHER = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"


def saved_rollout(zsh, sid=SID, slot="3", age=0):
    cwd = zsh.home / "code/.worktrees/api" / slot
    cwd.mkdir(parents=True, exist_ok=True)
    sessions = zsh.home / ".codex/sessions/2026/10/13"
    sessions.mkdir(parents=True, exist_ok=True)
    rollout = sessions / f"rollout-2026-10-13T10-00-00-{sid}.jsonl"
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": sid, "cwd": str(cwd), "cli_version": "0.160.1"}}) + "\n" +
        json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user",
                   "content": [{"type": "input_text", "text": f"Task {sid}"}]}}) + "\n")
    if age:
        os.utime(rollout, (age, age))
    return cwd, rollout


def app_stub(tmp_path):
    stub = tmp_path / "stubbin/t"
    stub.write_text('#!/bin/sh\nprintf "APP %s\\n" "$*"\n')
    stub.chmod(0o755)


@pytest.mark.parametrize("verb", ["t resume", "t session resume"])
@pytest.mark.parametrize("slot", ["3", "api-3"])
def test_resume_app_exact_saved_route(zsh, tmp_path, verb, slot):
    cwd, rollout = saved_rollout(zsh, slot=slot)
    before = rollout.read_bytes()
    app_stub(tmp_path)
    result = zsh(f"OSTYPE=darwin; {verb} api 3 --app")
    assert result.returncode == 0, result.stderr
    assert f"APP _app-resume api 3 {SID} {cwd}" in result.stdout
    assert "send-keys" not in zsh.log.read_text()
    assert rollout.read_bytes() == before


def test_resume_app_picker_preserves_older_selection(zsh, tmp_path):
    cwd, old = saved_rollout(zsh, age=1)
    saved_rollout(zsh, OTHER)
    app_stub(tmp_path)
    picker = tmp_path / "stubbin/fzf"
    picker.write_text(f'#!/bin/sh\ncat | grep "{SID}"\n')
    picker.chmod(0o755)
    result = zsh("OSTYPE=darwin; _t_resume api 3 --days all --app", _tty=True)
    assert result.returncode == 0, result.stdout
    assert f"APP _app-resume api 3 {SID} {cwd}" in result.stdout
    assert f"APP _app-resume api 3 {OTHER}" not in result.stdout


@pytest.mark.parametrize("flags", ["--fg", "--cli", "--host mini", "--host=mini", "--host"])
def test_resume_app_incompatible_flags_fail_before_scan(zsh, flags):
    result = zsh(f"OSTYPE=darwin; _dev_rows_all() {{ echo UNEXPECTED; }}; _t_resume api --app {flags}")
    assert result.returncode == 1
    assert "cannot be combined" in result.stderr
    assert "UNEXPECTED" not in result.stdout


def test_resume_app_platform_and_agent_refusals(zsh, tmp_path):
    assert "requires macOS" in zsh("OSTYPE=linux; _t_resume api --app").stderr
    cwd = zsh.home / "code/.worktrees/api/3"
    cwd.mkdir(parents=True)
    import re
    project = zsh.home / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", str(cwd))
    project.mkdir(parents=True)
    (project / "claude-id.jsonl").write_text('{"type":"user","message":{"content":"Claude work"}}\n')
    app_stub(tmp_path)
    result = zsh("OSTYPE=darwin; _t_resume api 3 --app")
    assert result.returncode == 1 and "only saved Codex" in result.stderr
    assert "APP" not in result.stdout


@pytest.mark.parametrize("route,message", [("--app", "mark a single row"),
    ("--cli", "select one exact conversation")])
def test_resume_app_multiple_marks_never_launch(zsh, tmp_path, route, message):
    saved_rollout(zsh)
    saved_rollout(zsh, OTHER)
    app_stub(tmp_path)
    picker = tmp_path / "stubbin/fzf"
    picker.write_text('#!/bin/sh\ncat\n')
    picker.chmod(0o755)
    controls = "T_CORE_DETACH=1 " if route == "--cli" else ""
    result = zsh(f"OSTYPE=darwin; {controls}_t_resume api {route}", _tty=True)
    assert result.returncode == 1
    assert message in result.stdout and "APP" not in result.stdout
    assert "send-keys" not in zsh.log.read_text()


def test_resume_cli_retains_terminal_route(zsh, tmp_path):
    saved_rollout(zsh)
    app_stub(tmp_path)
    result = zsh("_t_resume api 3 --cli")
    assert result.returncode == 0, result.stderr
    assert f"codex resume {SID}" in zsh.log.read_text()
    assert "APP" not in result.stdout


@pytest.mark.parametrize("host,thread,label,agent", [
    ("mini", SID, "api-3", "codex"), ("local", OTHER, "api-3", "codex"),
    ("local", SID, "api:foreground", "codex"), ("local", SID, "api-3", "claude"),
])
def test_resume_app_check_refuses_conflicting_owner(zsh, host, thread, label, agent):
    cwd, _ = saved_rollout(zsh)
    row = f"{host}\t{thread}\t{cwd}\t{label}\tdetached\tactive\tWork\t{agent}"
    result = zsh(f"_dev_rows_all() {{ print -r -- {shlex.quote(row)}; }}; "
                 f"_t_resume_app_check api 3 {SID} {shlex.quote(str(cwd))}")
    assert result.returncode == 1, result.stdout
    assert "owned on" in result.stderr or "different conversation" in result.stderr
    assert "new-session" not in (zsh.log.read_text() if zsh.log.exists() else "")


def test_resume_app_scan_failure_never_attaches_local_slot(zsh):
    cwd, _ = saved_rollout(zsh)
    result = zsh("OSTYPE=darwin; _dev_rows_all() { return 1; }; _t_resume api 3 --app",
                 FAKE_SESSIONS="dev-api-3", FAKE_SESSION_PATH=str(cwd))
    assert result.returncode == 1 and "ownership could not be verified" in result.stderr
    assert "attach-session" not in zsh.log.read_text()


@pytest.mark.parametrize("mode", ["saved", "cli"])
def test_saved_app_handoff_opens_exact_id(t_mod, app_command, app_slot, monkeypatch, mode):
    _, events, row = app_command
    cfg, _ = app_slot
    checks = []
    def check(selected, repo, slot):
        checks.append((selected["sid"], selected["cwd"]))
        return [mode, "api-13"]
    monkeypatch.setattr(t_mod, "_app_resume_check", check)
    args = t_mod.build_parser().parse_args(["app", "api", "13", "--reuse-window", "--no-plan", "--no-preview"])
    assert t_mod.cmd_app(cfg, args, saved=(SID, row["cwd"])) == 0
    assert checks == [(SID, row["cwd"])] * 2
    opens = [event[1] for event in events if event[0] == "open"]
    assert ["open", "-a", "/Applications/ChatGPT.app", "codex://threads/" + SID] in opens
    assert any("_dev_history_record" in str(argv) and "desktop" in str(argv) for argv in opens)
    assert any(event[0] == "stop" for event in events) == (mode == "cli")
    assert not any("threads/new" in str(argv) for argv in opens)


def test_saved_app_changed_owner_never_stops_or_opens(t_mod, app_command, app_slot, monkeypatch, capsys):
    _, events, row = app_command
    cfg, _ = app_slot
    owners = iter([["saved", "api-13"], ["cli", "api-13"]])
    monkeypatch.setattr(t_mod, "_app_resume_check", lambda *a: next(owners))
    args = t_mod.build_parser().parse_args(["app", "api", "13", "--reuse-window", "--no-plan"])
    assert t_mod.cmd_app(cfg, args, saved=(SID, row["cwd"])) == 1
    assert "owner changed" in capsys.readouterr().err
    assert not events


@pytest.mark.parametrize("output,rc", [("saved\tapi-3\n", 0), ("cli\tapi-3\n", 0),
                                      ("saved\tapi-3\n", 1), ("", 0), ("saved\tbad\n", 0)])
def test_resume_check_requires_successful_protocol(t_mod, monkeypatch, output, rc):
    def run(argv, **kw):
        assert argv[:2] == ["zsh", "-lic"]
        assert f"_t_resume_app_check api 3 {SID}" in argv[-1]
        return subprocess.CompletedProcess(argv, rc, output, "")
    monkeypatch.setattr(t_mod, "_run", run)
    if not rc and output.split("\t")[-1].strip() == "api-3":
        assert t_mod._app_resume_check(dict(sid=SID, cwd="/recorded path"), "api", "3") == output.strip().split("\t")
    else:
        with pytest.raises(ValueError, match="ownership could not be verified"):
            t_mod._app_resume_check(dict(sid=SID, cwd="/recorded path"), "api", "3")


@pytest.mark.parametrize("mode", ["saved", "cli", "app", "reserved", "parked"])
def test_resume_app_check_accepts_only_exact_local_owner(zsh, mode):
    cwd, _ = saved_rollout(zsh)
    state = 'app' if mode in ('app', 'reserved', 'parked') else 'detached'
    context = 'none' if mode == 'reserved' else 'active'
    thread = '-' if mode == 'reserved' else SID
    row = f"local\t{thread}\t{cwd}\tapi-3\t{state}\t{context}\tWork\tcodex"
    rows = "" if mode == "saved" else row
    snippet = (f"_dev_rows_all() {{ print -r -- {shlex.quote(rows)}; }}; "
               f"_dev_agent_of_session() {{ print codex; }}; _dev_session_sid() {{ print {SID}; }}; "
               f"_t_resume_app_check api 3 {SID} {shlex.quote(str(cwd))}")
    result = zsh(snippet, FAKE_SESSION_ROWS=f"dev-api-3|{cwd}" if mode in ('cli', 'parked') else "")
    assert result.returncode == 0, result.stderr
    assert result.stdout == ("cli" if mode in ('cli', 'parked') else "saved") + "\tapi-3\n"


def test_resume_app_current_cli_does_not_replace_saved_selection(zsh, tmp_path):
    cwd, _ = saved_rollout(zsh)
    app_stub(tmp_path)
    row = f"{OTHER}\t{cwd}\tapi-3\tdetached\tactive\tDifferent task\tcodex"
    result = zsh(f"OSTYPE=darwin; _dev_session_rows() {{ print -r -- {shlex.quote(row)}; }}; "
                 "_t_dev() { echo WRONG-ATTACH; }; _t_resume api 3 --app",
                 FAKE_SESSIONS="dev-api-3", FAKE_SESSION_PATH=str(cwd))
    # Selection still carries the saved ID to the handoff, whose recheck refuses
    # the different owner (covered above), rather than borrowing its live ID.
    assert result.returncode == 0, result.stderr
    assert f"APP _app-resume api 3 {SID}" in result.stdout
    assert "WRONG-ATTACH" not in result.stdout


@pytest.mark.parametrize("kind", ["remote", "unreachable", "stale-slot", "wrong-path", "preparing"])
def test_resume_app_check_fails_closed(zsh, kind):
    cwd, _ = saved_rollout(zsh)
    shell = "_dev_rows_all() { :; }; "
    env = {}
    if kind == "remote":
        remote_cwd = "/home/remote/code/.worktrees/api/3"
        row = f"mini\t{OTHER}\t{remote_cwd}\talias-99\tapp\tactive\tWork\tcodex"
        shell = f"_dev_rows_all() {{ print -r -- {shlex.quote(row)}; }}; "
        # Same host-relative path, even when the host uses another alias/slot.
        shell += f"_dev_homerel() {{ [[ $1 == {shlex.quote(str(cwd))} ]] && print code/.worktrees/api/3 || print ${{1#/home/remote/}}; }}; "
    elif kind == "unreachable":
        shell = "_dev_rows_all() { return 3; }; "
    elif kind == "stale-slot":
        env['FAKE_SESSION_ROWS'] = f"dev-api-3|{cwd}"
    elif kind == "wrong-path":
        cwd = cwd.parent / "api-99"
    else:
        shell += "_dev_issue_preparing() { return 0; }; "
    result = zsh(shell + f"_t_resume_app_check api 3 {SID} {shlex.quote(str(cwd))}", **env)
    assert result.returncode == 1, result.stdout
    assert "t resume --app:" in result.stderr
    assert "new-session" not in (zsh.log.read_text() if zsh.log.exists() else "")


@pytest.mark.parametrize("failure", ["unindexed", "old", "archived", "wrong-worktree"])
def test_saved_app_unavailable_never_substitutes(t_mod, app_command, app_slot, monkeypatch, capsys, failure):
    _, events, row = app_command
    cfg, _ = app_slot
    saved = (SID, row["cwd"])
    if failure == "unindexed":
        monkeypatch.setattr(t_mod, "zsh_capture", lambda _: "")
    elif failure == "old":
        from pathlib import Path
        rollout = Path(row["rollout"])
        rollout.write_text(rollout.read_text().replace('0.160.1', '0.100.0'))
    elif failure == "archived":
        resolve = t_mod._app_saved_thread
        monkeypatch.setattr(t_mod, "_app_saved_thread", lambda row: dict(resolve(row), archived=True))
    else:
        saved = (SID, "/other/worktree")
    args = t_mod.build_parser().parse_args(["app", "api", "13", "--reuse-window", "--no-plan"])
    assert t_mod.cmd_app(cfg, args, saved=saved) == 1
    assert not events
    err = capsys.readouterr().err
    expected = {"unindexed": "--cli and select " + SID, "old": "no verifiable writer-lock contract",
                "archived": "archived", "wrong-worktree": "recorded worktree"}[failure]
    assert expected in err


def test_saved_app_sid_lock_excludes_shell_launch(t_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(t_mod, "_cache_root", lambda: str(tmp_path / "cache"))
    row = dict(sid=SID, cwd=str(tmp_path / "worktree"))
    path = tmp_path / "cache/launch-sids" / (hashlib.sha256(SID.encode()).hexdigest() + ".lock")
    snippet = 'zmodload zsh/system; zsystem flock -t 0 -f lock_fd "$1"'
    with t_mod._app_resume_lock(row):
        inode = path.stat().st_ino
        result = subprocess.run(["zsh", "-c", snippet, "check", str(path)], capture_output=True, text=True)
        assert result.returncode != 0
    result = subprocess.run(["zsh", "-c", snippet, "check", str(path)], capture_output=True, text=True)
    assert result.returncode == 0 and path.stat().st_ino == inode


def test_saved_app_sid_lock_refuses_existing_owner(t_mod, tmp_path, monkeypatch):
    import sys
    monkeypatch.setattr(t_mod, "_cache_root", lambda: str(tmp_path / "cache"))
    row = dict(sid=SID, cwd=str(tmp_path / "worktree"))
    path = tmp_path / "cache/launch-sids" / (hashlib.sha256(SID.encode()).hexdigest() + ".lock")
    path.parent.mkdir(parents=True)
    owner = subprocess.Popen([sys.executable, "-c",
        "import fcntl,sys; f=open(sys.argv[1], 'a'); fcntl.lockf(f, fcntl.LOCK_EX); "
        "print('ready', flush=True); sys.stdin.read()", str(path)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert owner.stdout.readline().strip() == "ready"
        with pytest.raises(ValueError, match="could not lock the saved conversation"):
            with t_mod._app_resume_lock(row):
                pytest.fail("entered a competing launch")
    finally:
        owner.communicate(timeout=5)


def test_saved_old_metadata_handoff_waits_for_its_writer(t_mod, app_command, app_slot, monkeypatch):
    from pathlib import Path
    import sys
    _, events, row = app_command
    cfg, _ = app_slot
    rollout = Path(row['rollout'])
    rollout.write_text(rollout.read_text().replace('0.160.1', '0.100.0'))
    before = rollout.read_bytes()
    lock = Path(row['codex_home']) / 'thread-writer-locks' / (SID + '.lock')
    owner = subprocess.Popen([sys.executable, '-c',
        "import fcntl,sys; f=open(sys.argv[1], 'a'); fcntl.flock(f, fcntl.LOCK_EX); "
        "print('ready', flush=True); sys.stdin.readline(); fcntl.flock(f, fcntl.LOCK_UN); "
        "print('released', flush=True); sys.stdin.read()", str(lock)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert owner.stdout.readline().strip() == 'ready'
        inode = lock.stat().st_ino
        monkeypatch.setattr(t_mod, '_app_resume_check', lambda *a: ['cli', 'api-13'])
        def stop(selected):
            assert selected['sid'] == SID and selected['cwd'] == row['cwd']
            owner.stdin.write('release\n'); owner.stdin.flush()
            assert owner.stdout.readline().strip() == 'released'
            return subprocess.CompletedProcess([], 0, '', '')
        monkeypatch.setattr(t_mod, '_app_stop_cli', stop)
        args = t_mod.build_parser().parse_args(['app', 'api', '13', '--reuse-window', '--no-plan', '--no-preview'])
        assert t_mod.cmd_app(cfg, args, saved=(SID, row['cwd'])) == 0
        assert any(event[1] == ['open', '-a', '/Applications/ChatGPT.app', 'codex://threads/' + SID]
                   for event in events)
        assert rollout.read_bytes() == before and lock.stat().st_ino == inode
    finally:
        owner.communicate(timeout=5)


def test_resume_app_remote_history_fetch_keeps_local_destination(zsh, tmp_path):
    cwd, rollout = saved_rollout(zsh)
    # Simulate the existing rsync seam delivering a history from an idle host.
    source = tmp_path / 'remote-rollout.jsonl'
    rollout.rename(source)
    app_stub(tmp_path)
    rsync = tmp_path / 'stubbin/rsync'
    rsync.write_text('#!/bin/sh\n' +
        f'cp {shlex.quote(str(source))} {shlex.quote(str(rollout))}\nprintf "%s\\n" "$*" >> "$HOME/rsync-log"\n')
    rsync.chmod(0o755)
    cache = zsh.home / '.cache/t/hostnames'
    cache.mkdir(parents=True)
    (cache / 'mini').write_text('mini\n')
    result = zsh('OSTYPE=darwin; REMOTE_HOSTS[mini]=mini.local; _dev_rows_all() { :; }; '
                 '_t_resume api 3 --remote --app')
    assert result.returncode == 0, result.stderr
    assert f'APP _app-resume api 3 {SID} {cwd}' in result.stdout
    assert 'mini.local:' in (zsh.home / 'rsync-log').read_text()
    assert rollout.read_bytes() == source.read_bytes()


@pytest.mark.parametrize('repo', ['api', 'push', 'pull'])
def test_private_resume_dispatch_keeps_reserved_repo_names(t_mod, monkeypatch, repo):
    def app(cfg, args, *, saved=None):
        assert t_mod._app_target(args) == ('push', repo, '3')
        assert saved == (SID, '/recorded/worktree')
        assert args.reuse_window
        return 42
    monkeypatch.setattr(t_mod, 'cmd_app', app)
    assert t_mod.main(['_app-resume', repo, '3', SID, '/recorded/worktree']) == 42


def test_saved_app_real_worktree_reservation_keeps_exact_older_thread(t_mod, zsh, tmp_path, monkeypatch):
    """Real scanners see the marker created between the two handoff checks."""
    import sqlite3
    from contextlib import closing
    cwd, rollout = saved_rollout(zsh, age=1)
    _, newer = saved_rollout(zsh, OTHER)
    repo = zsh.home / 'code/api'
    repo.mkdir(parents=True)
    def git(*args):
        return subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True, text=True)
    git('init', '-q', '-b', 'main')
    git('-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-qm', 'fixture')
    git('worktree', 'add', '-q', '-b', 'dev/api-3', str(cwd))
    codex_home = zsh.home / '.codex'
    (codex_home / 'thread-writer-locks').mkdir()
    with closing(sqlite3.connect(codex_home / 'state_5.sqlite')) as db:
        db.execute('create table threads (id text, rollout_path text, cwd text, title text, name text, updated_at int, archived int, source text)')
        db.executemany('insert into threads values (?, ?, ?, ?, ?, ?, ?, ?)', [
            (SID, str(rollout), str(cwd), 'Older task', '', 1, 0, 'cli'),
            (OTHER, str(newer), str(cwd), 'Newer task', '', 2, 0, 'cli')])
        db.commit()
    monkeypatch.setattr(t_mod, 'CONFIG', str(tmp_path / 'absent'))
    monkeypatch.setattr(t_mod, '_cache_root', lambda: str(zsh.home / '.cache/claude-sessions'))
    monkeypatch.setattr(t_mod.sys, 'platform', 'darwin')
    monkeypatch.setattr(t_mod, '_app_bundle', lambda: '/Applications/Codex.app')
    cfg = t_mod.Config()
    cfg.repos = {'api': str(repo)}
    cfg.worktree_root = str(cwd.parent.parent)
    opened = []
    def run(argv, **kw):
        if argv[0] == 'open':
            opened.append(argv)
            return subprocess.CompletedProcess(argv, 0, '', '')
        if argv[0] == 'zsh':
            assert argv[1] == '-lic'
            return zsh(argv[2])
        return subprocess.run(argv, capture_output=True, text=True, **kw)
    def capture(snippet):
        result = zsh(snippet)
        assert result.returncode == 0, result.stderr
        return result.stdout
    monkeypatch.setattr(t_mod, '_run', run)
    monkeypatch.setattr(t_mod, 'zsh_capture', capture)
    monkeypatch.setattr(t_mod, '_app_stop_cli', lambda _: pytest.fail('saved history has no CLI to stop'))
    before = {p: p.read_bytes() for p in (rollout, newer)}
    args = t_mod.build_parser().parse_args(['app', 'api', '3', '--reuse-window', '--no-plan', '--no-preview'])
    assert t_mod.cmd_app(cfg, args, saved=(SID, str(cwd))) == 0
    assert opened == [['open', '-a', '/Applications/Codex.app', 'codex://threads/' + SID]]
    marker = repo / '.git/worktrees/3/t-app-slot'
    assert marker.read_text() == 'codex-app\n'
    assert {p: p.read_bytes() for p in before} == before
    assert 'new-session' not in zsh.log.read_text()
