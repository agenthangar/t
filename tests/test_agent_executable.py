"""Execute installer steps against isolated native agents, without real sign-ins."""

import os
from pathlib import Path
import sys

import pytest


AGENT_SCRIPT = '''#!/bin/sh
printf '%s|%s|%s\\n' "$0" "$*" "${NO_OPEN_BROWSER-}" >> "$HOME/agent-calls"
case "$*" in
    --version) printf '1.2.3\\n' ;;
    *status) printf 'Not logged in\\n'; exit 1 ;;
    *) exit "${FAKE_AGENT_EXIT:-0}" ;;
esac
'''


@pytest.fixture
def account(t_mod, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(t_mod, "HOME", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    monkeypatch.delenv("NO_OPEN_BROWSER", raising=False)
    monkeypatch.delenv("FAKE_AGENT_EXIT", raising=False)
    for name, suffix in (("XDG_CONFIG_HOME", ".config"),
                         ("XDG_DATA_HOME", ".local/share"),
                         ("XDG_CACHE_HOME", ".cache")):
        monkeypatch.setenv(name, str(home / suffix))
    monkeypatch.setattr(t_mod, "_install_relink", lambda: 0)
    return home


def native_binary(home, name, *, content=AGENT_SCRIPT, executable=True):
    exe = home / ".local" / "bin" / name
    exe.parent.mkdir(parents=True, exist_ok=True)
    exe.write_text(content)
    exe.chmod(0o755 if executable else 0o644)
    return exe


def plan(t_mod, agent, *, install=False, update=False, headless=False):
    selection = {"install": [agent] if install else [], "login": [agent],
                 "update": update, "hosts": []}
    return t_mod._install_plan(selection, t_mod._install_probe([agent]),
                               "linux", False, headless, [])


@pytest.mark.parametrize("agent,login", [
    ("claude", "auth login"), ("codex", "login --device-auth"),
    ("cursor", "login"),
])
def test_install_walk_executes_detected_native_agent_outside_path(
        t_mod, account, agent, login):
    exe = native_binary(account, t_mod._INSTALL_AGENTS[agent]["bin"])
    before = os.environ["PATH"]
    steps = plan(t_mod, agent, update=True, headless=True)
    reviewed = [(step["label"], step["cmd"]) for step in steps]
    assert t_mod._install_walk(steps) == 0
    calls = (account / "agent-calls").read_text().splitlines()
    assert calls[-2:] == [f"{exe}|update|",
                         f"{exe}|{login}|{'1' if agent == 'cursor' else ''}"]
    assert os.environ["PATH"] == before
    assert [(step["label"], step["cmd"]) for step in steps] == reviewed
    assert " ".join(t_mod._install_login_cmd(agent, True)) in [s["label"] for s in steps]


def test_install_walk_finds_binary_created_after_review(t_mod, account, monkeypatch):
    exe = account / ".local/bin/claude"
    installer = account / "install-fixture.py"
    installer.write_text(
        "from pathlib import Path\n"
        f"exe = Path({str(exe)!r})\n"
        "exe.parent.mkdir(parents=True)\n"
        f"exe.write_text({AGENT_SCRIPT!r})\n"
        "exe.chmod(0o755)\n"
    )
    monkeypatch.setattr(t_mod, "_install_cmd",
                        lambda *a: ("install fixture", [sys.executable, str(installer)]))
    steps = plan(t_mod, "claude", install=True)
    assert not exe.exists()
    assert t_mod._install_walk(steps) == 0
    assert (account / "agent-calls").read_text() == f"{exe}|auth login|\n"


def test_install_agent_resolution_prefers_path(t_mod, account, monkeypatch):
    native_binary(account, "claude")
    path_home = account / "path-agent"
    path_exe = native_binary(path_home, "claude")
    monkeypatch.setenv("PATH", str(path_exe.parent) + ":/usr/bin:/bin")
    steps = plan(t_mod, "claude")
    assert t_mod._install_probe(["claude"])["claude"]["bin"] == str(path_exe)
    assert t_mod._install_walk(steps) == 0
    assert (account / "agent-calls").read_text().splitlines()[-1] == f"{path_exe}|auth login|"


@pytest.mark.parametrize("mode", ["missing", "not-executable", "directory", "broken-link"])
def test_install_probe_rejects_unusable_native_binary(t_mod, account, mode):
    exe = account / ".local/bin/claude"
    exe.parent.mkdir(parents=True)
    if mode == "not-executable":
        native_binary(account, "claude", executable=False)
    elif mode == "directory":
        exe.mkdir()
    elif mode == "broken-link":
        exe.symlink_to(account / "absent")
    assert t_mod._install_agent_executable("claude") is None
    assert t_mod._install_probe(["claude"])["claude"]["bin"] is None
    assert not (account / "agent-calls").exists()


@pytest.mark.parametrize("change", ["removed", "not-executable"])
def test_install_walk_reports_binary_unusable_after_review(
        t_mod, account, change, capsys):
    exe = native_binary(account, "claude")
    steps = plan(t_mod, "claude", update=True)
    if change == "removed":
        exe.unlink()
    else:
        exe.chmod(0o644)
    before = (account / "agent-calls").read_text()
    assert t_mod._install_walk(steps) == 1
    assert (account / "agent-calls").read_text() == before
    output = capsys.readouterr().out
    assert output.count("is missing or not executable") == 2
    assert "t agent install claude" in output


@pytest.mark.parametrize("failure", ["bad-format", "removed-after-resolution"])
def test_install_walk_reports_launch_errors_without_traceback(
        t_mod, account, monkeypatch, failure, capsys):
    exe = native_binary(account, "claude")
    steps = plan(t_mod, "claude")
    if failure == "bad-format":
        exe.write_text("not an executable format\n")
    else:
        resolver = t_mod._install_agent_executable

        def removed_after_resolution(agent):
            result = resolver(agent)
            Path(result).unlink()
            return result

        monkeypatch.setattr(t_mod, "_install_agent_executable", removed_after_resolution)
    assert t_mod._install_walk(steps) == 1
    output = capsys.readouterr().out
    assert "could not start the agent" in output
    assert "t agent install claude --reinstall" in output
    assert "Traceback" not in output


def test_install_walk_preserves_native_agent_failure(t_mod, account, monkeypatch, capsys):
    native_binary(account, "claude")
    steps = plan(t_mod, "claude")
    monkeypatch.setenv("FAKE_AGENT_EXIT", "7")
    assert t_mod._install_walk(steps) == 1
    assert "(rc 7)" in capsys.readouterr().out


def test_install_walk_reports_headless_cursor_disappearing_before_launch(
        t_mod, account, monkeypatch, capsys):
    exe = native_binary(account, "cursor-agent")
    steps = plan(t_mod, "cursor", headless=True)
    resolver = t_mod._install_agent_executable

    def removed_after_resolution(agent):
        result = resolver(agent)
        exe.unlink()
        return result

    monkeypatch.setattr(t_mod, "_install_agent_executable", removed_after_resolution)
    assert t_mod._install_walk(steps) == 1
    assert "t agent install cursor --reinstall" in capsys.readouterr().out


def test_install_walk_reports_unusable_update_binary(t_mod, account, capsys):
    exe = native_binary(account, "claude")
    steps = [s for s in plan(t_mod, "claude", update=True) if s["step"] == "update:claude"]
    exe.write_text("not an executable format\n")
    assert t_mod._install_walk(steps) == 1
    assert "t agent install claude --reinstall" in capsys.readouterr().out
