"""Independent desktop bridge checks using real shell routing and isolated tmux.

No desktop application or real agent is launched. The shared lifecycle fixture
provides disposable Git/configuration and a fake native agent executable.
"""

import fcntl
import json
import os
import pty
import shlex
import shutil
import subprocess
import struct
import sys
import termios

import pytest

from test_session_lifecycle import ROOT, _run, _shell, _until, lifecycle as lifecycle_environment


BRIDGE = ROOT / "libexec" / "t_bridge.py"


@pytest.fixture
def lifecycle(lifecycle_environment):
    base, env = lifecycle_environment
    # Session discovery can enumerate system-wide process tables. Hide those
    # unrelated agents while preserving real per-PID ownership checks.
    ps = base / "bin" / "ps"
    ps.write_text("#!/bin/sh\ncase \"$1\" in -Axo) exit 0 ;; esac\nexec " +
                  shlex.quote(shutil.which("ps")) + " \"$@\"\n")
    ps.chmod(0o755)
    # Avoid host-wide login startup files rewriting the supplied sandbox PATH.
    zsh = base / "bin" / "zsh"
    zsh.write_text("#!/bin/sh\nexec " + shlex.quote(shutil.which("zsh")) + " -d \"$@\"\n")
    zsh.chmod(0o755)
    native = base / "bin" / "open"
    native.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> "$T_TEST_APP_LOG"\nexit 99\n')
    native.chmod(0o755)
    yield base, env
    assert not (base / "app.log").exists(), "headless operation attempted native app delivery"


def _request(method, params=None, request_id=1):
    return {"version": 1, "id": request_id, "method": method, "params": params or {}}


def _bridge(base, env, requests):
    result = subprocess.run(
        [sys.executable, str(BRIDGE)], env=env, cwd=base,
        input="".join(json.dumps(request) + "\n" for request in requests),
        capture_output=True, text=True, timeout=40)
    assert result.returncode == 0, result.stderr
    frames = [json.loads(line) for line in result.stdout.splitlines()]
    assert all(frame["version"] == 1 for frame in frames)
    return frames


def _call(base, env, method, params=None):
    responses = [frame for frame in _bridge(base, env, [_request(method, params)])
                 if "event" not in frame]
    assert len(responses) == 1, responses
    return responses[0]


def _prepare(base, env, method="sessions.open", **arguments):
    response = _call(base, env, method, arguments)
    assert "error" not in response, response
    assert response["result"]["state"] == "prepared"
    return response["result"]["operationId"]


def _apply(base, env, operation_id):
    response = _call(base, env, "operations.apply", {"operationId": operation_id})
    assert "error" not in response, response
    return response["result"]


def _launches(base):
    path = base / "agents.log"
    return path.read_text().splitlines() if path.exists() else []


def _tmux(base, env, *arguments):
    return _run([str(base / "bin" / "tmux"), *arguments], env=env, cwd=base)


def _communicate_all(processes):
    results = []
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=40)
            results.append((process.returncode, stdout, stderr))
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)
    return results


def test_stdio_invalid_requests_do_not_corrupt_following_json(lifecycle):
    base, env = lifecycle
    lines = ["not-json", "[]", json.dumps({"version": 99, "id": "bad"}),
             json.dumps(_request("initialize", request_id="init")),
             json.dumps(_request("unknown.method", request_id="missing")),
             json.dumps(_request("repositories.list", request_id="repos"))]
    completed = subprocess.run([sys.executable, str(BRIDGE)], env=env, cwd=base,
                               input="\n".join(lines) + "\n", capture_output=True,
                               text=True, timeout=30)
    assert completed.returncode == 0, completed.stderr
    frames = [json.loads(line) for line in completed.stdout.splitlines()]
    assert len(frames) == len(lines)
    assert all(frame["version"] == 1 for frame in frames)
    assert [frame["error"]["code"] for frame in frames[:3]] == ["invalid_request"] * 3
    assert frames[3]["id"] == "init" and frames[3]["result"]["protocolVersion"] == 1
    assert frames[4]["id"] == "missing" and frames[4]["error"]["code"] == "method_not_found"
    assert frames[5]["result"]["repositories"] == [{"alias": "repo", "path": str(base / "repo")}]
    assert not _launches(base)


