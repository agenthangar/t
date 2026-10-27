"""Fresh share receivers use synthetic Git repos and no real tmux or agent."""

import importlib.util
import os
from pathlib import Path
import shlex
import subprocess
from types import SimpleNamespace

import pytest
from test_agent_seam import zsh  # noqa: F401 -- sandboxed shell and tmux fixture


ROOT = Path(__file__).resolve().parents[1]


def load_launcher():
    spec = importlib.util.spec_from_file_location("share_launch_test", ROOT / "libexec/t_share_launch.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def launcher(monkeypatch):
    module = load_launcher()
    monkeypatch.setattr(module, "_check_prerequisites", lambda agent: None)
    monkeypatch.setattr(module, "_session_state", lambda session: False)
    def fake_identity(session):
        parent = Path(os.environ["HOME"]) / "code" / ".worktrees"
        if session.startswith("t-cursor-"):
            return "$42", str(parent / "repo-cursor" / session.removeprefix("t-cursor-"))
        return "$42", str(parent / "repo" / ("repo-" + session.rsplit("-", 1)[1]))
    monkeypatch.setattr(module, "_tmux_identity", fake_identity)
    return module


def git(cwd, *args):
    result = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    monkeypatch.setenv("XDG_STATE_HOME", str(home / ".local" / "state"))
    monkeypatch.setenv("T_RECOVERY_DISABLE", "1")
    origin = tmp_path / "origin.git"
    canonical = home / "code" / "repo"
    canonical.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "--bare", str(origin)], check=True, capture_output=True)
    subprocess.run(["git", "init", "-b", "main", str(canonical)], check=True, capture_output=True)
    git(canonical, "config", "user.name", "Share Test")
    git(canonical, "config", "user.email", "share@example.test")
    (canonical / "README.md").write_text("Project\n")
    git(canonical, "add", "README.md")
    git(canonical, "commit", "-m", "Initial")
    git(canonical, "remote", "add", "origin", str(origin))
    git(canonical, "push", "-u", "origin", "main")
    worktree_root = home / "code" / ".worktrees"
    cfg = SimpleNamespace(
        repos={"repo": str(canonical)}, worktree_root=str(worktree_root),
        worktree_default="1", worktree={}, hosts={},
        repo_of_dir=lambda cwd: ("repo", "") if str(cwd).startswith(str(canonical)) else (None, None),
    )
    source = dict(agent="claude", id="source", address="claude:source", cwd=str(canonical),
                  title="Synthetic source", host="local")
    return cfg, source, canonical


@pytest.mark.parametrize("agent", ["claude", "codex", "cursor"])
def test_launch_creates_private_context_on_source_head_without_attaching(
    launcher, repo, monkeypatch, agent
):
    cfg, source, canonical = repo
    calls = []
    monkeypatch.setattr(launcher, "_shell", lambda command: calls.append(command))
    reviewed = launcher.preview(cfg, source, agent)
    assert reviewed["dirty"] is False
    result = launcher.launch(cfg, source, agent, "Important source decision", plan=reviewed)

    cwd = Path(result["cwd"])
    assert cwd.is_dir()
    assert git(cwd, "rev-parse", "HEAD") == reviewed["source_head"]
    assert git(canonical, "status", "--porcelain") == ""
    context = Path(result["context_path"])
    assert context.is_file() and context.stat().st_mode & 0o777 == 0o600
    assert "Important source decision" in context.read_text()
    assert source["address"] in context.read_text()
    assert source["title"] in context.read_text()
    assert len(calls) == 1
    assert "Important source decision" not in calls[0]
    assert "_t_share_cursor_new" in calls[0] if agent == "cursor" else "_dev_new_session" in calls[0]
    if agent == "cursor":
        assert result["session"].startswith("t-cursor-")
        assert "/repo-cursor/" in result["cwd"]
        assert git(cwd, "symbolic-ref", "--short", "HEAD").startswith("cursor/share-")
    else:
        assert result["slot"] == "1"
        assert result["session"] == "dev-repo-1"
        assert git(cwd, "symbolic-ref", "--short", "HEAD") == "dev/repo-1"


