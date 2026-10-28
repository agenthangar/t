"""Verify per-conversation Codex ownership during CLI/desktop handoffs."""

import fcntl
import glob
import json
import os
import re
import select
import sqlite3
import stat
import subprocess
import time
from contextlib import closing
from urllib.parse import quote

import runpy
from pathlib import Path
_spinner = runpy.run_path(str(Path(__file__).with_name("t_progress.py")))["spinner"]


_RETRY = "Retry: t app pull."
_UUID = re.compile(r"[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\Z")


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


def _open_writer_cwds(path, sid, run):
    """Resolve only exact thread metadata in files opened by lock holders.

    Process cwd, executable and other threads on a shared backend are never
    workspace evidence. Failed or oversized probes leave the writer unknown.
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
        return cwds - {None}, pids
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
            uri = "file:" + quote(db, safe="/") + "?mode=ro"
            with closing(sqlite3.connect(uri, uri=True, timeout=0.5)) as connection:
                rows = connection.execute("select cwd from threads where id=?", (sid,)).fetchall()
                if any(not isinstance(row[0], str) or not os.path.isabs(row[0])
                       for row in rows):
                    raise ValueError("a Codex writer has invalid workspace metadata")
                known.update(row[0] for row in rows
                    if isinstance(row[0], str) and os.path.isabs(row[0]))
        except (OSError, sqlite3.Error) as exc:
            raise ValueError("could not inspect the Codex ownership index") from exc
    if not known:
        known = _rollout_cwds(codex_home, sid)
    return known


def _other_cwd_owner(folder, sid, cwd, codex_home, *, unknown=None, for_close=False, run=None):
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
            unresolved.append((other, pids))
            if unknown is not None:
                unknown.add(other)
            continue
        if any(os.path.realpath(path) == os.path.realpath(cwd) for path in cwds):
            return other
    if unresolved and unknown is None and not for_close:
        raise ValueError(_unknown_writer_message(*unresolved[0]))
    return None


def _pid_has_lock(path, pid, run):
    if type(pid) is not int or pid <= 0:
        return False
    result = run(["lsof", "-nP", "-t", path], timeout=5)
    return result.returncode == 0 and str(pid) in result.stdout.splitlines()


def _safe_archive_hint(cwd, run):
    """Only suggest archiving a reserved t worktree, never a Codex-managed one."""
    canonical = os.path.realpath(cwd)
    if "/codex-worktrees/" in canonical + "/" or "/.codex/worktrees/" in canonical + "/":
        return False
    try:
        if not stat.S_ISREG(os.lstat(os.path.join(cwd, ".git")).st_mode):
            return False
        result = run(["git", "-C", cwd, "rev-parse", "--absolute-git-dir"], timeout=5)
        if result.returncode or not os.path.isabs(result.stdout.strip()):
            return False
        private = result.stdout.strip()
        marker = os.path.join(private, "t-app-slot")
        if os.path.lexists(os.path.join(private, "codex-thread.json")):
            return False
        if not stat.S_ISREG(os.lstat(marker).st_mode):
            return False
        with open(marker, encoding="utf-8") as handle:
            return handle.read(32) == "codex-app\n"
    except (OSError, ValueError):
        return False


def _release_guidance(cwd, archived, run):
    if not archived and _safe_archive_hint(cwd, run):
        return ("Open this conversation in Codex, finish its turn, then Archive it "
                "(Cmd-Shift-A) and retry; t will restore it in the CLI, and the app "
                "can stay open. ")
    return ("Finish its turn and quit Codex; if the app is already closed, "
            "wait for its background backend to exit. ")


def _other_release_guidance(cwd, run):
    if run is not None and _safe_archive_hint(cwd, run):
        return ("Finish that other conversation's turn and Archive it in Codex "
                "(Cmd-Shift-A), then wait for its writer to release. ")
    return ("Finish that other conversation's turn and exit its owning client, "
            "then wait for its backend to release the writer. ")


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
        uri = "file:" + quote(db, safe="/") + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=0.5)) as connection:
            rows = connection.execute(
                "select id,cwd,archived,rollout_path from threads where id=?", (sid,)).fetchall()
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
                    archived=False, cli_pid=None, retry=_RETRY, unknown=None, for_close=False):
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
                                 "contract. " + _release_guidance(cwd, archived, run) + retry)
        if held and not (cli_pid is not None and _pid_has_lock(target, cli_pid, run)):
            raise ValueError("the selected Codex conversation is still loaded "
                             "(its writer lock is held). "
                             + _release_guidance(cwd, archived, run) + retry)
        try:
            other = _other_cwd_owner(folder, sid, cwd, codex_home, unknown=unknown,
                                     for_close=for_close, run=run)
        except ValueError as exc:
            raise ValueError(str(exc) + ". " + retry) from exc
        if other:
            if _safe_archive_hint(cwd, run):
                guidance = "Finish and Archive that other conversation in Codex before handoff. "
            else:
                guidance = "Wait for its owning backend to exit before handoff. "
            raise ValueError("another Codex conversation in this worktree is still loaded "
                             f"({other}). " + guidance + retry)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("could not verify the selected Codex rollout or writer lock: "
                         + str(exc) + ". " + retry) from exc


def preflight_cli_release(sid, cwd, *, codex_home, rollout, unknown=None,
                          run=None, retry="Retry: t open <repo> <slot> --app."):
    """Validate an exact CLI-to-app handoff before stopping its CLI frontend.

    A shared app-server may own the writer lock instead of the CLI PID. A
    positive held lock also establishes the contract for histories whose first
    metadata line predates the writer-lock implementation.
    """
    if not _UUID.fullmatch(sid):
        raise ValueError("the selected Codex thread id is invalid")
    try:
        version = _verify_rollout(rollout, sid, cwd)
        folder = _lock_dir(codex_home)
        if folder is None:
            raise ValueError("the selected Codex conversation has no verifiable writer-lock directory")
        held = _lock_held(os.path.join(folder, sid + ".lock"))
        contract = "modern" if version is not None and version >= (0, 159, 0) else None
        if contract is None and held is True:
            contract = "held"
        if contract is None:
            raise ValueError("the selected Codex conversation has no verifiable writer-lock contract")
        other = _other_cwd_owner(folder, sid, cwd, codex_home, unknown=unknown, run=run)
        if other:
            raise ValueError("another Codex conversation in this worktree is still loaded "
                             f"({other}). " + _other_release_guidance(cwd, run) + retry)
        return contract
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("could not verify the selected Codex conversation before stopping "
                         f"its CLI: {exc}") from exc


@_spinner('Waiting for conversation release')
def wait_cli_released(sid, cwd, *, codex_home, rollout, contract=None,
                      timeout=75, interval=0.2, on_wait=None,
                      retry="Retry: t open <repo> <slot> --app.", unknown=None, run=None):
    """Wait for this thread's writer to unload, without affecting other threads.

    `contract` must come from preflight_cli_release before the CLI is stopped;
    this matters for old saved metadata once Codex removes the target lock.
    """
    if not _UUID.fullmatch(sid):
        raise ValueError("the selected Codex thread id is invalid. " + retry)
    if contract not in ("modern", "held"):
        raise ValueError("the CLI release was not verified before stopping it. " + retry)
    if timeout <= 0 or interval <= 0:
        raise ValueError("the CLI release wait duration is invalid")
    try:
        _verify_rollout(rollout, sid, cwd)
        folder = _lock_dir(codex_home)
        if folder is None:
            raise ValueError("the Codex writer-lock directory disappeared. " + retry)
        target = os.path.join(folder, sid + ".lock")
        deadline = time.monotonic() + timeout
        announced = False
        while True:
            other = _other_cwd_owner(folder, sid, cwd, codex_home, unknown=unknown, run=run)
            if other:
                raise ValueError("another Codex conversation in this worktree is still loaded "
                                 f"({other}). " + _other_release_guidance(cwd, run) + retry)
            if _lock_held(target) is not True:
                return
            if not announced and on_wait is not None:
                on_wait()
                announced = True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ValueError("the selected Codex conversation is still loaded after "
                                 f"waiting {timeout:g}s for its CLI backend to release it. "
                                 + retry)
            time.sleep(min(interval, remaining))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("could not verify the selected Codex conversation was released: "
                         + str(exc) + ". " + retry) from exc


def assert_workspace_released(codex_home, cwd, *, retry="Retry: t kill <repo> <slot>."):
    """Verify an unselected workspace has only missing archives and no associated writer.

    Codex can retain archived index rows after their rollout files disappear.
    These are history, not owners. Keep the rows untouched and reject every
    active, existing, malformed, or out-of-home record. Callers recheck after
    cleanup so a new conversation still prevents reservation removal.
    """
    try:
        folder = _lock_dir(codex_home)
        if folder is None:
            raise ValueError("the Codex writer-lock directory is unavailable. " + retry)
        db = os.path.join(codex_home, "state_5.sqlite")
        if not stat.S_ISREG(os.lstat(db).st_mode):
            raise ValueError("the Codex conversation index is unavailable. " + retry)
        uri = "file:" + quote(db, safe="/") + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=0.5)) as connection:
            records = connection.execute(
                "select id,cwd,archived,rollout_path from threads").fetchall()
        for sid, recorded_cwd, archived, rollout in records:
            if (not isinstance(recorded_cwd, str)
                    or os.path.realpath(recorded_cwd) != os.path.realpath(cwd)):
                continue
            if (not isinstance(sid, str) or not _UUID.fullmatch(sid)
                    or archived != 1 or not isinstance(rollout, str)
                    or not os.path.isabs(rollout)
                    or not _archived_in_home(codex_home, rollout)):
                raise ValueError("this desktop workspace has a saved Codex conversation "
                                 "that could not be selected. " + retry)
            try:
                os.lstat(rollout)
            except FileNotFoundError:
                pass
            else:
                raise ValueError("this desktop workspace has a saved Codex conversation "
                                 "that could not be selected. " + retry)
        other = _other_cwd_owner(folder, "00000000-0000-0000-0000-000000000000",
                                 cwd, codex_home, for_close=True)
        if other:
            raise ValueError("a Codex conversation in this worktree is still loaded "
                             f"({other}). " + retry)
    except (OSError, sqlite3.Error) as exc:
        raise ValueError("could not verify the empty desktop workspace: "
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