def test_bridge_uses_bundled_plugin_without_user_dotfiles(lifecycle):
    base, env = lifecycle
    (base / "home" / ".zshrc").unlink()
    (base / "home" / ".zshenv").unlink()
    # Debian zsh otherwise runs its first-use wizard before accepting terminal input.
    # Disable startup files in this no-dotfiles fixture, including that host wizard.
    wrapper = base / "bin" / "zsh"
    wrapper.write_text(wrapper.read_text().replace(" -d ", " -d -f "))
    env["SHELL"] = str(wrapper)
    with (base / "config" / "t" / "local.zsh").open("a") as config:
        config.write("DEV_OPEN_MODE_DEFAULT=app\n")
    snapshot = _call(base, env, "workspace.snapshot")
    assert "error" not in snapshot, snapshot
    assert snapshot["result"]["sessions"] == []
    assert snapshot["result"]["repositories"] == [{"alias": "repo", "path": str(base / "repo")}]
    operation = _prepare(base, env, repo="repo", new=True, agent="claude")
    record = _apply(base, env, operation)
    assert record["state"] == "completed"
    assert record["result"]["session"] == "dev-repo-1"
    assert record["attachment"]["argv"][:2] == ["zsh", "-lic"]
    assert record["attachment"]["validation"]["session"] == "dev-repo-1"
    internal_id = record["attachment"]["validation"]["identity"].split("\t")[1]
    assert "exec tmux attach-session -t " + shlex.quote(internal_id) in record["attachment"]["argv"][2]
    launches = _until(lambda: _launches(base))
    assert len(launches) == 1
    assert launches[0].split("|")[:2] == ["claude", str(base / "worktrees" / "repo" / "repo-1")]


def test_concurrent_bridge_processes_claim_one_operation(lifecycle):
    base, env = lifecycle
    operation = _prepare(base, env, repo="repo", slot="1", agent="codex")
    payload = json.dumps(_request("operations.apply", {"operationId": operation})) + "\n"
    processes = [subprocess.Popen([sys.executable, str(BRIDGE)], env=env, cwd=base,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True) for _ in range(2)]
    for process in processes:
        process.stdin.write(payload)
        process.stdin.close()
        process.stdin = None
    results = _communicate_all(processes)
    assert all(rc == 0 for rc, _, _ in results), results
    finals = [next(frame for frame in map(json.loads, stdout.splitlines()) if "event" not in frame)
              for _, stdout, _ in results]
    winners = [frame for frame in finals if "result" in frame]
    losers = [frame for frame in finals if "error" in frame]
    assert len(winners) == len(losers) == 1, finals
    assert winners[0]["result"]["state"] == "completed"
    assert losers[0]["error"]["code"] == "already_applied"
    assert len(_until(lambda: _launches(base))) == 1
    reconciled = _call(base, env, "operations.reconcile", {"operationId": operation})
    assert reconciled["result"]["result"]["session"] == "dev-repo-1"


