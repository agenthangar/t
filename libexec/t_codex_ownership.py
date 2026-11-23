"""Verify Codex writer ownership before resuming saved CLI conversations."""

import fcntl
import glob
import hashlib
import json
import os
import re
import select
import shlex
import shutil
import sqlite3
import stat
import subprocess
import time
from contextlib import closing
from urllib.parse import quote

import runpy
from pathlib import Path
_spinner = runpy.run_path(str(Path(__file__).with_name("t_progress.py")))["spinner"]


_RETRY = "Retry: t session resume."
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")
_WRITER_SETTLE_SECONDS = 30.0


class WriterBusyError(ValueError):
    """A native writer still holds the selected thread or its workspace."""


class UnresolvedWriterError(ValueError):
    """A held writer has not published verifiable workspace metadata yet."""

    def __init__(self, message, *, fingerprint=None):
        super().__init__(message)
        self.fingerprint = fingerprint


def _processes(run, retry):
    result = run(["ps", "-ww", "-x", "-o", "pid=,command="], timeout=5)
    if result.returncode:
        raise ValueError("could not inspect desktop and app-server processes; " + retry)
    processes = []
    for line in result.stdout.splitlines():
        fields = line.strip().split(None, 1)
        if len(fields) == 2 and fields[0].isdigit():
            processes.append((int(fields[0]), fields[1]))
        elif line.strip():
            raise ValueError("could not parse the process list; " + retry)
    return processes


def _owners(bundle, processes):
    # The app may have been uninstalled since the reservation was written.
    # Recognize both historical names without requiring their bundles to exist.
    candidates = [os.path.join(base, name + ".app")
                  for base in ("/Applications", os.path.join(os.path.expanduser("~"), "Applications"))
                  for name in ("ChatGPT", "Codex")]
    if bundle:
        candidates.append(bundle)
    roots = {path + os.sep for candidate in candidates
             for path in (os.path.abspath(candidate), os.path.realpath(candidate))}
    frontends = {root + "Contents/MacOS/" + name
                 for root in roots for name in ("ChatGPT", "Codex")}
    bundled_clis = {root + "Contents/Resources/" + suffix
                   for root in roots for suffix in (
                       "codex", "codex-cli/bin/codex",
                       "codex-cli/CodexCLI.app/Contents/MacOS/codex",
                       "codex-cli/bin/../CodexCLI.app/Contents/MacOS/codex")}
    app = []
    daemon = []
    for pid, command in processes:
        # Only the actual desktop frontend requires quitting. Computer-use,
        # code-mode, and Electron helpers can outlive it, and CLIs may use the
        # bundled executable. Neither a bundle path nor an argument mentioning
        # the app establishes desktop ownership.
        if any(command == exe or command.startswith(exe + " ") for exe in frontends):
            app.append(pid)
        elif ((re.match(r"^(?:\S*/)?codex(?:-(?:aarch64|x86_64)-[\w.-]+)?(?:\s|$)", command)
               or any(command == exe or command.startswith(exe + " ") for exe in bundled_clis))
              and re.search(r"(?:^|\s)app-server(?:\s|$)", command)
              and not re.search(r"\bapp-server\s+(?:pid-update-loop|proxy|generate-ts|generate-json-schema)\b", command)
              and not re.search(r"\bapp-server\s+daemon\s+(?:start|stop|status|version|pid-update-loop)\b", command)):
            daemon.append(pid)
    return app, daemon


@_spinner('Waiting for conversation service')
def _read_response(process, request_id, deadline, buffer=b""):
    while True:
        if b"\n" in buffer:
            raw, buffer = buffer.split(b"\n", 1)
            try:
                reply = json.loads(raw)
            except ValueError:
                continue
            if isinstance(reply, dict) and reply.get("id") == request_id:
                if "error" in reply or not isinstance(reply.get("result"), dict):
                    raise ValueError("app-server refused the request")
                return reply["result"], buffer
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0 or not select.select([process.stdout], [], [], remaining)[0]:
            raise ValueError("app-server request timed out")
        chunk = os.read(process.stdout.fileno(), 65536)
        if not chunk:
            raise ValueError("app-server exited before replying")
        buffer += chunk
        if len(buffer) > 1024 * 1024:
            raise ValueError("app-server reply was too large")


