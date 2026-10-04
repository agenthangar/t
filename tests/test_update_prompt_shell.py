"""The zsh shim checks updates once before commands that need the current shell."""

import os
import pty
import select
import shutil
import subprocess
import time
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def run_shim(tmp_path, command, *, prompt_rc=0, interactive=True, extra_env=None,
             reload_body='print -r -- reload >> "$T_TEST_LOG"'):
    home = tmp_path / "home"
    home.mkdir()
    stub_dir = tmp_path / "bin"
    stub_dir.mkdir()
    log = tmp_path / "calls"
    binary = stub_dir / "t"
    binary.write_text(
        '#!/bin/sh\n'
        'printf "bin:%s:marker=%s\\n" "$*" "${T_UPDATE_PROMPTED:+set}" >> "$T_TEST_LOG"\n'
        'if [ "$1" = __update-prompt ]; then exit "$T_TEST_PROMPT_RC"; fi\n'
    )
    binary.chmod(0o755)
    script = (
        f'source "{ROOT / "zsh" / "shim.zsh"}"\n'
        f'_t_reload() {{ {reload_body}; }}\n'
        '_t_cd() { print -r -- "shell:cd:$*:marker=${T_UPDATE_PROMPTED:+set}" >> "$T_TEST_LOG"; }\n'
        '_t_open() { print -r -- "shell:open:$*:marker=${T_UPDATE_PROMPTED:+set}" >> "$T_TEST_LOG"; }\n'
        '_t_repos_cd() { print -r -- "shell:repos-cd:$*:marker=${T_UPDATE_PROMPTED:+set}" >> "$T_TEST_LOG"; }\n'
        f'{command}\n'
        'print -r -- "after:marker=${T_UPDATE_PROMPTED:+set}" >> "$T_TEST_LOG"\n'
    )
    env = {
        **os.environ,
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "TMUX_TMPDIR": str(tmp_path / "tmux"),
        "PATH": f"{stub_dir}:{os.environ['PATH']}",
        "T_TEST_LOG": str(log),
        "T_TEST_PROMPT_RC": str(prompt_rc),
    }
    for key in ("CI", "CLAUDECODE", "CODEX_THREAD_ID", "T_NO_UPDATE_CHECK",
                "T_UPDATE_PROMPTED"):
        env.pop(key, None)
    env.update(extra_env or {})
    for key in tuple(env):
        if key.startswith("COV_CORE_"):
            env.pop(key)
    if interactive:
        master, slave = pty.openpty()
        try:
            proc = subprocess.Popen(
                ["zsh", "-fic", script], stdin=slave, stdout=slave, stderr=slave,
                env=env, start_new_session=True,
            )
            os.close(slave)
            deadline = time.monotonic() + 10
            output = bytearray()
            while proc.poll() is None and time.monotonic() < deadline:
                if select.select([master], [], [], 0.1)[0]:
                    try:
                        output.extend(os.read(master, 4096))
                    except OSError:
                        break
            # PTY EOF may precede the child's exit status by a moment.
            try:
                proc.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=2)
                raise AssertionError(f"zsh timed out: {output!r}")
            assert proc.returncode == 0, output.decode(errors="replace")
        finally:
            os.close(master)
    else:
        proc = subprocess.run(["zsh", "-fc", script], env=env,
                              capture_output=True, text=True, timeout=10)
        assert proc.returncode == 0, proc.stderr
    return log.read_text().splitlines()


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is required")
def test_shell_command_reloads_after_update_then_runs_once(tmp_path):
    assert run_shim(tmp_path, "t cd api; t ls", prompt_rc=10) == [
        "bin:__update-prompt:marker=",
        "reload",
        "shell:cd:api:marker=set",
        "bin:ls:marker=",
        "after:marker=",
    ]


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is required")
def test_updated_shell_function_handles_original_command(tmp_path):
    calls = run_shim(
        tmp_path, "t cd api", prompt_rc=10,
        reload_body='_t_cd() { print -r -- "new:cd:$*:marker=${T_UPDATE_PROMPTED:+set}" >> "$T_TEST_LOG"; }',
    )
    assert calls == [
        "bin:__update-prompt:marker=",
        "new:cd:api:marker=set",
        "after:marker=",
    ]


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is required")
def test_failed_update_aborts_shell_command(tmp_path):
    assert run_shim(tmp_path, "t open api || :", prompt_rc=11) == [
        "bin:__update-prompt:marker=",
        "after:marker=",
    ]


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is required")
@pytest.mark.parametrize("command", ["t cd api", "t cd --help", "t ls"])
def test_noninteractive_shim_does_not_prompt(tmp_path, command):
    calls = run_shim(tmp_path, command, interactive=False)
    assert all("__update-prompt" not in call for call in calls)


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is required")
@pytest.mark.parametrize("setting", [{"CI": "1"}, {"T_NO_UPDATE_CHECK": "1"},
                                     {"CLAUDECODE": "1"}, {"CODEX_THREAD_ID": "thread"}])
def test_suppressed_check_does_not_prompt(tmp_path, setting):
    calls = run_shim(tmp_path, "t cd api", extra_env=setting)
    assert all("__update-prompt" not in call for call in calls)
    assert any(call.startswith("shell:cd:api") for call in calls)


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is required")
def test_stale_marker_from_parent_shell_does_not_suppress_check(tmp_path):
    calls = run_shim(tmp_path, "t cd api", extra_env={"T_UPDATE_PROMPTED": "1"})
    assert calls == [
        "bin:__update-prompt:marker=set",
        "shell:cd:api:marker=set",
        "after:marker=set",
    ]


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is required")
def test_current_shell_marker_suppresses_nested_check(tmp_path):
    calls = run_shim(tmp_path, "T_UPDATE_PROMPTED=$$; t cd api")
    assert calls == ["shell:cd:api:marker=set", "after:marker=set"]


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is required")
@pytest.mark.parametrize("flag", ["--dry-run", "--json", "--dump", "--statusline"])
def test_machine_or_dry_run_command_does_not_prompt(tmp_path, flag):
    calls = run_shim(tmp_path, f"t cd api {flag}")
    assert calls == [f"shell:cd:api {flag}:marker=", "after:marker="]


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is required")
def test_help_and_native_commands_do_not_prompt_in_shim(tmp_path):
    calls = run_shim(tmp_path, "t open --help; t ls; t update")
    assert calls == [
        "bin:open --help:marker=",
        "bin:ls:marker=",
        "bin:update:marker=",
        "reload",
        "after:marker=",
    ]


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is required")
def test_repos_checks_only_for_shell_navigation(tmp_path):
    calls = run_shim(tmp_path, "t repos ls; t repos cd api")
    assert calls == [
        "bin:repos ls:marker=",
        "bin:__update-prompt:marker=",
        "shell:repos-cd:api:marker=set",
        "after:marker=",
    ]


@pytest.mark.skipif(not shutil.which("zsh"), reason="zsh is required")
def test_nested_command_inherits_suppression(tmp_path):
    calls = run_shim(
        tmp_path,
        '_t_cd() { t open api; command t ls; }; t cd api',
    )
    assert calls == [
        "bin:__update-prompt:marker=",
        "shell:open:api:marker=set",
        "bin:ls:marker=set",
        "after:marker=",
    ]