def test_preview_reports_dirty_source_and_launch_rejects_changed_head(launcher, repo, monkeypatch):
    cfg, source, canonical = repo
    (canonical / "scratch.txt").write_text("uncommitted")
    reviewed = launcher.preview(cfg, source)
    assert reviewed["dirty"] is True
    git(canonical, "add", "scratch.txt")
    git(canonical, "commit", "-m", "Another commit")
    monkeypatch.setattr(launcher, "_shell", lambda command: pytest.fail("must not launch"))
    with pytest.raises(launcher.LaunchError, match="source changed"):
        launcher.launch(cfg, source, "claude", "Context", plan=reviewed)
    assert not Path(reviewed["worktree_parent"]).exists()


@pytest.mark.parametrize("agent", ["claude", "cursor"])
def test_failed_start_preserves_context_when_agent_may_have_exited(
    launcher, repo, monkeypatch, agent
):
    cfg, source, canonical = repo
    monkeypatch.setattr(launcher, "_shell", lambda command: (_ for _ in ()).throw(
        launcher.LaunchError("startup failed")))
    monkeypatch.setattr(launcher, "_session_state", lambda session: False)
    with pytest.raises(launcher.LaunchError, match="startup failed"):
        launcher.launch(cfg, source, agent, "Context")
    assert git(canonical, "worktree", "list", "--porcelain").count("worktree ") == 2
    assert list(Path(cfg.worktree_root).rglob("t-share-context-*.md")) == []  # Git metadata only


def test_failed_start_preserves_worktree_when_owner_may_exist(launcher, repo, monkeypatch):
    cfg, source, canonical = repo
    monkeypatch.setattr(launcher, "_shell", lambda command: (_ for _ in ()).throw(
        launcher.LaunchError("startup receipt lost")))
    monkeypatch.setattr(launcher, "_session_state", lambda session: None)
    with pytest.raises(launcher.LaunchError, match="cannot verify tmux slot ownership"):
        launcher.launch(cfg, source, "claude", "Context")
    assert not Path(cfg.worktree_root, "repo", "1").exists()
    assert git(canonical, "branch", "--list", "dev/repo-1") == ""


def test_prelaunch_artifact_failure_cleans_only_new_worktree(launcher, repo, monkeypatch):
    cfg, source, canonical = repo
    monkeypatch.setattr(launcher, "_context_files", lambda *args: (_ for _ in ()).throw(
        OSError("cannot save prompt")))
    monkeypatch.setattr(launcher, "_session_state", lambda session: False)
    monkeypatch.setattr(launcher, "_shell", lambda command: pytest.fail("must not launch"))
    with pytest.raises(OSError, match="cannot save prompt"):
        launcher.launch(cfg, source, "claude", "Context")
    assert git(canonical, "worktree", "list", "--porcelain").count("worktree ") == 1
    assert git(canonical, "branch", "--list", "dev/repo-1") == ""


def test_preview_rejects_shared_tree_and_unregistered_source(launcher, repo):
    cfg, source, canonical = repo
    cfg.worktree["repo"] = "0"
    with pytest.raises(launcher.LaunchError, match="isolated worktrees"):
        launcher.preview(cfg, source)
    cfg.worktree.clear()
    source["cwd"] = str(canonical.parent)
    with pytest.raises(launcher.LaunchError, match="registered repository"):
        launcher.preview(cfg, source)