def _verify_rollout(rollout, sid, cwd):
    """The chosen saved history must still name this exact conversation/root."""
    if not rollout or not os.path.isabs(rollout):
        raise ValueError("the selected Codex rollout path is missing")
    info = os.lstat(rollout)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError("the selected Codex rollout is not a regular file")
    with open(rollout, encoding="utf-8") as handle:
        first = json.loads(handle.readline())
    payload = first.get("payload") if isinstance(first, dict) else None
    if (not isinstance(first, dict) or first.get("type") != "session_meta"
            or not isinstance(payload, dict)
            or payload.get("id") != sid or not isinstance(payload.get("cwd"), str)
            or os.path.realpath(payload["cwd"]) != os.path.realpath(cwd)):
        raise ValueError("the saved Codex rollout names another thread or worktree")
    version = payload.get("cli_version")
    match = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)(?:[-+][\w.-]+)?", version) if isinstance(version, str) else None
    return tuple(map(int, match.groups())) if match else None


def _lock_held(path):
    """None means no modern lock; otherwise use the same macOS advisory lock."""
    try:
        entry = os.lstat(path)
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(entry.st_mode):
        raise ValueError("a Codex writer lock is not a regular file")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        current = os.fstat(fd)
        if (current.st_dev, current.st_ino) != (entry.st_dev, entry.st_ino):
            raise ValueError("a Codex writer lock changed during inspection")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    finally:
        os.close(fd)


def _lock_dir(codex_home):
    if not codex_home or not os.path.isabs(codex_home):
        raise ValueError("the Codex state directory is unknown")
    folder = os.path.join(codex_home, "thread-writer-locks")
    try:
        info = os.lstat(folder)
    except FileNotFoundError:
        return None
    if not stat.S_ISDIR(info.st_mode):
        raise ValueError("the Codex writer-lock directory is not a real directory")
    return folder


def _rollout_cwd(path, sid, *, exact=True):
    """Read one bounded identity record, checking the opened file's identity."""
    before = os.lstat(path)
    if not stat.S_ISREG(before.st_mode):
        raise ValueError("an unindexed Codex rollout is not a regular file")
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(fd)
        if (not stat.S_ISREG(opened.st_mode)
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)):
            raise ValueError("an unindexed Codex rollout changed during inspection")
        with os.fdopen(fd, encoding="utf-8", closefd=False) as handle:
            first = handle.readline(1024 * 1024 + 1)
        if len(first) > 1024 * 1024:
            raise ValueError("an unindexed Codex rollout has oversized identity metadata")
        record = json.loads(first)
    finally:
        os.close(fd)
    after = os.lstat(path)
    if (not stat.S_ISREG(after.st_mode)
            or (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino)):
        raise ValueError("an unindexed Codex rollout changed during inspection")
    payload = record.get("payload") if isinstance(record, dict) else None
    if (not isinstance(record, dict) or record.get("type") != "session_meta"
            or not isinstance(payload, dict)):
        raise ValueError("an unindexed Codex rollout has invalid identity metadata")
    if not exact and payload.get("id") != sid:
        return None
    if (payload.get("id") != sid or not isinstance(payload.get("cwd"), str)
            or not os.path.isabs(payload["cwd"])):
        raise ValueError("an unindexed Codex rollout has invalid identity metadata")
    return payload["cwd"]


def _rollout_cwds(codex_home, sid):
    """Read only identity metadata for an unindexed writer, never chat content."""
    known = set()
    def unreadable(error):
        raise error
    for folder in ("sessions", "archived_sessions"):
        root = os.path.join(codex_home, folder)
        try:
            info = os.lstat(root)
        except FileNotFoundError:
            continue
        if not stat.S_ISDIR(info.st_mode):
            raise ValueError("a Codex rollout directory is not a real directory")
        for directory, _, files in os.walk(root, onerror=unreadable):
            for name in files:
                if name.startswith("rollout-") and name.endswith(sid + ".jsonl"):
                    known.add(_rollout_cwd(os.path.join(directory, name), sid))
    return known


