"""Desktop release checks use a fake process table and a fake Codex proxy."""

import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


SID = "01234567-89ab-cdef-0123-456789abcdef"
MODULE = Path(__file__).resolve().parent.parent / "libexec" / "t_app_handoff.py"


@pytest.fixture
def handoff():
    spec = importlib.util.spec_from_file_location("t_app_handoff", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def fake_proxy(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    for key, folder in (("XDG_CONFIG_HOME", "config"), ("XDG_CACHE_HOME", "cache"), ("XDG_STATE_HOME", "state")):
        monkeypatch.setenv(key, str(tmp_path / folder))
    executable = tmp_path / "codex"
    executable.write_text(f"#!{sys.executable}\n" + '''
import json, os, sys
assert sys.argv[1:] == ['app-server', 'proxy']
with open(os.environ['T_TEST_RPC_LOG'], 'a') as log:
    for line in sys.stdin:
        request = json.loads(line)
        log.write(json.dumps(request) + '\\n')
        log.flush()
        if os.environ.get('T_TEST_PROXY_MODE') == 'exit':
            sys.exit(0)
        if request.get('method') == 'initialize':
            result = {}
        elif request.get('method') == 'initialized':
            continue
        elif request.get('method') == 'thread/read':
            result = {'thread': {'id': os.environ['T_TEST_SID'],
                                 'cwd': os.environ['T_TEST_CWD'],
                                 'status': {'type': os.environ['T_TEST_STATUS']}}}
            if os.environ.get('T_TEST_PROXY_MODE') == 'error':
                print(json.dumps({'id': request['id'], 'error': {'message': 'refused'}}), flush=True)
                continue
        else:
            raise AssertionError(request)
        print('not-json')
        print(json.dumps({'method': 'unrelated/notification'}))
        print(json.dumps({'id': request['id'], 'result': result}), flush=True)
''')
    executable.chmod(0o755)
    log = tmp_path / "rpc.jsonl"
    monkeypatch.setenv("PATH", str(tmp_path) + os.pathsep + os.environ.get("PATH", ""))
    monkeypatch.setenv("T_TEST_RPC_LOG", str(log))
    monkeypatch.setenv("T_TEST_SID", SID)
    monkeypatch.setenv("T_TEST_CWD", str(tmp_path / "worktree"))
    monkeypatch.setenv("T_TEST_STATUS", "idle")
    return log


def process_table(*entries):
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, "\n".join(entries), "")
    return run, calls


def test_refuses_gui_and_bundled_backend_without_rpc(handoff, tmp_path, monkeypatch):
    bundle = tmp_path / "ChatGPT.app"
    backend = bundle / "Contents" / "Resources" / "codex"
    run, calls = process_table(f"123 {backend} {backend} app-server")
    monkeypatch.setattr(handoff, "_thread_status", lambda *args: pytest.fail("must not connect"))
    with pytest.raises(ValueError, match="quit the Codex desktop app"):
        handoff.assert_released(str(bundle), SID, str(tmp_path), run)
    assert len(calls) == 1


def test_missing_bundle_still_detects_historical_app_names(handoff, monkeypatch):
    run, _ = process_table("88 /Applications/Codex.app/Contents/MacOS/Codex Codex")
    monkeypatch.setattr(handoff, "_thread_status", lambda *args: pytest.fail("must not connect"))
    with pytest.raises(ValueError, match="still running"):
        handoff.assert_released(None, SID, "/tmp/worktree", run)


@pytest.mark.parametrize("table,returncode", [
    ("88 /Applications/Codex.app/Contents/MacOS/Codex Codex", 0),
    ("bad process line", 0),
    ("", 1),
])
def test_open_retry_stays_with_the_command_user_ran(handoff, table, returncode):
    retry = "Finish the turn and quit the Codex desktop app on this Mac, then retry: t open api 13 --cli"
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, returncode, table, "")
    with pytest.raises(ValueError) as error:
        handoff.assert_released(None, SID, "/tmp/worktree", run, retry=retry)
    assert str(error.value).endswith(retry)
    assert "t app pull" not in str(error.value)


def test_no_gui_or_daemon_is_released(handoff, tmp_path, monkeypatch):
    run, calls = process_table("42 /usr/bin/python3 python3 unrelated.py")
    monkeypatch.setattr(handoff, "_thread_status", lambda *args: pytest.fail("must not connect"))
    handoff.assert_released(str(tmp_path / "ChatGPT.app"), SID, str(tmp_path), run)
    assert len(calls) == 2


def test_daemon_with_global_cli_options_is_checked(handoff, monkeypatch):
    checked = []
    run, _ = process_table('44 /opt/bin/codex --config profile=work app-server --listen unix://')
    monkeypatch.setattr(handoff, "_thread_status", lambda *args: checked.append(args))
    handoff.assert_released(None, SID, "/worktree", run)
    assert checked == [(SID, "/worktree")]


