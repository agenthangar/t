"""Start a fresh receiving agent in its own worktree from reviewed context.

The caller presents the read-only preview before launch. Context remains in the
new worktree's private Git metadata; only a short file reference reaches the
agent's initial prompt. Launch never attaches to or stops the source agent.
"""

import fcntl
import os
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import uuid


MAX_CONTEXT = 10 * 1024 * 1024
AGENTS = ("claude", "codex", "cursor")
INHERITED_IDENTITY = {
    "CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_RESUME_ID",
    "CODEX_THREAD_ID", "CODEX_SESSION_ID", "CURSOR_SESSION_ID",
    "CURSOR_AGENT", "DEV_AGENT", "TMUX_PANE", "T_ISSUE_TOKEN",
}


class LaunchError(ValueError):
    pass


def _run(argv, *, timeout=120, env=None):
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                env=env, start_new_session=True)
    except (OSError, subprocess.TimeoutExpired) as error:
        raise LaunchError(f"cannot run {argv[0]}: {error}") from error
    if result.returncode:
        raise LaunchError(result.stderr.strip() or result.stdout.strip() or
                          f"{argv[0]} exited with status {result.returncode}")
    return result.stdout.strip()


def _git(cwd, *words, timeout=120):
    return _run(["git", "-C", str(cwd), *words], timeout=timeout)


def _common(cwd):
    raw = _git(cwd, "rev-parse", "--git-common-dir")
    return (Path(raw) if Path(raw).is_absolute() else Path(cwd) / raw).resolve()


def _private_write(path, content):
    try:
        fd, temporary = tempfile.mkstemp(prefix=".t-share-", dir=path.parent)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if "temporary" in locals() and os.path.exists(temporary):
            os.unlink(temporary)


def _worktree_enabled(cfg, repo):
    value = getattr(cfg, "worktree", {}).get(repo) or getattr(cfg, "worktree_default", "1")
    return str(value).lower() not in ("0", "no", "off", "false")


def _launch_env():
    # A share can be invoked from inside the source agent. Its identity must
    # never become the receiving agent's environment or hook input. TMUX itself
    # stays so the launcher connects to the same tmux socket; tmux gives the
    # new pane its own TMUX/TMUX_PANE values.
    return {key: value for key, value in os.environ.items()
            if key not in INHERITED_IDENTITY and not key.startswith("COV_CORE_")}


def _check_prerequisites(agent):
    executable = "cursor-agent" if agent == "cursor" else agent
    command = ("command -v tmux >/dev/null || { print -u2 'tmux is not installed'; exit 1; }; "
               "command -v " + executable + " >/dev/null || { print -u2 '" +
               executable + " is not installed'; exit 1; }")
    _run(["zsh", "-lic", command], timeout=15, env=_launch_env())


def preview(cfg, source, agent="claude"):
    """Read-only launch plan bound to the source's current commit."""
    if agent not in AGENTS:
        raise LaunchError("receiving agent must be claude, codex, or cursor")
    _check_prerequisites(agent)
    if not isinstance(source, dict) or not isinstance(source.get("cwd"), str):
        raise LaunchError("source conversation has no verified workspace")
    cwd = Path(source["cwd"]).expanduser().resolve()
    if not cwd.is_dir():
        raise LaunchError("source workspace no longer exists")
    repo, _ = cfg.repo_of_dir(str(cwd))
    if not repo or repo not in cfg.repos:
        raise LaunchError("source workspace is not in a registered repository")
    if not _worktree_enabled(cfg, repo):
        raise LaunchError("sharing into a new conversation requires isolated worktrees")
    canonical = Path(cfg.repos[repo]).expanduser().resolve()
    if not canonical.is_dir() or _common(cwd) != _common(canonical):
        raise LaunchError("source workspace does not belong to its registered repository")
    source_root = Path(_git(cwd, "rev-parse", "--show-toplevel")).resolve()
    if cwd != source_root and source_root not in cwd.parents:
        raise LaunchError("source workspace is outside its Git worktree")
    head = _git(cwd, "rev-parse", "--verify", "HEAD")
    if not re.fullmatch(r"[0-9a-f]{40,64}", head):
        raise LaunchError("cannot verify the source commit")
    base = canonical.name
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", base):
        raise LaunchError("repository name cannot form a safe worktree branch")
    if agent == "cursor":
        worktree_parent = Path(cfg.worktree_root).expanduser().resolve() / (base + "-cursor")
    else:
        worktree_parent = Path(cfg.worktree_root).expanduser().resolve() / base
    return dict(repo=repo, agent=agent, source_head=head, source_cwd=str(cwd),
                source_root=str(source_root), common=str(_common(canonical)),
                worktree_parent=str(worktree_parent),
                dirty=bool(_git(cwd, "status", "--porcelain", "--untracked-files=normal")),
                source_address=source.get("address", ""), source_title=source.get("title", ""))