def _standalone_writer_cwd(path, pid, run):
    """Bind a fresh, in-process CLI's sole thread lock to its live cwd.

    This deliberately excludes shared servers, resumed/forked threads, cwd
    overrides and unknown CLI options. A process cwd alone is not evidence.
    Process birth/command, sole thread lock and cwd must remain stable; unrelated
    descriptors may change while the CLI is working.
    """
    def snapshot():
        process = run(['ps', '-ww', '-p', str(pid), '-o', 'lstart=,command='], timeout=5)
        files = run(['lsof', '-nP', '-a', '-p', str(pid), '-Fpcfn'], timeout=5)
        if any(r.returncode or (r.stderr or '').strip() or len(r.stdout) > 1024 * 1024
               for r in (process, files)):
            return None
        fields = process.stdout.strip().split(None, 5)
        if len(fields) != 6:
            return None
        args = shlex.split(fields[5])
        if not args or os.path.basename(args.pop(0)) != 'codex' or '--no-daemon' not in args:
            return None
        values = {'-c', '--config', '-m', '--model', '-s', '--sandbox',
                  '-a', '--ask-for-approval', '--enable'}
        switches = {'--no-daemon', '--no-alt-screen', '--full-auto',
                    '--dangerously-bypass-approvals-and-sandbox'}
        standalone = False
        while args:
            arg = args.pop(0)
            if arg in values and args:
                value = args.pop(0)
            elif '=' in arg and arg.split('=', 1)[0] in values:
                arg, value = arg.split('=', 1)
            elif arg in switches:
                standalone |= arg == '--no-daemon'
                continue
            else:
                return None
            if arg == '--enable' and value != 'fast_mode':
                return None
            if arg in {'-c', '--config'}:
                key = value.split('=', 1)[0]
                if key not in {'model', 'model_reasoning_effort', 'service_tier'} and not re.fullmatch(
                        r'mcp_servers\.[\w-]+\.tools\.[\w-]+\.approval_mode', key):
                    return None
        if not standalone:
            return None
        owner, descriptor, cwd, locks = None, None, [], set()
        for line in files.stdout.splitlines():
            if line.startswith('p'):
                if line != 'p' + str(pid) or owner is not None:
                    return None
                owner = pid
            elif line.startswith('f'):
                descriptor = line[1:]
            elif line.startswith('n') and owner == pid:
                filename = line[1:]
                if descriptor == 'cwd':
                    cwd.append(filename)
                if os.path.basename(os.path.dirname(filename)) == 'thread-writer-locks':
                    locks.add(filename)
        if len(cwd) != 1 or not os.path.isabs(cwd[0]) or locks != {path}:
            return None
        return process.stdout, frozenset(locks), cwd[0]
    before = snapshot()
    return before[2] if before is not None and snapshot() == before else None


def _holder_identity(pids, run):
    """An opaque retry token includes process birth, never just reusable PIDs."""
    if not pids or run is None:
        return None
    identities = []
    try:
        for pid in pids:
            result = run(['ps', '-ww', '-p', str(pid), '-o', 'lstart=,command='], timeout=5)
            if (result.returncode or (result.stderr or '').strip() or
                    len(result.stdout) > 1024 * 1024 or len(result.stdout.strip().split(None, 5)) != 6):
                return None
            identities.append((pid, hashlib.sha256(result.stdout.strip().encode()).hexdigest()))
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    return tuple(identities)


def _writer_fds(pid, run):
    result = run(['lsof', '-nP', '-a', '-p', str(pid), '-FpcfdDtin'], timeout=5)
    if result.returncode or (result.stderr or '').strip() or len(result.stdout) > 1024 * 1024:
        return None
    owner, current, records = False, None, []
    for line in result.stdout.splitlines():
        if line.startswith('p'):
            if owner or line != 'p' + str(pid):
                return None
            owner = True
        elif line.startswith('f') and owner:
            current = {'f': line[1:]}
            records.append(current)
        elif line[:1] in {'d', 'D', 't', 'i', 'n'} and current is not None:
            if line[0] in current:
                return None
            current[line[0]] = line[1:]
    return records if owner else None