def test_launch_rejects_invalid_context_before_creating_worktree(launcher, repo, monkeypatch):
    cfg, source, canonical = repo
    monkeypatch.setattr(launcher, "MAX_CONTEXT", 4)
    with pytest.raises(launcher.LaunchError, match="exceeds"):
        launcher.launch(cfg, source, "codex", "longer")
    with pytest.raises(launcher.LaunchError, match="empty"):
        launcher.launch(cfg, source, "codex", "")
    with pytest.raises(launcher.LaunchError, match="UTF-8"):
        launcher.launch(cfg, source, "codex", "\ud800")
    assert git(canonical, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_fresh_slot_skips_live_tmux_name_even_without_worktree(launcher, repo, monkeypatch):
    cfg, source, _ = repo
    monkeypatch.setattr(launcher, "_session_state", lambda session: session == "dev-repo-1")
    monkeypatch.setattr(launcher, "_shell", lambda command: None)
    result = launcher.launch(cfg, source, "claude", "Context")
    assert result["slot"] == "2" and result["session"] == "dev-repo-2"


@pytest.mark.parametrize("inside_tmux", [False, True])
def test_attach_exact_launched_session_without_restart(launcher, monkeypatch, tmp_path, inside_tmux):
    cwd = tmp_path / "receiver"
    cwd.mkdir()
    receipt = dict(session="dev-repo-2", tmux_id="$42", cwd=str(cwd),
                   open_command="t open repo 2 --here")
    monkeypatch.setattr(launcher, "_tmux_identity", lambda session: ("$42", str(cwd)))
    if inside_tmux:
        monkeypatch.setenv("TMUX", "/tmp/isolated-tmux-socket,1,0")
    else:
        monkeypatch.delenv("TMUX", raising=False)
    calls = []
    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=7)
    monkeypatch.setattr(launcher.subprocess, "run", fake_run)
    assert launcher.attach(receipt) == 7
    assert calls[0][0] == ["tmux", "switch-client" if inside_tmux else "attach-session",
                           "-t", "$42:"]
    assert "TMUX" in calls[0][1]["env"] if inside_tmux else "TMUX" not in calls[0][1]["env"]


@pytest.mark.parametrize("identity", [("$41", "same"), ("$42", "other")])
def test_attach_refuses_reused_or_moved_session(launcher, monkeypatch, tmp_path, identity):
    cwd = tmp_path / "receiver"
    cwd.mkdir()
    actual_cwd = cwd if identity[1] == "same" else tmp_path
    monkeypatch.setattr(launcher, "_tmux_identity", lambda session: (identity[0], str(actual_cwd)))
    monkeypatch.setattr(launcher.subprocess, "run", lambda *args, **kwargs: pytest.fail("must not attach"))
    receipt = dict(session="dev-repo-2", tmux_id="$42", cwd=str(cwd),
                   open_command="t open repo 2 --here")
    with pytest.raises(launcher.LaunchError, match="t open repo 2 --here"):
        launcher.attach(receipt)


def test_attach_errors_preserve_open_command(launcher, monkeypatch, tmp_path):
    cwd = tmp_path / "receiver"
    cwd.mkdir()
    monkeypatch.setattr(launcher, "_tmux_identity", lambda session: ("$42", str(cwd)))
    monkeypatch.setattr(launcher.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(
        OSError("socket disappeared")))
    with pytest.raises(launcher.LaunchError, match="t open repo 2 --here"):
        launcher.attach(dict(session="dev-repo-2", tmux_id="$42", cwd=str(cwd),
                             open_command="t open repo 2 --here"))
    with pytest.raises(launcher.LaunchError, match="invalid launched session receipt"):
        launcher.attach(None)


def test_launch_preserves_workspace_if_tmux_identity_cannot_be_verified(
    launcher, repo, monkeypatch
):
    cfg, source, canonical = repo
    monkeypatch.setattr(launcher, "_shell", lambda command: None)
    monkeypatch.setattr(launcher, "_tmux_identity", lambda session: ("$42", "/wrong/worktree"))
    with pytest.raises(launcher.LaunchError, match="changed workspace.*inspect the new workspace"):
        launcher.launch(cfg, source, "claude", "Context")
    assert git(canonical, "worktree", "list", "--porcelain").count("worktree ") == 2


def test_source_agent_identity_is_removed_from_launch_shell_environment(
    launcher, monkeypatch
):
    for key in launcher.INHERITED_IDENTITY:
        monkeypatch.setenv(key, "source-identity")
    monkeypatch.setenv("TMUX", "/tmp/private-socket,1,0")
    monkeypatch.setenv("CODEX_HOME", "/tmp/target-codex-home")
    monkeypatch.setenv("COV_CORE_SOURCE", "source")
    captured = {}

    def fake_run(argv, **kwargs):
        captured.update(kwargs["env"])
        return ""

    monkeypatch.setattr(launcher, "_run", fake_run)
    launcher._shell("_dev_new_session test")
    assert all(key not in captured for key in launcher.INHERITED_IDENTITY)
    assert "COV_CORE_SOURCE" not in captured
    assert captured["TMUX"] == "/tmp/private-socket,1,0"
    assert captured["CODEX_HOME"] == "/tmp/target-codex-home"