def test_concurrent_cli_and_bridge_same_slot_keep_one_live_owner(lifecycle):
    base, env = lifecycle
    operation = _prepare(base, env, repo="repo", slot=1, agent="codex")
    bridge = subprocess.Popen([sys.executable, str(BRIDGE)], env=env, cwd=base,
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                              stderr=subprocess.PIPE, text=True)
    cli = subprocess.Popen(["zsh", "-lic", "t session open repo 1 --codex --cli"],
                           env=env, cwd=base, stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True)
    bridge.stdin.write(json.dumps(_request("operations.apply", {"operationId": operation})) + "\n")
    bridge.stdin.close()
    bridge.stdin = None
    results = _communicate_all([bridge, cli])
    assert results[0][0] == 0 and results[1][0] in (0, 1), results
    final = [json.loads(line) for line in results[0][1].splitlines() if "event" not in json.loads(line)][0]
    if "error" in final:
        # The shell lock can refuse the losing concurrent caller. The bridge
        # preserves uncertainty and blocks replay instead of launching again.
        assert final["error"]["code"] == "unknown_outcome", final
        assert results[1][0] == 0, results
        reconciled = _call(base, env, "operations.reconcile", {"operationId": operation})
        assert reconciled["result"]["state"] == "unknown"
        assert "attachment" not in reconciled["result"]
        assert _call(base, env, "operations.apply", {"operationId": operation})["error"]["code"] == "already_applied"
    else:
        assert final["result"]["result"]["session"] == "dev-repo-1"
    launches = _until(lambda: _launches(base))
    assert len(launches) == 1, (results, launches)
    sessions = _tmux(base, env, "list-sessions", "-F", "#{session_name}:#{session_path}")
    assert sessions.stdout.splitlines() == ["dev-repo-1:" + str(base / "worktrees" / "repo" / "repo-1")]
    attached = _shell(base, env, "t session open repo 1 --cli")
    assert attached.returncode == 0, attached.stderr
    assert _launches(base) == launches


def test_detaching_real_pty_client_preserves_agent_pane(lifecycle):
    base, env = lifecycle
    _apply(base, env, _prepare(base, env, repo="repo", slot=1, agent="codex"))
    _until(lambda: _launches(base))
    before = _tmux(base, env, "display-message", "-p", "-t", "=dev-repo-1:", "#{pane_pid}")
    assert before.returncode == 0 and before.stdout.strip().isdigit(), before.stderr
    master, slave = pty.openpty()
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 80, 0, 0))
    # A controlling terminal is required by the real tmux attach client.
    code = "import os,sys; os.setsid(); import fcntl,termios; fcntl.ioctl(0,termios.TIOCSCTTY,0); os.execv(sys.argv[1],sys.argv[1:])"
    # The fixture wrapper emulates noninteractive attaches; bypass just that
    # adapter while retaining its unique server socket and empty tmux config.
    wrapper = (base / "bin" / "tmux").read_text()
    real_command = shlex.split(wrapper.splitlines()[-1])[1:-1]
    client = subprocess.Popen([sys.executable, "-c", code, *real_command,
                               "attach-session", "-t", "=dev-repo-1:"],
                              env={**env, "TERM": "xterm-256color"}, cwd=base,
                              stdin=slave, stdout=slave, stderr=slave)
    os.close(slave)
    try:
        def attached_client():
            if client.poll() is not None:
                pytest.fail("tmux PTY attach failed: " + os.read(master, 65536).decode(errors="replace"))
            return _tmux(base, env, "list-clients", "-F", "#{client_pid}").stdout.strip()
        clients = _until(attached_client)
        assert client.poll() is None, clients
        os.write(master, b"\x02d")  # tmux's default prefix + detach
        client.wait(timeout=10)
        assert client.returncode == 0
        after = _tmux(base, env, "display-message", "-p", "-t", "=dev-repo-1:", "#{pane_pid}")
        assert after.stdout == before.stdout
        os.kill(int(after.stdout.strip()), 0)
        assert len(_launches(base)) == 1
    finally:
        if client.poll() is None:
            client.kill()
            client.wait(timeout=5)
        os.close(master)


def test_terminal_lifecycle_cannot_replay_after_start_or_finish(lifecycle):
    base, env = lifecycle
    prepared = _call(base, env, "operations.prepare", {"command": ["repo", "list"], "cwd": str(base)})
    operation = prepared["result"]["operationId"]
    ready = _apply(base, env, operation)
    assert ready["state"] == "ready" and ready["attachment"]["kind"] == "terminal"
    started = _call(base, env, "operations.started", {"operationId": operation})
    assert started["result"]["state"] == "running"
    for method in ("operations.apply", "operations.started"):
        assert _call(base, env, method, {"operationId": operation})["error"]["code"] == "already_applied"
    finished = _call(base, env, "operations.finished", {"operationId": operation, "exitCode": 0})
    assert finished["result"]["state"] == "completed"
    for method in ("operations.apply", "operations.started"):
        assert _call(base, env, method, {"operationId": operation})["error"]["code"] == "already_applied"
    assert _call(base, env, "operations.finished", {"operationId": operation, "exitCode": 0})["error"]["code"] == "invalid_operation"


