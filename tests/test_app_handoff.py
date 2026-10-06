"""Desktop handoff checks use isolated Codex state and synthetic writer locks."""

from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest


SID = "01234567-89ab-cdef-0123-456789abcdef"
OTHER = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
MODULE = Path(__file__).resolve().parent.parent / "libexec" / "t_app_handoff.py"


@pytest.fixture
def handoff():
    spec = importlib.util.spec_from_file_location("t_app_handoff", MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def target(tmp_path, monkeypatch):
    home = tmp_path / "codex"
    locks = home / "thread-writer-locks"
    locks.mkdir(parents=True)
    cwd = tmp_path / "worktree"
    cwd.mkdir()
    rollout = home / "sessions" / "2026" / "10" / "05" / f"rollout-{SID}.jsonl"
    rollout.parent.mkdir(parents=True)
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": SID, "cwd": str(cwd), "source": "cli", "cli_version": "0.160.0"}}) + "\n")
    (locks / f"{SID}.lock").touch()
    monkeypatch.setenv("HOME", str(tmp_path))
    for key in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME"):
        folder = tmp_path / key.lower()
        folder.mkdir()
        monkeypatch.setenv(key, str(folder))
    return {"home": home, "cwd": cwd, "rollout": rollout, "locks": locks}


def options(target, **kwargs):
    result = dict(codex_home=str(target["home"]), rollout=str(target["rollout"]))
    result.update(kwargs)
    return result


def old_archived_target(target, *, id=SID, cwd=None, archived=1, rollout_path=None):
    """Create the old-history archive shape observed with Codex 0.159/0.160."""
    target["rollout"].write_text(json.dumps({"type": "session_meta", "payload": {
        "id": SID, "cwd": str(target["cwd"]), "cli_version": "0.158.0"}}) + "\n")
    archive = target["home"] / "archived_sessions" / target["rollout"].name
    archive.parent.mkdir()
    target["rollout"].replace(archive)
    (target["locks"] / f"{SID}.lock").unlink()
    db = sqlite3.connect(target["home"] / "state_5.sqlite")
    db.execute("create table threads (id text, cwd text, archived integer, rollout_path text)")
    db.execute("insert into threads values (?, ?, ?, ?)",
               (id, str(cwd or target["cwd"]), archived, str(rollout_path or archive)))
    db.commit()
    db.close()
    return archive


def process_table(*entries, lsof_pids=(), gitdir=None):
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        if argv[0] == "ps":
            return subprocess.CompletedProcess(argv, 0, "\n".join(entries), "")
        if argv[0] == "lsof":
            text = "\n".join(map(str, lsof_pids))
            return subprocess.CompletedProcess(argv, 0 if text else 1, text, "")
        if argv[0] == "git":
            text = str(gitdir) if gitdir else ""
            return subprocess.CompletedProcess(argv, 0 if text else 1, text, "")
        raise AssertionError(argv)
    return run, calls


@contextmanager
def locked(path, tmp_path):
    """A separate process owns the real advisory lock, as Codex would."""
    code = ("import fcntl,sys; f=open(sys.argv[1], 'r'); "
            "fcntl.flock(f, fcntl.LOCK_EX); print('ready', flush=True); sys.stdin.read()")
    env = {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", os.defpath)}
    process = subprocess.Popen([sys.executable, "-c", code, str(path)],
                               env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    assert process.stdout.readline().strip() == "ready"
    try:
        yield process.pid
    finally:
        process.stdin.close()
        process.wait(timeout=3)
        process.stdout.close()
        process.stderr.close()


def test_free_target_lock_allows_app_and_unrelated_backends(handoff, target):
    run, calls = process_table(
        "10 /Applications/ChatGPT.app/Contents/MacOS/ChatGPT",
        "11 /Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex app-server --listen stdio://")
    handoff.assert_released(None, SID, str(target["cwd"]), run, **options(target))
    assert calls == []  # Modern per-thread proof does not inspect unrelated processes.


def test_selected_lock_blocks_and_suggests_archive_for_reserved_worktree(handoff, target, tmp_path):
    private = tmp_path / "private-git"
    private.mkdir()
    (private / "t-app-slot").write_text("codex-app\n")
    (target["cwd"] / ".git").write_text("gitdir: ../private-git\n")
    run, calls = process_table(gitdir=private)
    with locked(target["locks"] / f"{SID}.lock", tmp_path):
        with pytest.raises(ValueError, match="Archive it.*app can stay open") as error:
            handoff.assert_released(None, SID, str(target["cwd"]), run, **options(target))
    assert "t will restore it" in str(error.value)
    assert any(call[0] == "git" for call in calls)
    assert not any(call[0] == "ps" for call in calls)


@pytest.mark.parametrize("unsafe", ["no_marker", "codex_metadata", "codex_worktree", "archived"])
def test_selected_lock_avoids_unsafe_archive_hint(handoff, target, tmp_path, unsafe):
    private = tmp_path / "private-git"
    private.mkdir()
    if unsafe != "no_marker":
        (private / "t-app-slot").touch()
    if unsafe == "codex_metadata":
        (private / "codex-thread.json").write_text("{}")
    (target["cwd"] / ".git").write_text("gitdir: ../private-git\n")
    cwd = target["cwd"]
    if unsafe == "codex_worktree":
        cwd = tmp_path / "codex-worktrees" / "bad"
        cwd.mkdir(parents=True)
        (cwd / ".git").write_text("gitdir: ../../private-git\n")
        target["rollout"].write_text(json.dumps({"type": "session_meta", "payload": {
            "id": SID, "cwd": str(cwd)}}) + "\n")
    if unsafe == "archived":
        archive = target["home"] / "archived_sessions" / target["rollout"].name
        archive.parent.mkdir()
        target["rollout"].replace(archive)
        target["rollout"] = archive
    run, _ = process_table(gitdir=private)
    with locked(target["locks"] / f"{SID}.lock", tmp_path):
        with pytest.raises(ValueError, match="writer lock is held") as error:
            handoff.assert_released(None, SID, str(cwd), run,
                                    **options(target, archived=unsafe == "archived"))
    assert "Archive it" not in str(error.value)
    assert "background backend" in str(error.value)


def test_modern_missing_target_lock_allows_unloaded_thread_with_other_backend(handoff, target):
    (target["locks"] / f"{SID}.lock").unlink()
    live, _ = process_table("44 /opt/bin/codex app-server --listen stdio://")
    handoff.assert_released(None, SID, str(target["cwd"]), live, **options(target))


def test_archived_target_missing_lock_ignores_unrelated_live_backends(handoff, target):
    archive = target["home"] / "archived_sessions" / target["rollout"].name
    archive.parent.mkdir()
    target["rollout"].replace(archive)
    (target["locks"] / f"{SID}.lock").unlink()
    run, calls = process_table("44 /opt/bin/codex app-server --listen stdio://")
    handoff.assert_released(None, SID, str(target["cwd"]), run,
                            **options(target, rollout=str(archive), archived=True))
    assert calls == []


def test_old_archived_history_release_ignores_unrelated_backend(handoff, target):
    archive = old_archived_target(target)
    run, calls = process_table("44 /opt/bin/codex app-server --listen stdio://")
    handoff.assert_released(None, SID, str(target["cwd"]), run,
                            **options(target, rollout=str(archive), archived=True))
    assert calls == [["lsof", "-nP", "-t", str(archive)]]


@pytest.mark.parametrize("wrong", ["id", "cwd", "archived", "path", "duplicate", "missing_db"])
def test_old_archived_history_requires_exact_database_row(handoff, target, wrong):
    arguments = {}
    if wrong == "id":
        arguments["id"] = OTHER
    elif wrong == "cwd":
        arguments["cwd"] = "/another/worktree"
    elif wrong == "archived":
        arguments["archived"] = 0
    elif wrong == "path":
        arguments["rollout_path"] = "/another/rollout.jsonl"
    archive = old_archived_target(target, **arguments)
    if wrong == "duplicate":
        db = sqlite3.connect(target["home"] / "state_5.sqlite")
        db.execute("insert into threads values (?, ?, ?, ?)",
                   (SID, str(target["cwd"]), 1, str(archive)))
        db.commit()
        db.close()
    elif wrong == "missing_db":
        (target["home"] / "state_5.sqlite").unlink()
    run, calls = process_table("44 /opt/bin/codex app-server --listen stdio://")
    with pytest.raises(ValueError, match="release could not be verified"):
        handoff.assert_released(None, SID, str(target["cwd"]), run,
                                **options(target, rollout=str(archive), archived=True))
    assert calls == []


def test_old_archived_history_open_file_descriptor_blocks(handoff, target, tmp_path):
    original = target["rollout"]
    code = ("import sys; f=open(sys.argv[1]); print('ready',flush=True); sys.stdin.read()")
    env = {"HOME": str(tmp_path), "PATH": os.environ.get("PATH", os.defpath)}
    process = subprocess.Popen([sys.executable, "-c", code, str(original)], env=env,
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True)
    try:
        assert process.stdout.readline().strip() == "ready"
        archive = old_archived_target(target)
        def run(argv, **kwargs):
            assert argv[0] == "lsof"
            return subprocess.run(argv, capture_output=True, text=True, timeout=5)
        with pytest.raises(ValueError, match="still open"):
            handoff.assert_released(None, SID, str(target["cwd"]), run,
                                    **options(target, rollout=str(archive), archived=True))
    finally:
        process.stdin.close()
        process.wait(timeout=3)
        process.stdout.close()
        process.stderr.close()


@pytest.mark.parametrize("code,stdout,stderr", [(2, "", "denied"), (1, "", "denied"),
                                                 (0, "", ""), (1, "123", "")])
def test_old_archived_history_lsof_errors_fail_closed(handoff, target, code, stdout, stderr):
    archive = old_archived_target(target)
    def run(argv, **kwargs):
        assert argv[0] == "lsof"
        return subprocess.CompletedProcess(argv, code, stdout, stderr)
    with pytest.raises(ValueError, match="could not reliably inspect"):
        handoff.assert_released(None, SID, str(target["cwd"]), run,
                                **options(target, rollout=str(archive), archived=True))


@pytest.mark.parametrize("version", ["0.158.0", "unknown"])
def test_missing_lock_contract_fails_closed_for_older_or_unknown_version(handoff, target, version):
    (target["locks"] / f"{SID}.lock").unlink()
    target["rollout"].write_text(json.dumps({"type": "session_meta", "payload": {
        "id": SID, "cwd": str(target["cwd"]), "cli_version": version}}) + "\n")
    idle, calls = process_table()
    handoff.assert_released(None, SID, str(target["cwd"]), idle, **options(target))
    assert len(calls) == 1
    live, _ = process_table("44 /opt/bin/codex app-server --listen stdio://")
    with pytest.raises(ValueError, match="no verifiable writer lock"):
        handoff.assert_released(None, SID, str(target["cwd"]), live, **options(target))


def test_old_active_history_suggests_archive_in_reserved_worktree(handoff, target, tmp_path):
    (target["locks"] / f"{SID}.lock").unlink()
    target["rollout"].write_text(json.dumps({"type": "session_meta", "payload": {
        "id": SID, "cwd": str(target["cwd"]), "cli_version": "0.158.0"}}) + "\n")
    private = tmp_path / "private-git"
    private.mkdir()
    (private / "t-app-slot").write_text("codex-app\n")
    (target["cwd"] / ".git").write_text("gitdir: ../private-git\n")
    run, _ = process_table("44 /opt/bin/codex app-server --listen stdio://", gitdir=private)
    with pytest.raises(ValueError, match="Archive it.*app can stay open"):
        handoff.assert_released(None, SID, str(target["cwd"]), run, **options(target))


def test_cli_pid_must_hold_exact_selected_lock(handoff, target, tmp_path):
    path = target["locks"] / f"{SID}.lock"
    with locked(path, tmp_path) as owner:
        wrong, _ = process_table(lsof_pids=(owner,))
        with pytest.raises(ValueError, match="selected Codex conversation is still loaded"):
            handoff.assert_released(None, SID, str(target["cwd"]), wrong,
                                    **options(target, cli_pid=owner + 1))
        handoff.assert_released(None, SID, str(target["cwd"]), wrong,
                                **options(target, cli_pid=owner))


@pytest.mark.parametrize("sibling_cwd,allowed", [("same", False), ("other", True)])
def test_other_held_thread_is_checked_by_exact_home_db(handoff, target, tmp_path,
                                                      sibling_cwd, allowed):
    db = sqlite3.connect(target["home"] / "state_5.sqlite")
    db.execute("create table threads (id text, cwd text, archived integer)")
    sibling = str(target["cwd"]) if sibling_cwd == "same" else str(tmp_path / "other")
    db.execute("insert into threads values (?, ?, 0)", (OTHER, sibling))
    db.commit()
    db.close()
    path = target["locks"] / f"{OTHER}.lock"
    path.touch()
    run, calls = process_table("44 /opt/bin/codex app-server --listen stdio://")
    with locked(path, tmp_path):
        if allowed:
            handoff.assert_released(None, SID, str(target["cwd"]), run, **options(target))
        else:
            with pytest.raises(ValueError, match="another Codex conversation in this worktree"):
                handoff.assert_released(None, SID, str(target["cwd"]), run, **options(target))
    assert calls == []


def test_unknown_held_sibling_fails_closed(handoff, target, tmp_path):
    path = target["locks"] / f"{OTHER}.lock"
    path.touch()
    run, _ = process_table()
    with locked(path, tmp_path):
        with pytest.raises(ValueError, match="could not be matched to its worktree"):
            handoff.assert_released(None, SID, str(target["cwd"]), run, **options(target))


def test_lock_file_symlink_is_rejected(handoff, target):
    path = target["locks"] / f"{SID}.lock"
    path.unlink()
    path.symlink_to(target["rollout"])
    run, _ = process_table()
    with pytest.raises(ValueError, match="not a regular file"):
        handoff.assert_released(None, SID, str(target["cwd"]), run, **options(target))


@pytest.mark.parametrize("wrong", ["id", "cwd", "malformed", "missing"])
def test_rollout_identity_is_required(handoff, target, wrong):
    if wrong == "id":
        payload = {"id": OTHER, "cwd": str(target["cwd"])}
    elif wrong == "cwd":
        payload = {"id": SID, "cwd": "/elsewhere"}
    elif wrong == "malformed":
        target["rollout"].write_text("not JSON\n")
        payload = None
    else:
        target["rollout"].unlink()
        payload = None
    if payload:
        target["rollout"].write_text(json.dumps({"type": "session_meta", "payload": payload}) + "\n")
    run, _ = process_table()
    with pytest.raises(ValueError, match="rollout"):
        handoff.assert_released(None, SID, str(target["cwd"]), run, **options(target))


def test_legacy_scan_failure_is_closed(handoff, target):
    (target["locks"] / f"{SID}.lock").unlink()
    target["rollout"].write_text(json.dumps({"type": "session_meta", "payload": {
        "id": SID, "cwd": str(target["cwd"]), "cli_version": "0.158.0"}}) + "\n")
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, "", "denied")
    with pytest.raises(ValueError, match="could not inspect"):
        handoff.assert_released(None, SID, str(target["cwd"]), run, **options(target))


def test_private_unarchive_restores_exact_saved_thread(handoff, target, tmp_path, monkeypatch):
    archive = target["home"] / "archived_sessions" / target["rollout"].name
    archive.parent.mkdir()
    target["rollout"].replace(archive)
    target["rollout"] = archive
    active = target["home"] / "sessions" / "2026" / "10" / "05" / archive.name
    log = tmp_path / "rpc.jsonl"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    executable = bin_dir / "codex"
    executable.write_text(f"#!{sys.executable}\n" + '''
import json, os, pathlib, sys
assert sys.argv[1:] == ['app-server', '--disable', 'plugins', '--listen', 'stdio://']
assert os.environ['CODEX_HOME'] == os.environ['T_TEST_CODEX_HOME']
with open(os.environ['T_TEST_RPC_LOG'], 'a') as log:
    for line in sys.stdin:
        request = json.loads(line)
        log.write(json.dumps(request) + '\\n')
        log.flush()
        if 'id' not in request:
            continue
        method = request['method']
        if method == 'initialize':
            result = {}
        elif method == 'thread/unarchive':
            pathlib.Path(os.environ['T_TEST_ARCHIVE']).replace(os.environ['T_TEST_ACTIVE'])
            result = {'thread': {'id': os.environ['T_TEST_SID']}}
        elif method == 'thread/read':
            result = {'thread': {'id': os.environ['T_TEST_SID'],
                                 'cwd': os.path.realpath(os.environ['T_TEST_CWD']),
                                 'path': os.environ['T_TEST_ACTIVE']}}
        else:
            raise AssertionError(method)
        print(json.dumps({'id': request['id'], 'result': result}), flush=True)
''')
    executable.chmod(0o755)
    monkeypatch.setenv("PATH", str(bin_dir) + os.pathsep + os.environ.get("PATH", ""))
    for name, value in (("T_TEST_CODEX_HOME", target["home"]), ("T_TEST_RPC_LOG", log),
                        ("T_TEST_ARCHIVE", archive), ("T_TEST_ACTIVE", active),
                        ("T_TEST_SID", SID), ("T_TEST_CWD", target["cwd"])):
        monkeypatch.setenv(name, str(value))
    handoff.unarchive_thread(SID, str(target["cwd"]), codex_home=str(target["home"]),
                             rollout=str(archive))
    assert active.is_file() and not archive.exists()
    requests = [json.loads(line) for line in log.read_text().splitlines()]
    assert [request["method"] for request in requests] == [
        "initialize", "initialized", "thread/unarchive", "thread/read"]
    assert requests[2]["params"] == {"threadId": SID}


def test_unarchive_wrong_rollout_refused_before_starting_process(handoff, target, monkeypatch):
    monkeypatch.setattr(handoff.subprocess, "Popen", lambda *args, **kwargs: pytest.fail("must not start"))
    with pytest.raises(ValueError, match="another thread"):
        handoff.unarchive_thread(OTHER, str(target["cwd"]),
                                 codex_home=str(target["home"]), rollout=str(target["rollout"]))


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


@pytest.mark.parametrize("command", [
    "/Applications/ChatGPT.app/Contents/Resources/cua_node/bin/node bridge.js",
    "/Applications/ChatGPT.app/Contents/Resources/codex-cli/bin/codex-code-mode-host",
    "/Applications/Codex.app/Contents/Frameworks/Codex Helper.app/Contents/MacOS/Codex Helper",
    "/opt/bin/codex app-server proxy",
    "/opt/bin/codex app-server daemon status",
])
def test_lingering_helpers_are_not_legacy_owners(handoff, target, command):
    (target["locks"] / f"{SID}.lock").unlink()
    target["rollout"].write_text(json.dumps({"type": "session_meta", "payload": {
        "id": SID, "cwd": str(target["cwd"]), "cli_version": "0.158.0"}}) + "\n")
    run, _ = process_table("123 " + command)
    handoff.assert_released(None, SID, str(target["cwd"]), run, **options(target))


def test_legacy_frontend_and_bad_process_listing_fail_closed(handoff, target):
    (target["locks"] / f"{SID}.lock").unlink()
    target["rollout"].write_text(json.dumps({"type": "session_meta", "payload": {
        "id": SID, "cwd": str(target["cwd"]), "cli_version": "0.158.0"}}) + "\n")
    bundle = target["home"] / "ChatGPT.app"
    run, _ = process_table(f"123 {bundle}/Contents/MacOS/ChatGPT")
    with pytest.raises(ValueError, match="no verifiable writer lock"):
        handoff.assert_released(str(bundle), SID, str(target["cwd"]), run, **options(target))
    invalid, _ = process_table("malformed")
    with pytest.raises(ValueError, match="could not parse the process list"):
        handoff.assert_released(None, SID, str(target["cwd"]), invalid, **options(target))


@pytest.mark.parametrize("suffix", [
    "codex-cli/CodexCLI.app/Contents/MacOS/codex",
    "codex-cli/bin/../CodexCLI.app/Contents/MacOS/codex",
])
def test_legacy_bundled_backend_in_bundle_with_spaces_fails_closed(
        handoff, target, suffix):
    (target["locks"] / f"{SID}.lock").unlink()
    target["rollout"].write_text(json.dumps({"type": "session_meta", "payload": {
        "id": SID, "cwd": str(target["cwd"]), "cli_version": "0.158.0"}}) + "\n")
    bundle = target["home"] / "Custom Folder" / "ChatGPT.app"
    run, _ = process_table(f"123 {bundle}/Contents/Resources/{suffix} app-server --listen stdio://")
    with pytest.raises(ValueError, match="no verifiable writer lock"):
        handoff.assert_released(str(bundle), SID, str(target["cwd"]), run, **options(target))


def test_rpc_skips_notifications_and_rejects_error_or_eof(handoff):
    class Process:
        def __init__(self, data):
            read, write = os.pipe()
            os.write(write, data)
            os.close(write)
            self.stdout = os.fdopen(read, "rb")
    process = Process(b'not-json\n{"method":"progress"}\n{"id":2,"result":{}}\n'
                      b'{"id":1,"result":{"thread":{"id":"x"}}}\n')
    try:
        result, rest = handoff._read_response(process, 1, handoff.time.monotonic() + 1)
        assert result == {"thread": {"id": "x"}} and rest == b""
    finally:
        process.stdout.close()
    process = Process(b'{"id":1,"error":{"message":"denied"}}\n')
    try:
        with pytest.raises(ValueError, match="refused"):
            handoff._read_response(process, 1, handoff.time.monotonic() + 1)
    finally:
        process.stdout.close()
    process = Process(b"")
    try:
        with pytest.raises(ValueError, match="exited before replying"):
            handoff._read_response(process, 1, handoff.time.monotonic() + 1)
    finally:
        process.stdout.close()


def test_nonregular_or_relative_rollout_rejected(handoff, target):
    run, _ = process_table()
    with pytest.raises(ValueError, match="path is missing"):
        handoff.assert_released(None, SID, str(target["cwd"]), run,
                                **options(target, rollout="relative.jsonl"))
    rollout = target["rollout"]
    rollout.unlink()
    rollout.symlink_to(target["locks"] / f"{SID}.lock")
    with pytest.raises(ValueError, match="not a regular file"):
        handoff.assert_released(None, SID, str(target["cwd"]), run, **options(target))


def test_writer_lock_replacement_and_bad_directory_fail_closed(handoff, target, monkeypatch):
    path = target["locks"] / f"{SID}.lock"
    real_fstat = handoff.os.fstat
    class Changed:
        st_dev = -1
        st_ino = -1
    monkeypatch.setattr(handoff.os, "fstat", lambda fd: Changed())
    run, _ = process_table()
    with pytest.raises(ValueError, match="changed during inspection"):
        handoff.assert_released(None, SID, str(target["cwd"]), run, **options(target))
    monkeypatch.setattr(handoff.os, "fstat", real_fstat)
    path.unlink()
    target["locks"].rmdir()
    with pytest.raises(ValueError, match="state directory is unknown"):
        handoff.assert_released(None, SID, str(target["cwd"]), run,
                                **dict(codex_home="relative", rollout=str(target["rollout"])))
    target["locks"].symlink_to(target["cwd"])
    with pytest.raises(ValueError, match="not a real directory"):
        handoff.assert_released(None, SID, str(target["cwd"]), run, **options(target))


def test_absent_lock_directory_uses_legacy_process_fallback(handoff, target):
    (target["locks"] / f"{SID}.lock").unlink()
    target["locks"].rmdir()
    run, calls = process_table("44 /opt/bin/codex app-server")
    with pytest.raises(ValueError, match="no verifiable writer lock"):
        handoff.assert_released(None, SID, str(target["cwd"]), run, **options(target))
    assert len(calls) == 1
    quiet, _ = process_table()
    handoff.assert_released(None, SID, str(target["cwd"]), quiet, **options(target))


def test_bad_or_incomplete_sibling_index_fails_closed(handoff, target, tmp_path):
    path = target["locks"] / f"{OTHER}.lock"
    path.touch()
    db = target["home"] / "state_5.sqlite"
    db.symlink_to(target["rollout"])
    run, _ = process_table()
    with locked(path, tmp_path):
        with pytest.raises(ValueError, match="could not be matched"):
            handoff.assert_released(None, SID, str(target["cwd"]), run, **options(target))
    db.unlink()
    db.write_text("not sqlite")
    with locked(path, tmp_path):
        with pytest.raises(ValueError, match="could not be matched"):
            handoff.assert_released(None, SID, str(target["cwd"]), run, **options(target))


def test_unheld_sibling_lock_and_bad_cli_pid_do_not_claim_ownership(handoff, target, tmp_path):
    (target["locks"] / f"{OTHER}.lock").touch()
    run, _ = process_table()
    handoff.assert_released(None, SID, str(target["cwd"]), run, **options(target))
    with locked(target["locks"] / f"{SID}.lock", tmp_path):
        with pytest.raises(ValueError, match="still loaded"):
            handoff.assert_released(None, SID, str(target["cwd"]), run,
                                    **options(target, cli_pid="not-an-int"))


def test_other_cwd_owner_archive_guidance_is_guarded(handoff, target, tmp_path):
    private = tmp_path / "private-git"
    private.mkdir()
    (private / "t-app-slot").write_text("codex-app\n")
    (target["cwd"] / ".git").write_text("gitdir: ../private-git\n")
    db = sqlite3.connect(target["home"] / "state_5.sqlite")
    db.execute("create table threads (id text, cwd text)")
    db.execute("insert into threads values (?, ?)", (OTHER, str(target["cwd"])))
    db.commit()
    db.close()
    path = target["locks"] / f"{OTHER}.lock"
    path.touch()
    run, _ = process_table(gitdir=private)
    with locked(path, tmp_path):
        with pytest.raises(ValueError, match="Archive that other conversation"):
            handoff.assert_released(None, SID, str(target["cwd"]), run, **options(target))


def test_archive_path_and_invalid_ids_rejected(handoff, target):
    run, _ = process_table()
    with pytest.raises(ValueError, match="thread id is invalid"):
        handoff.assert_released(None, "bad", str(target["cwd"]), run, **options(target))
    with pytest.raises(ValueError, match="archive is outside"):
        handoff.assert_released(None, SID, str(target["cwd"]), run,
                                **options(target, archived=True))
    with pytest.raises(ValueError, match="thread id is invalid"):
        handoff.unarchive_thread("bad", str(target["cwd"]),
                                 codex_home=str(target["home"]), rollout=str(target["rollout"]))
    with pytest.raises(ValueError, match="archive is outside"):
        handoff.unarchive_thread(SID, str(target["cwd"]),
                                 codex_home=str(target["home"]), rollout=str(target["rollout"]))


def test_archive_hint_requires_regular_gitfile_and_valid_git_directory(handoff, target):
    run, _ = process_table()
    (target["cwd"] / ".git").mkdir()
    assert not handoff._safe_archive_hint(str(target["cwd"]), run)
    (target["cwd"] / ".git").rmdir()
    (target["cwd"] / ".git").write_text("gitdir: ../private\n")
    assert not handoff._safe_archive_hint(str(target["cwd"]), run)
    private = target["home"] / "private"
    private.mkdir()
    marker = private / "t-app-slot"
    marker.mkdir()
    valid_git, _ = process_table(gitdir=private)
    assert not handoff._safe_archive_hint(str(target["cwd"]), valid_git)
    marker.rmdir()
    marker.write_text("wrong\n")
    assert not handoff._safe_archive_hint(str(target["cwd"]), valid_git)


@pytest.mark.parametrize("wrong", ["id", "cwd"])
def test_private_unarchive_rejects_wrong_rpc_and_reaps_stubborn_child(
        handoff, target, monkeypatch, wrong):
    archive = target["home"] / "archived_sessions" / target["rollout"].name
    archive.parent.mkdir()
    target["rollout"].replace(archive)
    class Pipe:
        def write(self, data):
            pass
        def flush(self):
            pass
        def close(self):
            pass
    class Child:
        stdin = Pipe()
        stdout = Pipe()
        killed = False
        def terminate(self):
            raise ProcessLookupError
        def wait(self, timeout):
            if not self.killed:
                raise subprocess.TimeoutExpired("codex", timeout)
        def kill(self):
            self.killed = True
    child = Child()
    monkeypatch.setattr(handoff.subprocess, "Popen", lambda *args, **kwargs: child)
    replies = iter([({}, b""),
                    ({"thread": {"id": OTHER if wrong == "id" else SID}}, b""),
                    ({"thread": {"id": SID, "cwd": "/elsewhere"}}, b"")])
    monkeypatch.setattr(handoff, "_read_response", lambda *args: next(replies))
    with pytest.raises(ValueError, match="different conversation" if wrong == "id" else "another worktree"):
        handoff.unarchive_thread(SID, str(target["cwd"]),
                                 codex_home=str(target["home"]), rollout=str(archive))
    assert child.killed