def test_prerequisite_check_uses_login_shell_and_names_missing_agent(monkeypatch):
    launcher = load_launcher()
    calls = []

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        raise launcher.LaunchError("cursor-agent is not installed")

    monkeypatch.setattr(launcher, "_run", fake_run)
    with pytest.raises(launcher.LaunchError, match="cursor-agent is not installed"):
        launcher._check_prerequisites("cursor")
    assert calls[0][0][:2] == ["zsh", "-lic"]
    assert "command -v tmux" in calls[0][0][2]
    assert "command -v cursor-agent" in calls[0][0][2]


def test_preview_rejects_invalid_source_and_wrong_git_tree(launcher, repo, monkeypatch, tmp_path):
    cfg, source, canonical = repo
    with pytest.raises(launcher.LaunchError, match="receiving agent"):
        launcher.preview(cfg, source, "unknown")
    with pytest.raises(launcher.LaunchError, match="verified workspace"):
        launcher.preview(cfg, {})
    with pytest.raises(launcher.LaunchError, match="no longer exists"):
        launcher.preview(cfg, {**source, "cwd": str(tmp_path / "missing")})
    monkeypatch.setattr(launcher, "_common", lambda cwd: Path(str(cwd) + "-different")
                        if Path(cwd) == canonical else Path(cwd))
    # A different registered checkout must not borrow the source commit.
    cfg.repos["repo"] = str(tmp_path / "missing-canonical")
    with pytest.raises(launcher.LaunchError, match="registered repository"):
        launcher.preview(cfg, source)


def test_preview_rejects_git_boundary_and_bad_head(launcher, repo, monkeypatch, tmp_path):
    cfg, source, canonical = repo
    real_git = launcher._git
    def fake_git(cwd, *words, **kwargs):
        if words == ("rev-parse", "--show-toplevel"):
            return str(tmp_path / "other-root")
        return real_git(cwd, *words, **kwargs)
    monkeypatch.setattr(launcher, "_git", fake_git)
    with pytest.raises(launcher.LaunchError, match="outside its Git worktree"):
        launcher.preview(cfg, source)
    def bad_head(cwd, *words, **kwargs):
        if words == ("rev-parse", "--verify", "HEAD"):
            return "invalid head"
        return real_git(cwd, *words, **kwargs)
    monkeypatch.setattr(launcher, "_git", bad_head)
    with pytest.raises(launcher.LaunchError, match="cannot verify the source commit"):
        launcher.preview(cfg, source)


def test_tmux_state_and_identity_parser_are_fail_closed(monkeypatch, tmp_path):
    module = load_launcher()
    for code, expected in [(0, True), (1, False), (2, None)]:
        monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=code))
        assert module._session_state("dev-repo-1") is expected
    monkeypatch.setattr(module.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("tmux")))
    assert module._session_state("dev-repo-1") is None
    with pytest.raises(module.LaunchError, match="invalid launched session name"):
        module._tmux_identity("-unsafe")
    monkeypatch.setattr(module, "_run", lambda *args, **kwargs: "not-an-id\t/tmp")
    with pytest.raises(module.LaunchError, match="cannot verify"):
        module._tmux_identity("dev-repo-1")
    monkeypatch.setattr(module, "_run", lambda *args, **kwargs: "$12\trelative")
    with pytest.raises(module.LaunchError, match="cannot verify"):
        module._tmux_identity("dev-repo-1")
    monkeypatch.setattr(module, "_run", lambda *args, **kwargs: "$12\t" + str(tmp_path))
    assert module._tmux_identity("dev-repo-1") == ("$12", str(tmp_path))