def _pipe_identity(fd):
    if fd.get('t') == 'PIPE' and fd.get('d', '').startswith('0x') and fd.get('n', '').startswith('->0x'):
        return tuple(sorted((fd['d'], fd['n'][2:])))
    if fd.get('t') == 'FIFO' and fd.get('i', '').isdecimal() and fd.get('D', '').startswith('0x'):
        return fd['D'], fd['i']
    return None


def _private_file(path, *, directory=False):
    info = os.lstat(path)
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError('the owned runtime evidence is not private')
    return info


def _checkpoint_workspace(path, sid, legacy_native=False):
    """Read bounded private identity evidence; do not retain task or chat text."""
    _private_file(os.path.dirname(path), directory=True)
    before = _private_file(path)
    fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | getattr(os, 'O_NOFOLLOW', 0))
    try:
        opened = os.fstat(fd)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            return None
        with os.fdopen(fd, encoding='utf-8', closefd=False) as handle:
            raw = handle.read(1024 * 1024 + 1)
        if len(raw) > 1024 * 1024:
            return None
        value = json.loads(raw)
    finally:
        os.close(fd)
    after = _private_file(path)
    if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
        return None
    if (not isinstance(value, dict) or value.get('thread_id') != sid or
            not isinstance(value.get('workspace'), str) or not os.path.isabs(value['workspace'])):
        return None
    workspace = os.path.realpath(value['workspace'])
    if 'thread_workspace' in value:
        return workspace if value['thread_workspace'] == workspace else None
    # Before explicit evidence was saved, Checkpoint compared the correlated
    # native response cwd with launch cwd. In this known protocol cwd is a
    # required ThreadStartResponse/Thread field. Do not generalize that promise
    # to unknown native versions, which may omit it and trigger the old default.
    return workspace if legacy_native and value.get('unavailable_reason') == '' else None


def _supervisor_scripts():
    """Only this t source, its canonical checkout, or the installed t source."""
    source = Path(__file__).resolve().parents[1]
    roots = {source}
    try:
        scope = runpy.run_path(str(source / 'libexec/t_launch_policy.py'))['repository_scope']
        common = scope(source)[1]
        if common is not None and common.name == '.git' and (common.parent / '.git').resolve() == common:
            roots.add(common.parent)
    except (OSError, ValueError):
        pass
    installed = shutil.which('t')
    if installed:
        command = Path(installed).resolve()
        if command.name == 't' and command.parent.name == 'bin':
            roots.add(command.parent.parent)
    return {(root / 'libexec/t_local_inference.py').resolve() for root in roots
            if (root / 'libexec/t_local_inference.py').is_file()}


