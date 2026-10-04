"""Regressions for postponed offers and TTY-bearing remote commands."""

import importlib.util
from pathlib import Path
import shlex
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("t_updates_review", ROOT / "libexec" / "t_updates.py")
updates = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updates)


def test_forced_check_reports_available_version_during_prompt_snooze(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    installation = {"kind": "release", "source": str(tmp_path / "release"), "current": "v1.2.3"}
    monkeypatch.setattr(updates, "_release", lambda identity: "v1.2.4")

    assert updates.check(installation, force=True)["available"]
    updates.snooze(installation)
    assert not updates.cached(installation)["available"]
    assert updates.check(installation, force=True)["available"]
    assert not updates.cached(installation)["available"]


def test_remote_on_keeps_login_shell_and_suppresses_update_prompt(t_mod, monkeypatch):
    seen = []

    def fake_exec(command, argv):
        seen.append((command, argv))
        raise RuntimeError("exec intercepted")

    monkeypatch.setattr(t_mod, "_set_term_title", lambda title: None)
    monkeypatch.setattr(t_mod.os, "execvp", fake_exec)
    remote_args = ["t", "ls", "two words", "a'b"]
    with pytest.raises(RuntimeError, match="exec intercepted"):
        t_mod.cmd_on(SimpleNamespace(hosts={"mini": "me@example.test"}),
                     SimpleNamespace(host="mini", rest=remote_args))

    command, argv = seen[0]
    assert command == "ssh"
    assert argv[:3] == ["ssh", "-t", "me@example.test"]
    assert argv[3] == "zsh -lic " + shlex.quote(
        "T_UPDATE_PROMPTED=$$ " + " ".join(shlex.quote(a) for a in remote_args))


def test_remote_on_without_command_still_opens_interactive_shell(t_mod, monkeypatch):
    seen = []

    def fake_exec(command, argv):
        seen.append((command, argv))
        raise RuntimeError("exec intercepted")

    monkeypatch.setattr(t_mod, "_set_term_title", lambda title: None)
    monkeypatch.setattr(t_mod.os, "execvp", fake_exec)
    with pytest.raises(RuntimeError, match="exec intercepted"):
        t_mod.cmd_on(SimpleNamespace(hosts={"mini": "me@example.test"}),
                     SimpleNamespace(host="mini", rest=[]))

    assert seen == [("ssh", ["ssh", "-t", "me@example.test"])]