def test_remote_slots_only_reserve_matching_repository(launcher, repo, monkeypatch):
    cfg, _, _ = repo
    assert launcher._remote_slots(cfg, "repo", "repo") == set()
    cfg.hosts["remote"] = "example.test"
    rows = "\n".join([
        "local\trepo\t/code/repo\tdev-repo-1",
        "remote\trepo\t/code/.worktrees/repo/2\tdev-repo-2",
        "remote\trepo\t/code/.worktrees/other/3\tdev-other-3",
        "remote\trepo\t/code/repo\tdev-repo-4",
        "malformed",
    ])
    monkeypatch.setattr(launcher, "_shell", lambda snippet: rows)
    assert launcher._remote_slots(cfg, "repo", "repo") == {2, 4}


def test_private_write_removes_temp_on_error(launcher, tmp_path, monkeypatch):
    destination = tmp_path / "private.txt"
    monkeypatch.setattr(launcher.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("rename failed")))
    with pytest.raises(OSError, match="rename failed"):
        launcher._private_write(destination, "secret")
    assert list(tmp_path.iterdir()) == []


def test_cleanup_never_removes_owned_or_changed_worktree(launcher, repo, monkeypatch):
    cfg, source, canonical = repo
    head = git(canonical, "rev-parse", "HEAD")
    cwd = Path(cfg.worktree_root) / "repo" / "1"
    cwd.parent.mkdir(parents=True)
    git(canonical, "worktree", "add", "-q", "-b", "dev/repo-1", str(cwd), head)
    monkeypatch.setattr(launcher, "_session_state", lambda session: True)
    launcher._cleanup_unstarted(str(canonical), cwd, "dev/repo-1", head, "dev-repo-1")
    assert cwd.is_dir()
    monkeypatch.setattr(launcher, "_session_state", lambda session: False)
    (cwd / "changed.txt").write_text("preserve")
    launcher._cleanup_unstarted(str(canonical), cwd, "dev/repo-1", head, "dev-repo-1")
    assert cwd.is_dir()
    (cwd / "changed.txt").unlink()
    monkeypatch.setattr(launcher, "_git", lambda *args, **kwargs: (_ for _ in ()).throw(
        launcher.LaunchError("git unavailable")))
    launcher._cleanup_unstarted(str(canonical), cwd, "dev/repo-1", head, "dev-repo-1")
    assert cwd.is_dir()


def test_launch_detects_source_change_inside_lock(launcher, repo, monkeypatch):
    cfg, source, canonical = repo
    actual_preview = launcher.preview
    counter = 0
    def changing_preview(*args):
        nonlocal counter
        counter += 1
        result = actual_preview(*args)
        if counter == 2:
            return {**result, "source_head": "0" * 40}
        return result
    monkeypatch.setattr(launcher, "preview", changing_preview)
    with pytest.raises(launcher.LaunchError, match="changed during launch"):
        launcher.launch(cfg, source, "claude", "Context")
    assert git(canonical, "worktree", "list", "--porcelain").count("worktree ") == 1


def test_cursor_shell_launcher_sends_only_private_prompt_path(zsh, tmp_path):
    """Use the established sandbox tmux stub; no real Cursor client starts."""
    executable = zsh.log.parent / "stubbin" / "cursor-agent"
    executable.write_text("#!/bin/sh\nexit 99\n")
    executable.chmod(0o755)
    sid = "12345678-1234-1234-1234-123456789abc"
    session = "t-cursor-" + sid
    cwd = zsh.home / "code" / "cursor-worktree"
    cwd.mkdir(parents=True)
    prompt = tmp_path / "initial-prompt.txt"
    prompt.write_text("Private context must stay out of tmux commands")
    snippet = (
        "_t_share_install_cursor_hook() { return 0; }; "
        "_dev_auto_trust() { return 0; }; "
        "_t_share_cursor_new " + shlex.join([session, str(cwd), str(prompt)])
    )
    result = zsh(snippet)
    assert result.returncode == 0, result.stderr
    commands = zsh.log.read_text()
    assert "new-session -d -s " + session in commands
    assert "send-keys -t =" + session + ":" in commands
    assert "cursor-agent" in commands and "$(cat --" in commands
    assert "Private context must stay out of tmux commands" not in commands