def test_lost_terminal_apply_response_reconciles_without_replay(lifecycle):
    base, env = lifecycle
    prepared = _call(base, env, "operations.prepare", {"command": ["repo", "list"], "cwd": str(base)})
    operation = prepared["result"]["operationId"]
    lost = subprocess.run([sys.executable, str(BRIDGE)], env=env, cwd=base,
                          input=json.dumps(_request("operations.apply", {"operationId": operation})) + "\n",
                          stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, timeout=15)
    assert lost.returncode == 0, lost.stderr
    reconciled = _call(base, env, "operations.reconcile", {"operationId": operation})
    assert reconciled["result"]["state"] == "unknown"
    assert "attachment" not in reconciled["result"]
    assert _call(base, env, "operations.apply", {"operationId": operation})["error"]["code"] == "already_applied"
    assert not _launches(base)


def test_response_loss_after_real_launch_reconciles_unknown_and_blocks_replay(lifecycle):
    base, env = lifecycle
    operation = _prepare(base, env, repo="repo", slot=1, agent="codex")
    wrapper = base / "bin" / "tmux"
    lines = wrapper.read_text().splitlines()
    real_command = shlex.join(shlex.split(lines[-1])[1:-1])
    launched = base / "tmux-launched"
    release = base / "release-tmux"
    # Deliver the actual agent command, then hold the caller before its success
    # reaches the core. Killing only the bridge now loses a real effect/result.
    lines[-1] = ("if [ \"$1\" = send-keys ]; then\n" + real_command + ' "$@"\n' +
                 'launch_rc=$?\nif [ "$launch_rc" = 0 ]; then\n' +
                 "touch " + shlex.quote(str(launched)) + "\n" +
                 "while [ ! -e " + shlex.quote(str(release)) + " ]; do sleep 0.03; done\n" +
                 'fi\nexit "$launch_rc"\nfi\nexec ' + real_command + ' "$@"')
    wrapper.write_text("\n".join(lines) + "\n")
    process = subprocess.Popen([sys.executable, str(BRIDGE)], env=env, cwd=base,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        process.stdin.write(json.dumps(_request("operations.apply", {"operationId": operation})) + "\n")
        process.stdin.flush()
        _until(launched.exists)
        launches = _until(lambda: _launches(base))
        assert len(launches) == 1
        process.kill()
        release.touch()
        stdout, stderr = process.communicate(timeout=20)
        assert process.returncode < 0, stderr
        frames = [json.loads(line) for line in stdout.splitlines()]
        assert frames == [{"version": 1, "id": 1, "event": "progress",
                           "operationId": operation, "stage": "applying"}]
        reconciled = _call(base, env, "operations.reconcile", {"operationId": operation})
        assert reconciled["result"]["state"] == "unknown"
        assert "attachment" not in reconciled["result"]
        assert _call(base, env, "operations.apply", {"operationId": operation})["error"]["code"] == "already_applied"
        assert _tmux(base, env, "has-session", "-t", "=dev-repo-1:").returncode == 0
        assert _launches(base) == launches
    finally:
        release.touch()
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=20)


def test_concurrent_terminal_start_claims_one_attachment(lifecycle):
    base, env = lifecycle
    prepared = _call(base, env, "operations.prepare", {"command": ["repo", "list"], "cwd": str(base)})
    operation = prepared["result"]["operationId"]
    assert _apply(base, env, operation)["state"] == "ready"
    processes = [subprocess.Popen([sys.executable, str(BRIDGE)], env=env, cwd=base,
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                 stderr=subprocess.PIPE, text=True) for _ in range(2)]
    try:
        # Load both workers before claiming the same ready terminal operation.
        for process in processes:
            process.stdin.write(json.dumps(_request("initialize")) + "\n")
            process.stdin.flush()
            assert "result" in json.loads(process.stdout.readline())
        payload = json.dumps(_request("operations.started", {"operationId": operation})) + "\n"
        for process in processes:
            process.stdin.write(payload)
            process.stdin.flush()
        for process in processes:
            process.stdin.close()
            process.stdin = None
        results = _communicate_all(processes)
        assert all(rc == 0 for rc, _, _ in results), results
        finals = [json.loads(stdout) for _, stdout, _ in results]
        assert sum("result" in frame for frame in finals) == 1, finals
        assert sum(frame.get("error", {}).get("code") == "already_applied" for frame in finals) == 1, finals
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=5)


