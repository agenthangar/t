"""A desktop slot closes only after exact Codex ownership checks."""

import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest


SID = "01234567-89ab-cdef-0123-456789abcdef"


@pytest.fixture
def desktop_close(t_mod, tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "README.md").write_text("# disposable\n")
    subprocess.run(["git", "-C", str(repo), "add", "README.md"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.name=Test",
                    "-c", "user.email=test@example.invalid", "commit", "-qm", "start"], check=True)
    cwd = tmp_path / "worktrees" / "repo" / "13"
    cwd.parent.mkdir(parents=True)
    subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", "-b",
                    "dev/repo-13", str(cwd), "main"], check=True)
    private = Path(subprocess.run(["git", "-C", str(cwd), "rev-parse", "--absolute-git-dir"],
                                  capture_output=True, text=True, check=True).stdout.strip())
    marker = private / "t-app-slot"
    marker.write_text("codex-app\n")
    home = tmp_path / "codex"
    (home / "thread-writer-locks").mkdir(parents=True)
    rollout = home / "sessions" / "2026" / "10" / "06" / f"rollout-{SID}.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": SID, "cwd": str(cwd), "cli_version": "0.160.0"}}) + "\n")
    (home / "thread-writer-locks" / f"{SID}.lock").touch()
    cfg = t_mod.Config()
    cfg.repos = {"repo": str(repo)}
    cfg.worktree_root = str(cwd.parent.parent)
    row = {"host": "local", "sid": SID, "cwd": str(cwd), "slot": "repo-13",
           "state": "app", "context": "none", "agent": "codex", "summary": "saved"}
    saved = lambda: "\t".join([SID, str(rollout), str(cwd), "", "", "0", str(home)])
    monkeypatch.setattr(t_mod, "_parse_rows", lambda _: [row])
    monkeypatch.setattr(t_mod, "zsh_capture", lambda command: saved() if command.startswith(
        "_codex_threads_for_cwd") else "rows")
    monkeypatch.setattr(t_mod, "_app_bundle", lambda: None)
    monkeypatch.setattr(t_mod, "_cache_root", lambda: str(tmp_path / "cache"))
    calls = []
    def run(argv, cwd=None, timeout=None):
        calls.append(argv)
        if argv[0] == "git":
            return subprocess.run(argv, cwd=cwd, timeout=timeout, text=True,
                                  capture_output=True)
        if argv[0] == "ps":
            return subprocess.CompletedProcess(argv, 0, "", "")
        if argv[0] == "zsh":
            return subprocess.CompletedProcess(argv, 0, "", "")
        raise AssertionError(argv)
    monkeypatch.setattr(t_mod, "_run", run)
    return cfg, row, home, rollout, marker, calls, monkeypatch


def test_close_released_desktop_slot(t_mod, desktop_close, capsys):
    cfg, row, home, rollout, marker, calls, _ = desktop_close
    assert t_mod._app_close(cfg, "repo", "13") == row
    shell = [argv for argv in calls if argv[0] == "zsh"]
    assert len(shell) == 1
    command = shell[0][2]
    for value in ("_t_app_close_slot", "dev-repo-13", str(row["cwd"]), SID,
                  str(home), str(rollout)):
        assert value in command
    assert command.endswith(" 0")
    assert marker.read_text() == "codex-app\n"
    assert "conversation and worktree are preserved" in capsys.readouterr().out


def test_close_archived_desktop_slot_without_unarchiving(t_mod, desktop_close):
    cfg, row, home, rollout, marker, calls, monkeypatch = desktop_close
    archive = home / "archived_sessions" / rollout.name
    archive.parent.mkdir()
    rollout.replace(archive)
    monkeypatch.setattr(t_mod, "zsh_capture", lambda command: "\t".join([
        SID, str(archive), row["cwd"], "", "", "1", str(home)]) if command.startswith(
            "_codex_threads_for_cwd") else "rows")
    assert t_mod._app_close(cfg, "repo", "13") == row
    shell = [argv for argv in calls if argv[0] == "zsh"]
    assert len(shell) == 1 and shell[0][2].endswith(" 1")
    assert archive.exists() and not rollout.exists()