def _supervised_writer_cwd(path, sid, pid, run):
    """Bind an exact thread to t's private backend, never a shared server cwd."""
    def snapshot():
        child = run(['ps', '-ww', '-p', str(pid), '-o', 'ppid=,lstart=,command='], timeout=5)
        fields = child.stdout.strip().split(None, 6)
        if (child.returncode or (child.stderr or '').strip() or len(child.stdout) > 1024 * 1024 or
                len(fields) != 7 or not fields[0].isdecimal() or int(fields[0]) <= 1):
            return None
        argv = shlex.split(fields[6])
        if (not argv or os.path.basename(argv[0]) != 'codex' or
                argv[1:4] != ['app-server', '--listen', 'stdio://']):
            return None
        parent_pid = int(fields[0])
        parent = run(['ps', '-ww', '-p', str(parent_pid), '-o', 'lstart=,command='], timeout=5)
        fields = parent.stdout.strip().split(None, 5)
        if (parent.returncode or (parent.stderr or '').strip() or len(parent.stdout) > 1024 * 1024 or len(fields) != 6):
            return None
        command = shlex.split(fields[5])
        if (len(command) < 4 or not os.path.basename(command[0]).lower().startswith('python') or
                not os.path.isabs(command[1]) or Path(command[1]).resolve() not in _supervisor_scripts() or
                command[2] not in {'--lead', '--lead-policy'}):
            return None
        backend, supervisor = _writer_fds(pid, run), _writer_fds(parent_pid, run)
        if not backend or not supervisor or path not in {fd.get('n') for fd in backend}:
            return None
        pipes = [_pipe_identity(fd) for descriptor in ('0', '1')
                 for fd in backend if fd['f'].rstrip('rwu') == descriptor]
        parent_pipes = {_pipe_identity(fd) for fd in supervisor} - {None}
        if len(pipes) != 2 or None in pipes or len(set(pipes)) != 2 or not set(pipes) <= parent_pipes:
            return None
        logs = [fd for fd in backend if fd['f'].rstrip('rwu') == '2' and
                os.path.basename(fd.get('n', '')) == 'runtime.log' and os.path.isabs(fd['n'])]
        if len(logs) != 1:
            return None
        log = logs[0]
        info = _private_file(log['n'])
        identity = (str(info.st_ino), info.st_dev)
        def same_log(fd):
            return (fd.get('n') == log['n'] and fd.get('i') == identity[0] and
                    int(fd.get('D', '-1'), 16) == identity[1])
        if not same_log(log) or not any(same_log(fd) for fd in supervisor):
            return None
        checkpoint = os.path.join(os.path.dirname(log['n']), 'checkpoint.json')
        # A legacy checkpoint relied on cwd being required by the native
        # protocol. Recognize only the verified installed 0.170.0 cask build,
        # mapped by this backend. Never execute a process-provided argv path.
        installed = shutil.which('codex')
        native = os.path.realpath(installed) if installed else ''
        legacy_native = False
        if re.search(r'/Caskroom/codex/0\.170\.0/bin/codex$', native):
            native_info = os.stat(native)
            legacy_native = (stat.S_ISREG(native_info.st_mode) and not native_info.st_mode & 0o022 and
                any(fd['f'] == 'txt' and fd.get('n') == native and
                    fd.get('i') == str(native_info.st_ino) and
                    int(fd.get('D', '-1'), 16) == native_info.st_dev for fd in backend))
        workspace = _checkpoint_workspace(checkpoint, sid, legacy_native)
        if not workspace:
            return None
        return child.stdout, parent.stdout, tuple(pipes), identity, log['n'], workspace
    before = snapshot()
    return before[-1] if before is not None and before == snapshot() else None


def _open_writer_cwds(path, sid, run):
    """Resolve only exact thread metadata in files opened by lock holders.

    Shared backend cwd and other threads are never workspace evidence. A
    standalone CLI requires an additional stable sole-thread proof below.
    """
    pids = ()
    if run is None:
        return set(), pids
    try:
        result = run(["lsof", "-nP", "-t", path], timeout=5)
        lines = result.stdout.splitlines()
        if (result.returncode or (result.stderr or "").strip()
                or not lines or len(lines) > 32
                or any(not line.isdecimal() or int(line) <= 0 for line in lines)):
            return set(), pids
        pids = tuple(sorted({int(line) for line in lines}))
        result = run(["lsof", "-nP", "-p", ",".join(map(str, pids)), "-Fpn"], timeout=5)
        if result.returncode or (result.stderr or "").strip() or len(result.stdout) > 1024 * 1024:
            return set(), pids
        paths = set()
        owner = None
        for line in result.stdout.splitlines():
            if line.startswith("p"):
                owner = int(line[1:]) if line[1:].isdecimal() else None
            elif line.startswith("n") and owner in pids:
                filename = line[1:]
                name = os.path.basename(filename)
                if os.path.isabs(filename) and name.startswith("rollout-") and name.endswith(".jsonl"):
                    paths.add(filename)
        if len(paths) > 128:
            return set(), pids
        cwds = {_rollout_cwd(filename, sid, exact=False) for filename in paths}
        cwds.discard(None)
        if not cwds and len(pids) == 1 and ('n' + path) in result.stdout.splitlines():
            cwd = (_standalone_writer_cwd(path, pids[0], run)
                   or _supervised_writer_cwd(path, sid, pids[0], run))
            if cwd:
                cwds.add(cwd)
        return cwds, pids
    except (OSError, ValueError, UnicodeError, subprocess.SubprocessError):
        return set(), pids


def _unknown_writer_message(sid, pids):
    owner = " (PID " + ", ".join(map(str, pids)) + ")" if pids else ""
    inspect = ("Inspect holders with `ps -p " + ",".join(map(str, pids))
               + " -o pid=,comm=` or `t session list`. " if pids else
               "Look for this thread in Codex or `t session list`. ")
    return (f"another loaded Codex conversation {sid}{owner} could not be matched to its worktree. "
            + inspect +
            "Finish and exit its identified CLI, or finish its turn and close the owning app; "
            "allow any shared backend to release the writer, then retry resume")