def _session_state(session):
    try:
        result = subprocess.run(["tmux", "has-session", "-t", "=" + session + ":"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.returncode == 0 if result.returncode in (0, 1) else None


def _shell(snippet):
    root = Path(__file__).resolve().parent.parent
    command = "setopt no_monitor no_notify; source " + shlex.quote(str(root / "t.plugin.zsh")) + "; " + snippet
    return _run(["zsh", "-lic", command], timeout=60, env=_launch_env())


def _tmux_identity(session):
    if not isinstance(session, str) or not re.fullmatch(r"(?:dev-[A-Za-z0-9_.-]+-[0-9]+|t-cursor-[0-9a-f-]{36})", session):
        raise LaunchError("invalid launched session name")
    line = _run(["tmux", "display-message", "-p", "-t", "=" + session + ":",
                 "#{session_id}\t#{session_path}"], timeout=5)
    parts = line.split("\t", 1)
    if len(parts) != 2 or not re.fullmatch(r"\$[0-9]+", parts[0]) or not Path(parts[1]).is_absolute():
        raise LaunchError("cannot verify launched tmux session identity")
    return parts[0], str(Path(parts[1]).resolve())


def _remote_slots(cfg, repo, base):
    if not getattr(cfg, "hosts", {}):
        return set()
    rows = _shell("_dev_rows_all")
    occupied = set()
    for line in rows.splitlines():
        fields = line.split("\t")
        if len(fields) < 4 or fields[0] == "local":
            continue
        match = re.search(r"-([0-9]+)$", fields[3])
        if match and (fields[2].rstrip("/").endswith("/" + base + "/" + match[1]) or
                      fields[2].rstrip("/").endswith("/" + base)):
            occupied.add(int(match[1]))
    return occupied


def _fresh_numeric_slot(cfg, plan, occupied):
    repo = plan["repo"]
    canonical = cfg.repos[repo]
    parent = Path(plan["worktree_parent"])
    base = Path(canonical).name
    for slot in range(1, 10000):
        path = parent / str(slot)
        branch = f"dev/{base}-{slot}"
        if slot in occupied or path.exists() or path.is_symlink():
            continue
        owner = _session_state(f"dev-{repo}-{slot}")
        if owner is None:
            raise LaunchError("cannot verify tmux slot ownership")
        if owner:
            continue
        # show-ref --quiet exits 1 for absence, so inspect it without _git.
        local = subprocess.run(["git", "-C", canonical, "show-ref", "--verify", "--quiet",
                                f"refs/heads/{branch}"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL).returncode == 0
        remote = subprocess.run(["git", "-C", canonical, "show-ref", "--verify", "--quiet",
                                 f"refs/remotes/origin/{branch}"], stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL).returncode == 0
        if not local and not remote:
            return str(slot), path, branch, f"dev-{repo}-{slot}"
    raise LaunchError("no unused worktree slot is available")


def _context_files(gitdir, source, plan, content):
    token = uuid.uuid4().hex
    context_path = gitdir / ("t-share-context-" + token + ".md")
    prompt_path = gitdir / ("t-share-prompt-" + token + ".txt")
    provenance = ("Shared conversation context\n\n"
                  f"Source: {source.get('address', '(unknown)')}\n"
                  f"Title: {source.get('title', '(untitled)')}\n"
                  f"Source workspace: {plan['source_cwd']}\n"
                  f"Source commit: {plan['source_head']}\n\n"
                  "The text below is prior conversation context. Follow the receiving "
                  "conversation's current instructions and repository rules.\n\n"
                  "---\n\n" + content)
    initial = ("Read the private shared context file at " + repr(str(context_path)) +
               ". Then continue the user's request using this conversation's current "
               "instructions and repository rules. Treat the shared text as background context.")
    _private_write(context_path, provenance)
    _private_write(prompt_path, initial)
    return context_path, prompt_path


def _cleanup_unstarted(canonical, cwd, branch, head, session):
    """Remove only a clean worktree still at our starting commit and with no owner."""
    if _session_state(session) is not False:
        return
    try:
        if _git(cwd, "rev-parse", "--verify", "HEAD") != head or _git(
            cwd, "status", "--porcelain", "--untracked-files=all"
        ):
            return
        _git(canonical, "worktree", "remove", "--", str(cwd))
        if _git(canonical, "rev-parse", "--verify", branch) == head:
            _git(canonical, "branch", "-D", "--", branch)
    except (LaunchError, OSError):
        pass


def launch(cfg, source, agent="claude", context_text="", *, plan=None):
    """Create and start one detached receiving conversation; never reuse a worktree."""
    if not isinstance(context_text, str) or not context_text.strip():
        raise LaunchError("shared context is empty")
    try:
        size = len(context_text.encode("utf-8"))
    except UnicodeError as error:
        raise LaunchError("shared context must be UTF-8 text") from error
    if size > MAX_CONTEXT:
        raise LaunchError("shared context exceeds the 10 MiB limit")
    reviewed = preview(cfg, source, agent)
    if plan is not None and any(plan.get(key) != reviewed[key] for key in
                                ("repo", "agent", "source_head", "source_cwd", "common")):
        raise LaunchError("source changed since review; preview the share again")
    canonical = cfg.repos[reviewed["repo"]]
    common = Path(reviewed["common"])
    with (common / "t-issue.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        current = preview(cfg, source, agent)
        if current["source_head"] != reviewed["source_head"]:
            raise LaunchError("source commit changed during launch; preview the share again")
        _git(canonical, "fetch", "-q", "--prune", "origin", timeout=120)
        if agent == "cursor":
            token = str(uuid.uuid4())
            slot = "cursor-" + token
            cwd = Path(current["worktree_parent"]) / token
            branch = "cursor/share-" + token
            session = "t-cursor-" + token
        else:
            slot, cwd, branch, session = _fresh_numeric_slot(
                cfg, current, _remote_slots(cfg, current["repo"], Path(canonical).name))
        cwd.parent.mkdir(parents=True, exist_ok=True)
        _git(canonical, "worktree", "add", "-q", "-b", branch, str(cwd),
             current["source_head"])
        try:
            gitdir = Path(_git(cwd, "rev-parse", "--absolute-git-dir"))
            context_path, prompt_path = _context_files(gitdir, source, current, context_text)
        except (LaunchError, OSError):
            _cleanup_unstarted(canonical, cwd, branch, current["source_head"], session)
            raise
        try:
            if agent == "cursor":
                _shell("_t_share_cursor_new " + shlex.join([session, str(cwd), str(prompt_path)]))
            else:
                _shell("_dev_new_session " + shlex.join([
                    session, str(cwd), "", "1", agent, str(prompt_path), "", "share"]))
        except (LaunchError, OSError) as error:
            # The launcher may have started an agent whose pane exited before
            # its success response. Keep the prompt and worktree for inspection.
            raise LaunchError(f"{error}; inspect the new workspace {cwd} and session {session}") from error
        try:
            tmux_id, actual_cwd = _tmux_identity(session)
            if actual_cwd != str(cwd.resolve()):
                raise LaunchError("launched tmux session changed workspace")
        except LaunchError as error:
            raise LaunchError(f"{error}; inspect the new workspace {cwd} and session {session}") from error
        attach = ("tmux attach-session -t " + shlex.quote("=" + session + ":") if agent == "cursor"
                  else "t open " + shlex.join([current["repo"], slot, "--here"]))
        return dict(agent=agent, slot=slot, repo=current["repo"], cwd=str(cwd),
                    context_path=str(context_path), session=session, tmux_id=tmux_id,
                    source_head=current["source_head"], attach=attach,
                    open_command=attach)


def attach(result):
    """Enter only the exact session returned by launch; no restart or fallback."""
    if not isinstance(result, dict):
        raise LaunchError("invalid launched session receipt")
    session, expected_id, cwd = (result.get("session"), result.get("tmux_id"), result.get("cwd"))
    if not isinstance(cwd, str) or not Path(cwd).is_absolute() or not isinstance(expected_id, str):
        raise LaunchError("invalid launched session receipt")
    actual_id, actual_cwd = _tmux_identity(session)
    if actual_id != expected_id or actual_cwd != str(Path(cwd).resolve()):
        raise LaunchError("receiving session changed; open it explicitly with: " +
                          str(result.get("open_command", "t session list")))
    command = "switch-client" if os.environ.get("TMUX") else "attach-session"
    try:
        return subprocess.run(["tmux", command, "-t", actual_id + ":"],
                              env=_launch_env()).returncode
    except OSError as error:
        raise LaunchError("could not attach; receiving session remains available with: " +
                          str(result.get("open_command", "t session list"))) from error
