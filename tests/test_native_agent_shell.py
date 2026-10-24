"""Native agent launch paths work before the user's shell PATH is refreshed."""

import os
from pathlib import Path
import shlex
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
ZSH = shutil.which("zsh")
pytestmark = pytest.mark.skipif(not ZSH, reason="zsh is required")


def executable(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('#!/bin/sh\nprintf "%s\\n" "$0" "$@"\n')
    path.chmod(0o755)
    return path


def run_shell(tmp_path, code, path="/usr/bin:/bin"):
    home = tmp_path / "home with spaces and 'quotes'"
    home.mkdir(exist_ok=True)
    env = {"HOME": str(home), "ZDOTDIR": str(home), "PATH": path,
           "XDG_CONFIG_HOME": str(home / ".config"),
           "XDG_CACHE_HOME": str(home / ".cache"),
           "TMUX_TMPDIR": str(tmp_path / "tmux"), "T_RECOVERY_DISABLE": "1"}
    script = (f"source {shlex.quote(str(ROOT / 'zsh/agent.zsh'))}; "
              "typeset -gA DEV_MODEL DEV_EFFORT DEV_FAST; " + code)
    return subprocess.run([ZSH, "-f", "-c", script], env=env, cwd=tmp_path,
                          capture_output=True, text=True, timeout=10)


@pytest.mark.parametrize("agent", ["claude", "codex"])
@pytest.mark.parametrize("mode", ["new", "resume"])
@pytest.mark.parametrize("on_path", [False, True])
def test_native_agent_generated_commands_execute_with_quoted_arguments(tmp_path, agent, mode, on_path):
    home = tmp_path / "home with spaces and 'quotes'"
    native = executable(home / ".local/bin" / agent)
    preferred = executable(tmp_path / "preferred bin" / agent)
    path = f"{preferred.parent}:/usr/bin:/bin" if on_path else "/usr/bin:/bin"
    sid = "session; $(touch unexpected)"
    extra = "directory with spaces; $(touch unexpected)"
    arguments = [agent, sid] + (["--cd", extra] if mode == "resume" else [])
    result = run_shell(tmp_path, f"_dev_agent_check {agent} || exit; "
                       f'eval "$(_dev_agent_{mode}_cmd {shlex.join(arguments)})"; '
                       'print -r -- "PATH=$PATH"', path)
    assert result.returncode == 0, result.stderr
    expected = [str(preferred if on_path else native)]
    if mode == "new":
        expected += ["--session-id", sid] if agent == "claude" else ["--no-daemon"]
    else:
        expected += (["-r", sid] if agent == "claude" else ["resume", sid]) + ["--cd", extra]
        if agent == "codex":
            expected += ["--no-daemon"]
    assert result.stdout.splitlines() == expected + [f"PATH={path}"]
    assert not (tmp_path / "unexpected").exists()


@pytest.mark.parametrize("agent", ["claude", "codex"])
def test_native_fallback_preserves_shell_wrappers(tmp_path, agent):
    executable(tmp_path / "home with spaces and 'quotes'" / ".local/bin" / agent)
    result = run_shell(tmp_path, f'{agent}() {{ print -r -- "wrapper:$*"; }}; '
                       f'_dev_agent_check {agent} || exit; '
                       f'eval "$(_dev_agent_resume_cmd {agent} sid)"')
    assert result.returncode == 0, result.stderr
    expected = "wrapper:-r sid" if agent == "claude" else "wrapper:resume sid --no-daemon"
    assert result.stdout.strip() == expected


@pytest.mark.parametrize("state", ["missing", "not-executable", "directory", "broken-link"])
def test_native_agent_check_rejects_unusable_fallback(tmp_path, state):
    native = tmp_path / "home with spaces and 'quotes'" / ".local/bin/claude"
    native.parent.mkdir(parents=True)
    if state == "not-executable":
        native.write_text("#!/bin/sh\nexit 0\n")
        native.chmod(0o644)
    elif state == "directory":
        native.mkdir()
    elif state == "broken-link":
        native.symlink_to(native.parent / "missing-target")
    result = run_shell(tmp_path, "_dev_agent_check claude")
    assert result.returncode == 1
    assert "t agent install claude" in result.stderr
    assert not result.stdout


def test_native_fallback_does_not_resolve_arbitrary_paths(tmp_path):
    target = executable(tmp_path / "home with spaces and 'quotes'" / ".local/other")
    result = run_shell(tmp_path, "_dev_agent_executable ../other")
    assert result.returncode == 1
    assert not result.stdout
    assert os.access(target, os.X_OK)