_index_rows = runpy.run_path(str(Path(__file__).with_name("t_codex_store.py")))["rows"]


def _thread_cwds(codex_home, sid, *, for_close=False):
    """Resolve a held sibling lock through this Codex home's local state DBs."""
    known = set()
    databases = glob.glob(os.path.join(codex_home, "state_*.sqlite"))
    if for_close and os.path.join(codex_home, "state_5.sqlite") not in databases:
        raise ValueError("the Codex ownership index is unavailable")
    for db in databases:
        try:
            if not stat.S_ISREG(os.lstat(db).st_mode):
                raise ValueError("a Codex ownership index is not a regular file")
            rows = _index_rows(db, "select cwd from threads where id=?", (sid,))
            if any(not isinstance(row[0], str) or not os.path.isabs(row[0])
                   for row in rows):
                raise ValueError("a Codex writer has invalid workspace metadata")
            known.update(row[0] for row in rows
                if isinstance(row[0], str) and os.path.isabs(row[0]))
        except (OSError, sqlite3.Error) as exc:
            raise ValueError(f"could not inspect the Codex ownership index {os.path.basename(db)}: {exc}") from exc
    if not known:
        known = _rollout_cwds(codex_home, sid)
    return known


def _scan_cwd_owner(folder, sid, cwd, codex_home, *, unknown=None, for_close=False, run=None):
    """Prefer known worktree conflicts; otherwise retain the unknown policy."""
    if folder is None:
        return None
    unresolved = []
    for entry in os.scandir(folder):
        other = entry.name.removesuffix(".lock")
        if not entry.name.endswith(".lock") or not _UUID.fullmatch(other) or other == sid:
            continue
        if not _lock_held(entry.path):
            continue
        before = os.lstat(entry.path)
        cwds = _thread_cwds(codex_home, other, for_close=for_close)
        pids = ()
        if not cwds:
            cwds, pids = _open_writer_cwds(entry.path, other, run)
            # A backend may publish its index or rollout while lsof is running.
            cwds.update(_thread_cwds(codex_home, other, for_close=for_close))
        if not _lock_held(entry.path):
            continue
        after = os.lstat(entry.path)
        if (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino):
            raise ValueError("a Codex writer lock changed during inspection")
        if not cwds:
            unresolved.append((other, pids, (after.st_dev, after.st_ino)))
            if unknown is not None:
                unknown.add(other)
            continue
        if any(os.path.realpath(path) == os.path.realpath(cwd) for path in cwds):
            return other
    if unresolved and unknown is None and not for_close:
        unresolved.sort(key=lambda item: item[0])
        identities = tuple(_holder_identity(pids, run) for _, pids, _ in unresolved)
        fingerprint = tuple((thread, inode, identity)
                            for (thread, _, inode), identity in zip(unresolved, identities)) if all(identities) else None
        raise UnresolvedWriterError(_unknown_writer_message(*unresolved[0][:2]),
                                    fingerprint=fingerprint)
    return None


def _other_cwd_owner(folder, sid, cwd, codex_home, *, unknown=None, for_close=False, run=None,
                     unresolved_fingerprint=None):
    """Let starting writers publish metadata; never treat a timeout as release.

    Healthy launches and allocator-proven fresh workspaces take one scan. Only
    unresolved writers enter the bounded wait, with a fresh scan on each retry
    so a newly published conflict cannot be hidden by an earlier snapshot.
    """
    options = dict(unknown=unknown, for_close=for_close, run=run)
    try:
        return _scan_cwd_owner(folder, sid, cwd, codex_home, **options)
    except UnresolvedWriterError as exc:
        if (_WRITER_SETTLE_SECONDS <= 0 or
                (exc.fingerprint is not None and exc.fingerprint == unresolved_fingerprint)):
            raise
    return _wait_cwd_owner(folder, sid, cwd, codex_home, options)