@pytest.mark.parametrize("restart_server", [False, True], ids=["same-server", "new-server"])
def test_delayed_attachment_refuses_same_name_replacement(lifecycle, restart_server):
    base, env = lifecycle
    # Keep the server alive for the same-server variant, then exercise internal
    # ID reuse by explicitly replacing the entire server in the other variant.
    keeper = _tmux(base, env, "new-session", "-d", "-s", "attachment-keeper", "-c", str(base))
    assert keeper.returncode == 0, keeper.stderr
    opened = _apply(base, env, _prepare(base, env, repo="repo", slot=1, agent="claude"))
    descriptor = opened["attachment"]
    original = descriptor["validation"]
    launches = _until(lambda: _launches(base))
    assert len(launches) == 1
    original_sid = _tmux(base, env, "show-environment", "-t", "=dev-repo-1", "CLAUDE_RESUME_ID")
    assert original_sid.returncode == 0 and original_sid.stdout.startswith("CLAUDE_RESUME_ID="), original_sid.stderr
    assert _call(base, env, "attachments.validate", {"validation": original})["result"] == {"valid": True}

    removed = _tmux(base, env, "kill-server") if restart_server else _tmux(
        base, env, "kill-session", "-t", "=dev-repo-1:")
    assert removed.returncode == 0, removed.stderr
    worktree = base / "worktrees" / "repo" / "repo-1"
    replacement = _tmux(base, env, "new-session", "-d", "-s", "dev-repo-1", "-c", str(worktree), "sleep 60")
    assert replacement.returncode == 0, replacement.stderr
    replacement_sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert replacement_sid not in original_sid.stdout
    stamped = _tmux(base, env, "set-environment", "-t", "=dev-repo-1", "CLAUDE_RESUME_ID", replacement_sid)
    assert stamped.returncode == 0, stamped.stderr
    current = _call(base, env, "sessions.attach", {"repo": "repo", "slot": 1})
    assert "error" not in current, current
    current_identity = current["result"]["attachment"]["validation"]["identity"]
    assert current_identity != original["identity"]
    if restart_server:
        assert current_identity.split("\t")[0] != original["identity"].split("\t")[0]
    else:
        assert current_identity.split("\t")[1] != original["identity"].split("\t")[1]

    stale = _call(base, env, "attachments.validate", {"validation": original})
    assert stale["error"]["code"] == "stale_attachment", stale
    # The recorded shell also guards against replacement independently of the
    # claim check, so a delayed descriptor cannot reach even our attach adapter.
    delayed = _run(descriptor["argv"], env=env, cwd=descriptor["cwd"])
    assert delayed.returncode == 1, (delayed.stdout, delayed.stderr)
    assert "attachment changed" in delayed.stderr
    assert _tmux(base, env, "list-clients", "-F", "#{client_pid}").stdout.strip() == ""
    assert _tmux(base, env, "has-session", "-t", "=dev-repo-1:").returncode == 0
    assert _launches(base) == launches


def _slot_identity(base, env):
    identity = _tmux(base, env, "display-message", "-p", "-t", "=dev-repo-1:",
                     "#{pid}\t#{session_id}\t#{pane_id}\t#{pane_pid}\t#{session_path}")
    assert identity.returncode == 0, identity.stderr
    sid = _tmux(base, env, "show-environment", "-t", "=dev-repo-1", "CLAUDE_RESUME_ID")
    assert sid.returncode == 0 and sid.stdout.startswith("CLAUDE_RESUME_ID="), sid.stderr
    return identity.stdout.strip(), sid.stdout.strip().split("=", 1)[1]


