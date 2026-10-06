#!/usr/bin/env python3
"""Exercise the production desktop handoff against disposable Codex and tmux.

Both conversations, the Git worktree, Codex home, fake login, and tmux socket
exist only under a TemporaryDirectory. No turn or model request is started.
"""

from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import runpy
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import uuid

from poc_desktop_release import Rpc


ROOT = Path(__file__).resolve().parents[1]
HANDOFF = runpy.run_path(str(ROOT / "libexec" / "t_app_handoff.py"))


def command(argv: list[str], env: dict[str, str], cwd: Path, *, input: str | None = None,
            timeout: float = 15, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(argv, env=env, cwd=cwd, input=input,
                            capture_output=True, text=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError(f"{shlex.join(argv[:2])} failed: {result.stderr[-600:]}")
    return result


@contextmanager
def isolated_environment(env: dict[str, str]):
    old = dict(os.environ)
    os.environ.clear()
    os.environ.update(env)
    try:
        yield
    finally:
        os.environ.clear()
        os.environ.update(old)


def rollout_path(codex_home: Path, sid: str) -> Path:
    db = codex_home / "state_5.sqlite"
    with sqlite3.connect(f"file:{db}?mode=ro", uri=True) as connection:
        found = connection.execute("select rollout_path from threads where id=?", (sid,)).fetchone()
    if not found or not found[0]:
        raise RuntimeError("synthetic thread has no persisted rollout")
    return Path(found[0])


def run(binary: Path, history_version: str | None = None) -> dict:
    tmux_binary = shutil.which("tmux")
    zsh_binary = shutil.which("zsh")
    if not tmux_binary or not zsh_binary:
        raise RuntimeError("tmux and zsh are required")
    with tempfile.TemporaryDirectory(prefix="t-handoff-e2e-") as scratch:
        # macOS exposes temporary files under both /var and /private/var.
        # Codex stores the canonical spelling in SQLite, so use it everywhere.
        root = Path(scratch).resolve()
        for name in ("home", "codex", "config", "cache", "data", "state", "tmp", "bin", "repo", "other"):
            (root / name).mkdir()
        home, codex_home = root / "home", root / "codex"
        env = dict(
            PATH=str(root / "bin") + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            TERM="xterm-256color", LANG="C",
            HOME=str(home), ZDOTDIR=str(home), CODEX_HOME=str(codex_home),
            XDG_CONFIG_HOME=str(root / "config"), XDG_CACHE_HOME=str(root / "cache"),
            XDG_DATA_HOME=str(root / "data"), XDG_STATE_HOME=str(root / "state"),
            TMPDIR=str(root / "tmp"), TMUX_TMPDIR=str(root / "tmp"),
            GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
            T_RECOVERY_DISABLE="1", T_NO_AUTORELOAD="1", T_NO_UPDATE_CHECK="1",
        )
        (root / "bin" / "codex").symlink_to(binary)
        (root / "bin" / "t").symlink_to(ROOT / "bin" / "t")
        tmux_socket = root / "tmp" / "tmux.sock"
        tmux_wrapper = root / "bin" / "tmux"
        tmux_wrapper.write_text(
            "#!/bin/sh\nexec " + shlex.quote(tmux_binary) + " -S "
            + shlex.quote(str(tmux_socket)) + " -f /dev/null \"$@\"\n"
        )
        tmux_wrapper.chmod(0o755)
        config_text = (
            'model_provider = "poc"\nmodel = "poc"\n'
            '[features]\nplugins = false\n'
            '[model_providers.poc]\nname = "No inference POC"\n'
            'base_url = "http://127.0.0.1:9/v1"\nwire_api = "responses"\n'
            'requires_openai_auth = false\n'
        )
        (codex_home / "config.toml").write_text(config_text)
        (home / ".zshenv").write_text("skip_global_compinit=1\n")
        (home / ".zshrc").write_text(
            "export PATH=" + shlex.quote(env["PATH"]) + "\n"
            "source " + shlex.quote(str(ROOT / "t.plugin.zsh")) + "\n"
        )
        version = command([str(binary), "--version"], env, root / "repo").stdout.strip()
        command(["git", "init", "-q", "-b", "main", str(root / "repo")], env, root)
        (root / "repo" / "README.md").write_text("synthetic handoff worktree\n")
        command(["git", "add", "README.md"], env, root / "repo")
        command(["git", "-c", "user.name=POC", "-c", "user.email=poc@example.invalid",
                 "commit", "-qm", "fixture"], env, root / "repo")
        worktree = root / "worktrees" / "repo" / "1"
        worktree.parent.mkdir(parents=True)
        command(["git", "worktree", "add", "-q", "-b", "dev/poc-1", str(worktree)],
                env, root / "repo")
        with (codex_home / "config.toml").open("a") as config_file:
            for trusted in {str(root / "repo"), os.path.realpath(root / "repo"),
                            str(worktree), os.path.realpath(worktree)}:
                config_file.write("\n[projects." + json.dumps(trusted) + "]\n"
                                  'trust_level = "trusted"\n')
        gitdir = command(["git", "-C", str(worktree), "rev-parse", "--absolute-git-dir"],
                         env, worktree).stdout.strip()
        marker = Path(gitdir) / "t-app-slot"
        marker.write_text("codex-app\n")
        (root / "config" / "t").mkdir()
        (root / "config" / "t" / "local.zsh").write_text(
            "DEV_REPOS[repo]=" + shlex.quote(str(root / "repo")) + "\n"
            "DEV_WORKTREE_ROOT=" + shlex.quote(str(root / "worktrees")) + "\n"
        )
        # The TUI checks login before it will resume, even with a local dummy
        # provider. This fake key is written only into the temporary Codex home.
        command([str(binary), "login", "--with-api-key"], env, worktree,
                input="sk-poc-never-used\n")

        desktop_errors = (root / "desktop.log").open("wb")
        retry_errors = (root / "retry.log").open("wb")
        desktop = Rpc([str(binary), "app-server", "--stdio"], env, worktree,
                      stderr=desktop_errors)
        retry = None
        started_tmux = False
        try:
            try:
                desktop.initialize(experimental=True)
            except Exception:
                desktop_errors.flush()
                print((root / "desktop.log").read_text(errors="replace")[-2000:], file=sys.stderr)
                raise
            retry = Rpc([str(binary), "app-server", "--stdio"], env, worktree,
                        stderr=retry_errors)
            try:
                retry.initialize()
            except Exception:
                retry_errors.flush()
                print((root / "retry.log").read_text(errors="replace")[-2000:], file=sys.stderr)
                raise
            if history_version:
                target = str(uuid.uuid4())
                now = datetime.now(timezone.utc)
                timestamp = now.isoformat().replace("+00:00", "Z")
                folder = codex_home / "sessions" / now.strftime("%Y/%m/%d")
                folder.mkdir(parents=True, exist_ok=True)
                old_rollout = folder / (f"rollout-{now.strftime('%Y-%m-%dT%H-%M-%S')}-{target}.jsonl")
                records = [
                    {"timestamp": timestamp, "type": "session_meta", "payload": {
                        "id": target, "session_id": target, "timestamp": timestamp,
                        "cwd": str(worktree), "originator": "codex-tui",
                        "cli_version": history_version, "source": "cli"}},
                    {"timestamp": timestamp, "type": "response_item", "payload": {
                        "type": "message", "role": "user", "content": [{
                            "type": "input_text", "text": "synthetic target fixture"}]}}
                ]
                old_rollout.write_text("".join(json.dumps(row) + "\n" for row in records))
                resumed = desktop.request("thread/resume", {"threadId": target,
                                                            "path": str(old_rollout),
                                                            "cwd": str(worktree)})
                assert resumed["thread"]["id"] == target
            else:
                target = desktop.request("thread/start", {"cwd": str(worktree), "approvalPolicy": "never"})["thread"]["id"]
            control = desktop.request("thread/start", {"cwd": str(root / "other"), "approvalPolicy": "never"})["thread"]["id"]
            for sid, label in (((control, "control"),) if history_version else
                               ((target, "target"), (control, "control"))):
                desktop.request("thread/inject_items", {"threadId": sid, "items": [{
                    "type": "message", "role": "user", "content": [{
                        "type": "input_text", "text": "synthetic " + label + " fixture",
                    }],
                }]})
            # inject_items persists a rollout but deliberately skips the search
            # index used for a real user's first message. Populate that one
            # synthetic index field so production's strict UI proof applies.
            with sqlite3.connect(codex_home / "state_5.sqlite") as db:
                db.execute("update threads set first_user_message=? where id=?",
                           ("synthetic target fixture", target))
                db.commit()
            listed = command([zsh_binary, "-lic", "_codex_threads_for_cwd "
                              + shlex.quote(str(worktree)) + " --include-archived"],
                             env, worktree).stdout
            assert any(line.split("\t", 1)[0] == target for line in listed.splitlines()), (
                "production thread selector could not find synthetic target")
            active_rollout = rollout_path(codex_home, target)
            assert active_rollout.is_file()
            assert target in desktop.request("thread/loaded/list")["data"]
            assert control in desktop.request("thread/loaded/list")["data"]

            def helper_run(argv, **kwargs):
                return command(argv, env, worktree, check=False, **kwargs)

            check = HANDOFF["assert_released"]
            blocked = False
            try:
                check(None, target, str(worktree), helper_run, codex_home=str(codex_home),
                      rollout=str(active_rollout), archived=False, retry="Retry t open repo 1 --cli.")
            except ValueError as exc:
                blocked = "still loaded" in str(exc)
            assert blocked, "production helper permitted a second writer"

            desktop.request("thread/archive", {"threadId": target})
            loaded = desktop.request("thread/loaded/list")["data"]
            assert target not in loaded and control in loaded
            archived_rollout = rollout_path(codex_home, target)
            assert archived_rollout.is_file() and b"synthetic target fixture" in archived_rollout.read_bytes()
            with sqlite3.connect(f"file:{codex_home / 'state_5.sqlite'}?mode=ro", uri=True) as db:
                archived_row = db.execute("select cwd, archived, rollout_path from threads where id=?",
                                          (target,)).fetchone()
            assert archived_row == (str(worktree), 1, str(archived_rollout))
            archived_open = command(["lsof", "-nP", "-t", "--", str(archived_rollout)],
                                    env, worktree, check=False).stdout.strip()
            assert not archived_open, "owning backend still has archived rollout open"
            if history_version:
                old_meta = json.loads(archived_rollout.read_text().splitlines()[0])["payload"]
                assert old_meta["cli_version"] == history_version
            check(None, target, str(worktree), helper_run, codex_home=str(codex_home),
                  rollout=str(archived_rollout), archived=True)
            with isolated_environment(env):
                HANDOFF["unarchive_thread"](target, str(worktree),
                                            codex_home=str(codex_home), rollout=str(archived_rollout))
            restored_rollout = rollout_path(codex_home, target)
            assert restored_rollout.is_file() and b"synthetic target fixture" in restored_rollout.read_bytes()
            assert control in desktop.request("thread/loaded/list")["data"]
            handoff = command([zsh_binary, "-lic",
                               "_t_app_pull_slot dev-repo-1 " + shlex.quote(str(worktree)) + " "
                               + target + " - " + shlex.quote(str(codex_home))],
                              env, worktree, timeout=30, check=False)
            started_tmux = tmux_socket.exists()
            if handoff.returncode:
                pane = command(["tmux", "capture-pane", "-p", "-t", "=dev-repo-1:"],
                               env, worktree, check=False).stdout[-700:]
                panes = command(["tmux", "list-panes", "-a", "-F",
                                 "#{session_name} #{pane_pid} #{pane_current_command} #{pane_dead}"],
                                env, worktree, check=False).stdout.strip()
                lock = codex_home / "thread-writer-locks" / (target + ".lock")
                holders = command(["lsof", "-nP", "-t", str(lock)], env, worktree,
                                  check=False).stdout.strip()
                owner_probe = command([zsh_binary, "-lic", "_t_app_pull_owner dev-repo-1 "
                                       + shlex.quote(str(worktree)) + " " + target + " "
                                       + shlex.quote(str(codex_home))], env, worktree,
                                      check=False)
                details = command([zsh_binary, "-lic",
                                   "print -r -- agent=$(_dev_agent_of_session dev-repo-1) "
                                   "pid=$(_dev_session_claude_pid dev-repo-1) "
                                   "sid=$(_dev_session_sid dev-repo-1 " + shlex.quote(str(worktree)) + ") "
                                   "ready=$(_t_app_pull_ready dev-repo-1 "
                                   + shlex.quote(str(worktree)) + " " + target + " "
                                   + shlex.quote(holders.splitlines()[0] if holders else "0") + " "
                                   + shlex.quote(str(codex_home)) + "; print $?)"],
                                  env, worktree, check=False).stdout.strip()
                process_lines = command(["ps", "-Axo", "pid=,ppid=,comm="], env, worktree,
                                        check=False).stdout.splitlines()
                candidate_pids = {piece for line in panes.splitlines()
                                  for piece in line.split() if piece.isdecimal()}
                candidate_pids.update(holders.splitlines())
                process_details = [line.strip() for line in process_lines
                                   if line.strip().split(None, 1)[0] in candidate_pids]
                pane_pid = panes.split()[1] if len(panes.split()) > 1 else "0"
                holder_pid = holders.splitlines()[0] if holders else "0"
                args_line = command(["ps", "-ww", "-o", "args=", "-p", holder_pid],
                                    env, worktree, check=False).stdout.strip()
                with sqlite3.connect(f"file:{codex_home / 'state_5.sqlite'}?mode=ro", uri=True) as db:
                    row = db.execute("select cwd, archived, source, first_user_message, rollout_path "
                                     "from threads where id=?", (target,)).fetchone()
                meta = json.loads(restored_rollout.read_text().splitlines()[0])["payload"]
                ui_probe = command([zsh_binary, "-lic", "_t_app_pull_ui_ready dev-repo-1 "
                                    + shlex.quote(str(worktree)) + " " + target + " "
                                    + holder_pid + " " + shlex.quote(str(codex_home))],
                                   env, worktree, check=False)
                snapshot = command([zsh_binary, "-lic",
                                    "_dev_ps_snapshot; "
                                    "print -r -- snap_ok=$(_dev_snap_ok; print $?) "
                                    "pane=\"${_DEV_PANE_PIDS[dev-repo-1]}\" "
                                    "pane_comm=\"${_DEV_PS_COMM[" + pane_pid + "]}\" "
                                    "holder_comm=\"${_DEV_PS_COMM[" + holder_pid + "]}\" "
                                    "holder_parent=\"${_DEV_PS_PPID[" + holder_pid + "]}\" "
                                    "children=\"${_DEV_PS_KIDS[" + pane_pid + "]}\""],
                                   env, worktree, check=False).stdout.strip()
                tmux_from_zsh = command([zsh_binary, "-lic",
                                         "print -r -- path=$(command -v tmux) "
                                         "tmux_env=${TMUX:-none}; "
                                         "tmux list-panes -a -F "
                                         '"#{session_name}"$\'\\t\'"#{pane_pid}"'],
                                        env, worktree, check=False)
                raise RuntimeError("production tmux handoff failed: " + handoff.stderr[-700:]
                                   + " pane=" + pane + " panes=" + panes
                                   + " lock_exists=" + str(lock.exists())
                                   + " lock_holders=" + holders
                                   + " owner_probe=" + owner_probe.stdout.strip()
                                   + " details=" + details
                                   + " processes=" + repr(process_details)
                                   + " argv=" + repr(args_line)
                                   + " db=" + repr(row)
                                   + " meta=" + repr({key: meta.get(key) for key in
                                                     ('id', 'cwd', 'source', 'thread_source')})
                                   + " ui_probe=" + str(ui_probe.returncode)
                                   + " snapshot=" + snapshot
                                   + " tmux_zsh=" + repr((tmux_from_zsh.returncode,
                                                         tmux_from_zsh.stdout.strip(),
                                                         tmux_from_zsh.stderr[-150:]))
                                   + " owner_error=" + owner_probe.stderr[-300:])
            assert not marker.exists(), "production handoff retained its desktop marker"
            owner = command([zsh_binary, "-lic", "_t_app_pull_owner dev-repo-1 "
                             + shlex.quote(str(worktree)) + " " + target + " "
                             + shlex.quote(str(codex_home))], env, worktree,
                            check=False).stdout.strip()
            assert owner.isdecimal(), "production owner probe did not verify CLI"
            check(None, target, str(worktree), helper_run, codex_home=str(codex_home),
                  rollout=str(restored_rollout), archived=False, cli_pid=int(owner))
            second_blocked = False
            try:
                retry.request("thread/resume", {"threadId": target})
            except RuntimeError as exc:
                second_blocked = "active writer" in str(exc)
            assert second_blocked, "a second backend loaded the CLI-owned thread"
            return {
                "version": version,
                "production_helper_blocked_live_desktop_thread": blocked,
                "control_thread_remained_loaded": control in desktop.request("thread/loaded/list")["data"],
                "production_helper_allowed_archived_target": True,
                "archived_row_exact": True,
                "archived_rollout_open_fds": False,
                "history_version": history_version or version,
                "old_history_version_retained": bool(history_version),
                "production_unarchive_preserved_history": True,
                "production_tmux_started_exact_thread": True,
                "reservation_released_after_verified_cli": True,
                "production_owner_probe_found_cli": True,
                "helper_allows_verified_cli_retry": True,
                "other_backend_blocked_by_cli_writer": second_blocked,
                "inference_requested": False,
            }
        finally:
            if started_tmux:
                command(["tmux", "kill-server"], env, worktree, check=False)
            if retry is not None:
                retry.close()
            desktop.close()
            retry_errors.close()
            desktop_errors.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default=shutil.which("codex"))
    parser.add_argument("--history-version", help="seed an older synthetic rollout before modern resume")
    args = parser.parse_args()
    if not args.codex:
        parser.error("codex is required")
    print(json.dumps(run(Path(args.codex).resolve(), args.history_version), sort_keys=True, indent=2))