@pytest.mark.parametrize("status,allowed", [
    ("idle", True), ("notLoaded", True), ("active", False), ("systemError", False),
])
def test_shared_daemon_requires_exact_idle_thread(handoff, fake_proxy, tmp_path,
                                                   monkeypatch, status, allowed):
    monkeypatch.setenv("T_TEST_STATUS", status)
    cwd = str(tmp_path / "worktree")
    daemon = "44 /opt/homebrew/bin/codex /opt/homebrew/bin/codex app-server --listen unix:// --managed-daemon"
    run, _ = process_table(daemon)
    if allowed:
        handoff.assert_released(str(tmp_path / "ChatGPT.app"), SID, cwd, run)
    else:
        with pytest.raises(ValueError, match="still active or its status is unknown"):
            handoff.assert_released(str(tmp_path / "ChatGPT.app"), SID, cwd, run)
    requests = [json.loads(line) for line in fake_proxy.read_text().splitlines()]
    assert [request["method"] for request in requests] == ["initialize", "initialized", "thread/read"]
    assert requests[2]["params"] == {"threadId": SID, "includeTurns": False}


@pytest.mark.parametrize("wrong", ["sid", "cwd"])
def test_shared_daemon_rejects_wrong_thread(handoff, fake_proxy, tmp_path, monkeypatch, wrong):
    cwd = str(tmp_path / "worktree")
    monkeypatch.setenv("T_TEST_" + wrong.upper(), "elsewhere")
    run, _ = process_table("44 /opt/homebrew/bin/codex /opt/homebrew/bin/codex app-server --managed-daemon")
    with pytest.raises(ValueError, match="different thread or worktree"):
        handoff.assert_released(str(tmp_path / "ChatGPT.app"), SID, cwd, run)


@pytest.mark.parametrize("mode,reason", [("exit", "exited before"), ("error", "refused")])
def test_unverifiable_daemon_fails_closed(handoff, fake_proxy, tmp_path, monkeypatch, mode, reason):
    monkeypatch.setenv("T_TEST_PROXY_MODE", mode)
    run, _ = process_table("44 /opt/bin/codex-aarch64-apple-darwin codex-aarch64-apple-darwin app-server daemon run")
    with pytest.raises(ValueError, match=reason):
        handoff.assert_released(None, SID, str(tmp_path / "worktree"), run)


def test_gui_reopening_during_probe_blocks_pull(handoff, tmp_path, monkeypatch):
    bundle = tmp_path / "ChatGPT.app"
    seen = 0
    def run(argv, **kwargs):
        nonlocal seen
        seen += 1
        line = "" if seen == 1 else f"9 {bundle}/Contents/MacOS/ChatGPT ChatGPT"
        return subprocess.CompletedProcess(argv, 0, line, "")
    with pytest.raises(ValueError, match="opened during the status check"):
        handoff.assert_released(str(bundle), SID, str(tmp_path), run)


def test_process_scan_failure_is_closed(handoff, tmp_path):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "denied")
    with pytest.raises(ValueError, match="could not inspect"):
        handoff.assert_released(str(tmp_path / "ChatGPT.app"), SID, str(tmp_path), run)


def test_unparseable_process_scan_is_closed(handoff, tmp_path):
    run, _ = process_table("bad process line")
    with pytest.raises(ValueError, match="could not parse"):
        handoff.assert_released(None, SID, str(tmp_path), run)


def test_rpc_timeout_and_oversize_fail_closed(handoff, monkeypatch):
    class Output:
        def fileno(self):
            return 10
    class Process:
        stdout = Output()
    with pytest.raises(ValueError, match="timed out"):
        handoff._read_response(Process(), 1, handoff.time.monotonic() - 1)
    monkeypatch.setattr(handoff.select, "select", lambda *args: ([Output()], [], []))
    monkeypatch.setattr(handoff.os, "read", lambda *args: b"x" * (1024 * 1024 + 1))
    with pytest.raises(ValueError, match="too large"):
        handoff._read_response(Process(), 1, handoff.time.monotonic() + 1)


def test_proxy_cleanup_reaps_child_that_ignores_terminate(handoff, monkeypatch):
    class Pipe:
        def write(self, data):
            pass
        def flush(self):
            pass
        def close(self):
            pass
    class Process:
        stdin = Pipe()
        stdout = Pipe()
        killed = False
        def terminate(self):
            raise ProcessLookupError
        def wait(self, timeout):
            if not self.killed:
                raise subprocess.TimeoutExpired("proxy", timeout)
        def kill(self):
            self.killed = True
    process = Process()
    monkeypatch.setattr(handoff.subprocess, "Popen", lambda *args, **kwargs: process)
    monkeypatch.setattr(handoff, "_read_response", lambda *args: ({}, b""))
    with pytest.raises(ValueError, match="different thread"):
        handoff._thread_status(SID, "/tmp/worktree")
    assert process.killed