def test_close_loaded_desktop_conversation_requires_only_selected_archive(
        t_mod, desktop_close, tmp_path):
    cfg, row, home, rollout, marker, calls, _ = desktop_close
    lock = home / "thread-writer-locks" / f"{SID}.lock"
    code = ("import fcntl,sys; f=open(sys.argv[1], 'r'); "
            "fcntl.flock(f, fcntl.LOCK_EX); print('ready', flush=True); sys.stdin.read()")
    env = {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", os.defpath)}
    owner = subprocess.Popen([sys.executable, "-c", code, str(lock)], env=env,
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        assert owner.stdout.readline().strip() == "ready"
        with pytest.raises(ValueError, match="Archive it.*app can stay open"):
            t_mod._app_close(cfg, "repo", "13")
    finally:
        owner.stdin.close()
        owner.wait(timeout=3)
        owner.stdout.close()
        owner.stderr.close()
    assert marker.exists()
    assert not any(argv[0] == "zsh" for argv in calls)


def test_close_rejects_unverified_marker(t_mod, desktop_close):
    cfg, row, home, rollout, marker, calls, _ = desktop_close
    marker.write_text("not-a-t-slot\n")
    with pytest.raises(ValueError, match="not a verified t-owned"):
        t_mod._app_close(cfg, "repo", "13")
    assert not any(argv[0] == "zsh" for argv in calls)


def test_close_shell_failure_keeps_reservation(t_mod, desktop_close, monkeypatch):
    cfg, row, home, rollout, marker, calls, _ = desktop_close
    actual = t_mod._run
    def failed(argv, cwd=None, timeout=None):
        if argv[0] == "zsh":
            return subprocess.CompletedProcess(argv, 1, "", "writer appeared")
        return actual(argv, cwd=cwd, timeout=timeout)
    monkeypatch.setattr(t_mod, "_run", failed)
    with pytest.raises(ValueError, match="writer appeared"):
        t_mod._app_close(cfg, "repo", "13")
    assert marker.read_text() == "codex-app\n"


def test_close_active_saved_view_requires_selected_archive_while_app_runs(
        t_mod, desktop_close, monkeypatch):
    cfg, row, home, rollout, marker, calls, _ = desktop_close
    actual = t_mod._run
    def run(argv, cwd=None, timeout=None):
        if argv[0] == "ps":
            return subprocess.CompletedProcess(
                argv, 0, "123 /Applications/Codex.app/Contents/MacOS/Codex", "")
        return actual(argv, cwd=cwd, timeout=timeout)
    monkeypatch.setattr(t_mod, "_run", run)
    with pytest.raises(ValueError, match="Archive this chat.*app and other chats can stay open"):
        t_mod._app_close(cfg, "repo", "13")
    assert marker.exists() and not any(argv[0] == "zsh" for argv in calls)


def test_close_archived_view_allows_unrelated_running_app(t_mod, desktop_close, monkeypatch):
    cfg, row, home, rollout, marker, calls, _ = desktop_close
    archive = home / "archived_sessions" / rollout.name
    archive.parent.mkdir()
    rollout.replace(archive)
    monkeypatch.setattr(t_mod, "zsh_capture", lambda command: "\t".join([
        SID, str(archive), row["cwd"], "", "", "1", str(home)]) if command.startswith(
            "_codex_threads_for_cwd") else "rows")
    actual = t_mod._run
    def run(argv, cwd=None, timeout=None):
        if argv[0] == "ps":
            return subprocess.CompletedProcess(
                argv, 0, "123 /Applications/Codex.app/Contents/MacOS/Codex", "")
        return actual(argv, cwd=cwd, timeout=timeout)
    monkeypatch.setattr(t_mod, "_run", run)
    t_mod._app_close(cfg, "repo", "13")
    assert len([argv for argv in calls if argv[0] == "zsh"]) == 1


def test_close_blank_desktop_workspace_without_inventing_a_thread(
        t_mod, desktop_close, monkeypatch):
    cfg, row, home, rollout, marker, calls, _ = desktop_close
    row["sid"] = "-"
    db = sqlite3.connect(home / "state_5.sqlite")
    db.execute("create table threads (id text, cwd text)")
    db.commit()
    db.close()
    monkeypatch.setattr(t_mod, "zsh_capture", lambda command: (
        str(home / "state_5.sqlite") if command == "_codex_db" else ""
        if command.startswith("_codex_threads_for_cwd") else "rows"))
    assert t_mod._app_close(cfg, "repo", "13") == row
    shell = [argv for argv in calls if argv[0] == "zsh"]
    assert len(shell) == 1 and "dev-repo-13" in shell[0][2]
    assert " - " in shell[0][2] and shell[0][2].endswith(" - 0")


def test_blank_workspace_rejects_saved_record_even_when_not_selectable(
        t_mod, desktop_close, monkeypatch):
    cfg, row, home, rollout, marker, calls, _ = desktop_close
    row["sid"] = "-"
    db = sqlite3.connect(home / "state_5.sqlite")
    db.execute("create table threads (id text, cwd text)")
    db.execute("insert into threads values (?, ?)", (SID, row["cwd"]))
    db.commit()
    db.close()
    monkeypatch.setattr(t_mod, "zsh_capture", lambda command: (
        str(home / "state_5.sqlite") if command == "_codex_db" else ""
        if command.startswith("_codex_threads_for_cwd") else "rows"))
    with pytest.raises(ValueError, match="saved Codex conversation.*could not be selected"):
        t_mod._app_close(cfg, "repo", "13")
    assert not any(argv[0] == "zsh" for argv in calls)


def test_close_blank_workspace_requires_app_frontend_absent(
        t_mod, desktop_close, monkeypatch):
    cfg, row, home, rollout, marker, calls, _ = desktop_close
    row["sid"] = "-"
    db = sqlite3.connect(home / "state_5.sqlite")
    db.execute("create table threads (id text, cwd text)")
    db.commit()
    db.close()
    monkeypatch.setattr(t_mod, "zsh_capture", lambda command: (
        str(home / "state_5.sqlite") if command == "_codex_db" else ""
        if command.startswith("_codex_threads_for_cwd") else "rows"))
    actual = t_mod._run
    def run(argv, cwd=None, timeout=None):
        if argv[0] == "ps":
            return subprocess.CompletedProcess(
                argv, 0, "123 /Applications/Codex.app/Contents/MacOS/Codex", "")
        return actual(argv, cwd=cwd, timeout=timeout)
    monkeypatch.setattr(t_mod, "_run", run)
    with pytest.raises(ValueError, match="Cannot verify this empty workspace.*close Codex"):
        t_mod._app_close(cfg, "repo", "13")
    assert marker.exists() and not any(argv[0] == "zsh" for argv in calls)