@_spinner('Waiting for conversation ownership')
def _wait_cwd_owner(folder, sid, cwd, codex_home, options):
    deadline = time.monotonic() + _WRITER_SETTLE_SECONDS
    while True:
        remaining = deadline - time.monotonic()
        if remaining > 0:
            time.sleep(min(.5, remaining))
        try:
            return _scan_cwd_owner(folder, sid, cwd, codex_home, **options)
        except UnresolvedWriterError:
            if time.monotonic() >= deadline:
                raise




def _selected_writer_hint(path, run):
    """Diagnostic only: identify processes with this exact lock open."""
    try:
        result = run(["lsof", "-nP", "-t", path], timeout=5)
        pids = result.stdout.splitlines()
        if (result.returncode or not pids or len(pids) > 32 or
                any(not pid.isdecimal() or int(pid) <= 0 for pid in pids)):
            return ""
        labels = []
        for pid in sorted(set(pids), key=int):
            result = run(["ps", "-p", pid, "-o", "comm="], timeout=5)
            executable = result.stdout.strip() if not result.returncode else ""
            app = next((name for name in ("ChatGPT", "Codex")
                        if f"/{name}.app/Contents/" in executable), None)
            labels.append(pid + (f" ({app} backend)" if app else ""))
        return "Writer-lock processes: " + ", ".join(labels) + ". "
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return ""




def _archived_in_home(codex_home, rollout):
    """The selected archived file is inside this exact Codex home's archive."""
    archive = os.path.realpath(os.path.join(codex_home, "archived_sessions"))
    return os.path.commonpath((archive, os.path.realpath(rollout))) == archive


def _archived_released(codex_home, sid, cwd, rollout, run):
    """Prove an older saved thread was archived and its rollout is closed.

    `session_meta.cli_version` records creation, not the version of the backend
    that archived it. Codex 0.159/0.160 atomically moves an unloaded thread to
    archived_sessions and updates state_5.sqlite. The exact archive inode must
    have no open process FD before the CLI attempts its own atomic writer claim.
    """
    db = os.path.join(codex_home, "state_5.sqlite")
    try:
        if not stat.S_ISREG(os.lstat(db).st_mode):
            return False
        rows = _index_rows(db,
            "select id,cwd,archived,rollout_path from threads where id=?", (sid,))
    except (OSError, sqlite3.Error):
        return False
    if (len(rows) != 1 or rows[0][0] != sid or not isinstance(rows[0][1], str)
            or os.path.realpath(rows[0][1]) != os.path.realpath(cwd)
            or rows[0][2] != 1 or not isinstance(rows[0][3], str)
            or os.path.realpath(rows[0][3]) != os.path.realpath(rollout)):
        return False
    before = os.lstat(rollout)
    if not stat.S_ISREG(before.st_mode):
        return False
    result = run(["lsof", "-nP", "-t", rollout], timeout=5)
    if result.returncode == 0 and result.stdout.strip():
        return False
    if result.returncode != 1 or result.stdout.strip() or (result.stderr or "").strip():
        raise ValueError("could not reliably inspect the archived Codex rollout's open file handles")
    after = os.lstat(rollout)
    return stat.S_ISREG(after.st_mode) and (before.st_dev, before.st_ino) == (after.st_dev, after.st_ino)


