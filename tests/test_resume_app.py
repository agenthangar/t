"""Saved conversation selection must reach the guarded, exact desktop route."""

import json
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


def test_resume_app_multiple_marks_never_launch(zsh, tmp_path):
    saved_rollout(zsh)
    saved_rollout(zsh, OTHER)
    app_stub(tmp_path)
    picker = tmp_path / "stubbin/fzf"
    picker.write_text('#!/bin/sh\ncat\n')
    picker.chmod(0o755)
    result = zsh("OSTYPE=darwin; _t_resume api --app", _tty=True)
    assert "mark a single row" in result.stdout and "APP" not in result.stdout


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