def test_cursor_shell_launcher_rejects_invalid_session_before_tmux(zsh, tmp_path):
    cwd = zsh.home / "code" / "cursor-worktree"
    cwd.mkdir(parents=True)
    prompt = tmp_path / "prompt.txt"
    prompt.write_text("Context")
    result = zsh("_t_share_cursor_new " + shlex.join(["dev-repo-1", str(cwd), str(prompt)]))
    assert result.returncode != 0
    assert "invalid Cursor session name" in result.stderr
    assert not zsh.log.exists() or "new-session" not in zsh.log.read_text()


@pytest.mark.parametrize("agent", ["claude", "codex", "cursor"])
def test_guarded_shell_launcher_starts_fake_agent_with_private_initial_prompt(
    zsh, tmp_path, agent
):
    """A tmux stub executes only the newly created pane's command in sandbox HOME."""
    bins = zsh.log.parent / "stubbin"
    tmux = bins / "tmux"
    body = tmux.read_text()
    tmux.write_text(body.replace(
        'case "$1" in',
        'if [[ "$1" == send-keys || "$1" == respawn-pane ]]; then\n'
        '  launch="$4"; [[ "$1" == respawn-pane ]] && launch="$9"\n'
        '  (cd "$FAKE_SESSION_CWD" && env CLAUDECODE=old CODEX_THREAD_ID=old T_ISSUE_TOKEN=old CLAUDE_RESUME_ID=old zsh -f -c "$launch")\n'
        'fi\n'
        'case "$1" in',
        1,
    ))
    fake_agent = bins / ("cursor-agent" if agent == "cursor" else agent)
    fake_agent.write_text('#!/bin/sh\nprintf "%s\\n" "$*" > "$AGENT_LOG"\n'
                          'printf "%s|%s|%s|%s\\n" "${CLAUDECODE-unset}" "${CLAUDE_RESUME_ID-unset}" "${CODEX_THREAD_ID-unset}" "${T_ISSUE_TOKEN-unset}" > "$AGENT_ENV_LOG"\n')
    fake_agent.chmod(0o755)
    cwd = zsh.home / "code" / (agent + "-target")
    cwd.mkdir(parents=True)
    context = tmp_path / "context.md"
    context.write_text("FULL PRIVATE CONTEXT")
    prompt = tmp_path / "initial.txt"
    prompt.write_text("Read private context file " + str(context))
    log = tmp_path / "agent-argv.txt"
    env_log = tmp_path / "agent-env.txt"
    sid = "12345678-1234-1234-1234-123456789abc"
    session = "t-cursor-" + sid if agent == "cursor" else "dev-" + agent + "-1"
    prefix = "_dev_auto_trust() { return 0; }; _dev_history_record() { return 0; }; "
    if agent == "cursor":
        prefix += "_t_share_install_cursor_hook() { return 0; }; "
        command = "_t_share_cursor_new " + shlex.join([session, str(cwd), str(prompt)])
    else:
            command = "_dev_new_session " + shlex.join([
                session, str(cwd), "", "1", agent, str(prompt), "", "share"
            ])
    result = zsh(prefix + command, FAKE_SESSION_CWD=str(cwd), AGENT_LOG=str(log),
                 AGENT_ENV_LOG=str(env_log))
    assert result.returncode == 0, result.stderr
    assert log.is_file()
    argv = log.read_text()
    assert "Read private context file " + str(context) in argv
    assert "FULL PRIVATE CONTEXT" not in zsh.log.read_text()
    assert "$(cat --" in zsh.log.read_text()
    assert env_log.read_text().strip() == "unset|unset|unset|unset"
    send_command = next(line for line in zsh.log.read_text().splitlines() if "send-keys" in line or "respawn-pane" in line)
    assert "CLAUDE_RESUME_ID" in send_command
    if agent == "claude":
        assert "--session-id" in argv
        assert "0f0e0d0c-0b0a-0908-0706-050403020100" in argv