def assert_released(bundle, sid, cwd, run, *, codex_home=None, rollout=None,
                    archived=False, retry=_RETRY, unknown=None, for_close=False,
                    ownership_cwd=None, unresolved_fingerprint=None):
    """Allow only a free selected writer lock and no other writer in its cwd.

    The eventual `codex resume --no-daemon` takes the lock atomically. This
    check is for clear guidance and avoiding unrelated hung app-server probes.
    """
    if not _UUID.fullmatch(sid):
        raise ValueError("the selected Codex thread id is invalid. " + retry)
    try:
        version = _verify_rollout(rollout, sid, cwd)
        folder = _lock_dir(codex_home)
        if archived and not _archived_in_home(codex_home, rollout):
            raise ValueError("the selected Codex archive is outside this state directory. " + retry)
        target = os.path.join(folder, sid + ".lock") if folder else None
        held = _lock_held(target) if target else None
        modern = folder is not None and version is not None and version >= (0, 159, 0)
        archived_proven = False
        if held is None and archived and folder is not None and not modern:
            archived_proven = _archived_released(codex_home, sid, cwd, rollout, run)
            if not archived_proven:
                raise ValueError("the selected archived Codex conversation is still open "
                                 "or its release could not be verified. " + retry)
        if not modern and not archived_proven and (held is None or held is False):
            # Codex >=0.159 removes this file when it unloads the thread,
            # including after archive. Older or unknown versions need a
            # conservative process fallback.
            app, backends = _owners(bundle, _processes(run, retry))
            if app or backends:
                raise ValueError("this saved conversation has no verifiable writer lock "
                                 "contract. " + "Exit the owning client and wait for its backend to release the writer. " + retry)
        if held:
            raise WriterBusyError("the selected Codex conversation is still loaded "
                             "(its writer lock is held). "
                             + _selected_writer_hint(target, run)
                             + "Exit the owning client and wait for its backend to release the writer. " + retry)
        try:
            other = _other_cwd_owner(folder, sid, ownership_cwd or cwd, codex_home, unknown=unknown,
                                     for_close=for_close, run=run,
                                     unresolved_fingerprint=unresolved_fingerprint)
        except UnresolvedWriterError as exc:
            raise UnresolvedWriterError(str(exc) + ". " + retry, fingerprint=exc.fingerprint) from exc
        except ValueError as exc:
            raise ValueError(str(exc) + ". " + retry) from exc
        if other:
            guidance = "Wait for its owning backend to exit before resuming. "
            raise WriterBusyError("another Codex conversation in this worktree is still loaded "
                             f"({other}). " + guidance + retry)
        # Resolving a starting sibling may take time. Recheck the selected
        # writer before returning; the native resume still claims it atomically.
        if target and _lock_held(target):
            raise WriterBusyError("the selected Codex conversation is still loaded "
                             "(its writer lock is held). " + retry)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("could not verify the selected Codex rollout or writer lock: "
                         + str(exc) + ". " + retry) from exc








@_spinner('Restoring conversation')
def unarchive_thread(sid, cwd, *, codex_home, rollout, process_cwd=None):
    """Restore one saved thread through a private stdio backend, never proxy."""
    if not _UUID.fullmatch(sid):
        raise ValueError("the selected Codex thread id is invalid")
    _verify_rollout(rollout, sid, cwd)
    if not _archived_in_home(codex_home, rollout):
        raise ValueError("the selected Codex archive is outside this state directory")
    environment = {key: value for key, value in os.environ.items()
                   if not key.startswith("COV_CORE_")}
    environment["CODEX_HOME"] = codex_home
    process = subprocess.Popen(
        ["codex", "app-server", "--disable", "plugins", "--listen", "stdio://"],
        cwd=process_cwd or cwd, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, start_new_session=True)
    deadline = time.monotonic() + 12
    try:
        def send(message):
            process.stdin.write((json.dumps(message) + "\n").encode())
            process.stdin.flush()

        send({"id": 1, "method": "initialize",
              "params": {"clientInfo": {"name": "t-app-pull", "version": "1"}}})
        _, pending = _read_response(process, 1, deadline)
        send({"method": "initialized", "params": {}})
        send({"id": 2, "method": "thread/unarchive", "params": {"threadId": sid}})
        result, pending = _read_response(process, 2, deadline, pending)
        thread = result.get("thread")
        if not isinstance(thread, dict) or thread.get("id") != sid:
            raise ValueError("Codex restored a different conversation")
        send({"id": 3, "method": "thread/read",
              "params": {"threadId": sid, "includeTurns": False}})
        result, _ = _read_response(process, 3, deadline, pending)
        thread = result.get("thread")
        if (not isinstance(thread, dict) or thread.get("id") != sid
                or not isinstance(thread.get("cwd"), str)
                or os.path.realpath(thread["cwd"]) != os.path.realpath(cwd)):
            raise ValueError("the restored Codex conversation has another worktree")
        restored = thread.get("path")
        _verify_rollout(restored, sid, cwd)
    finally:
        try:
            process.terminate()
        except ProcessLookupError:
            pass
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
        process.stdin.close()
        process.stdout.close()