def _save_claude_thread(base, sid):
    worktree = base / "worktrees" / "repo" / "repo-1"
    project = base / "home" / ".claude" / "projects" / "".join(
        character if character.isalnum() else "-" for character in str(worktree))
    project.mkdir(parents=True, exist_ok=True)
    (project / (sid + ".jsonl")).write_text(json.dumps({"type": "user", "message": {
        "role": "user", "content": "Isolated lifecycle regression"}}, separators=(",", ":")) + "\n")


@pytest.mark.parametrize("method", ["sessions.close", "sessions.restart"], ids=["close", "restart"])
@pytest.mark.parametrize("restart_server", [False, True], ids=["same-server", "new-server"])
def test_prepared_mutation_refuses_replacement_live_owner(lifecycle, method, restart_server):
    base, env = lifecycle
    keeper = _tmux(base, env, "new-session", "-d", "-s", "mutation-keeper", "-c", str(base))
    assert keeper.returncode == 0, keeper.stderr
    _apply(base, env, _prepare(base, env, repo="repo", slot=1, agent="claude"))
    _until(lambda: _launches(base))
    original_identity, original_sid = _slot_identity(base, env)
    _save_claude_thread(base, original_sid)
    operation = _prepare(base, env, method, repo="repo", slot=1, sid=original_sid)

    removed = _tmux(base, env, "kill-server") if restart_server else _tmux(
        base, env, "kill-session", "-t", "=dev-repo-1:")
    assert removed.returncode == 0, removed.stderr
    replacement_sid = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    assert replacement_sid != original_sid
    _save_claude_thread(base, replacement_sid)
    worktree = base / "worktrees" / "repo" / "repo-1"
    replacement = _tmux(base, env, "new-session", "-d", "-s", "dev-repo-1", "-c", str(worktree),
                        "exec " + shlex.join([str(base / "bin" / "claude"), "-r", replacement_sid]))
    assert replacement.returncode == 0, replacement.stderr
    for key, value in (("CLAUDE_RESUME_ID", replacement_sid), ("DEV_AGENT", "claude")):
        stamped = _tmux(base, env, "set-environment", "-t", "=dev-repo-1", key, value)
        assert stamped.returncode == 0, stamped.stderr
    launches = _until(lambda: _launches(base) if len(_launches(base)) == 2 else None)
    replacement_identity, recorded_sid = _slot_identity(base, env)
    assert replacement_identity != original_identity and recorded_sid == replacement_sid
    pane_pid = int(replacement_identity.split("\t")[3])

    applied = _call(base, env, "operations.apply", {"operationId": operation})
    assert applied["error"]["code"] == "stale_target", applied
    reconciled = _call(base, env, "operations.reconcile", {"operationId": operation})
    assert reconciled["result"]["state"] == "rejected", reconciled
    assert _slot_identity(base, env) == (replacement_identity, replacement_sid)
    os.kill(pane_pid, 0)
    assert _launches(base) == launches
    assert _call(base, env, "operations.apply", {"operationId": operation})["error"]["code"] == "already_applied"
    assert _slot_identity(base, env) == (replacement_identity, replacement_sid)
    assert _launches(base) == launches


def test_prepared_restart_preserves_unchanged_conversation(lifecycle):
    base, env = lifecycle
    _apply(base, env, _prepare(base, env, repo="repo", slot=1, agent="claude"))
    _until(lambda: _launches(base))
    original_identity, original_sid = _slot_identity(base, env)
    _save_claude_thread(base, original_sid)
    operation = _prepare(base, env, "sessions.restart", repo="repo", slot=1, sid=original_sid)
    restarted = _apply(base, env, operation)
    assert restarted["state"] == "completed", restarted
    launches = _until(lambda: _launches(base) if len(_launches(base)) == 2 else None)
    assert launches[1].endswith("|-r|" + original_sid)
    current_identity, current_sid = _slot_identity(base, env)
    assert current_sid == original_sid
    assert current_identity.split("\t")[3] != original_identity.split("\t")[3]
    assert restarted["result"]["session"] == "dev-repo-1"
    assert restarted["attachment"]["validation"]["identity"] == current_identity
