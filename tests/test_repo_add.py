"""Register disposable existing checkouts without altering their Git state."""
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def box(tmp_path, monkeypatch, t_mod):
    home = tmp_path / "home"
    repo = home / "config" / "scripts"
    repo.mkdir(parents=True)
    git(repo, "init", "-q", "-b", "custom")
    git(repo, "-c", "user.name=Tester", "-c", "user.email=test@example.invalid",
        "commit", "--allow-empty", "-qm", "initial")
    git(repo, "remote", "add", "origin", "https://example.invalid/scripts.git")
    (repo / "untracked").write_text("keep\n")
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("COV_CORE_") and key not in ("T_LOCAL_RC", "ZDOTDIR")}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"),
               XDG_CACHE_HOME=str(home / ".cache"), XDG_STATE_HOME=str(home / ".local/state"),
               T_NO_UPDATE_CHECK="1", T_RECOVERY_DISABLE="1", T_AUTO_TRUST="0")
    def run(*words, cwd=repo):
        return subprocess.run([sys.executable, str(ROOT / "bin/t"), *words], cwd=cwd,
                              env=env, capture_output=True, text=True, timeout=20)
    monkeypatch.setattr(t_mod, "_new_sh_whence", lambda alias: None)
    cfg = SimpleNamespace(repos={}, worktree_root=str(home / "worktrees"))
    return run, repo, home / ".config/t/local.zsh", cfg


def test_add_existing_checkout_and_repeat(box):
    run, repo, local, _ = box
    original = git(repo, "status", "--porcelain"), git(repo, "rev-parse", "HEAD"), (repo / ".git/config").read_text()
    sub = repo / "nested"
    sub.mkdir()
    added = run("repo", "add", "--alias", "my-scripts", cwd=sub)
    assert added.returncode == 0, added.stderr
    assert "Registered: my-scripts" in added.stdout
    saved = local.read_text()
    assert run("repo", "locate", "my-scripts").stdout.strip() == str(repo)
    repeated = run("repo", "add", str(repo))
    assert repeated.returncode == 0 and "Already registered" in repeated.stdout
    assert local.read_text() == saved
    assert original == (git(repo, "status", "--porcelain"), git(repo, "rev-parse", "HEAD"), (repo / ".git/config").read_text())


def test_add_dry_run_does_not_write(box):
    run, repo, local, _ = box
    result = run("repo", "add", str(repo), "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "Would register: scripts" in result.stdout
    assert not local.parent.exists()


def test_add_conflict_preserves_registration(box):
    run, repo, local, _ = box
    assert run("repo", "add").returncode == 0
    before = local.read_text()
    other = repo.parent / "other"
    other.mkdir()
    git(other, "init", "-q")
    result = run("repo", "add", str(other), "--alias", "scripts")
    assert result.returncode == 1 and "already maps" in result.stderr
    assert local.read_text() == before


def test_add_target_path_alias_and_safety(box, t_mod, monkeypatch):
    _, repo, _, cfg = box
    monkeypatch.chdir(repo)
    assert t_mod._repo_add_target(cfg, None) == ("scripts", str(repo))
    cfg.repos = {"mine": str(repo)}
    assert t_mod._repo_add_target(cfg, str(repo))[0] == "mine"
    for alias in ("bad;key", "__add__", "__back__"):
        with pytest.raises(ValueError, match="repo alias"):
            t_mod._repo_add_target(cfg, str(repo), alias)
    cfg.repos = {"conflict": str(repo.parent / "elsewhere" / "scripts")}
    with pytest.raises(ValueError, match="directory name"):
        t_mod._repo_add_target(cfg, str(repo))
    cfg.repos = {}
    monkeypatch.setattr(t_mod, "_new_sh_whence", lambda alias: "command")
    with pytest.raises(ValueError, match="shadow"):
        t_mod._repo_add_target(cfg, str(repo))
    with pytest.raises(ValueError, match="not a Git checkout"):
        t_mod._repo_add_target(cfg, str(repo.parent))
    cfg.worktree_root = str(repo.parent)
    with pytest.raises(ValueError, match="per-session worktrees"):
        t_mod._repo_add_target(cfg, str(repo))


def test_add_rejects_linked_worktree(box, t_mod):
    run, repo, local, cfg = box
    linked = repo.parent / "linked"
    git(repo, "worktree", "add", "-qb", "linked", str(linked))
    with pytest.raises(ValueError, match="linked worktree"):
        t_mod._repo_add_target(cfg, str(linked))
    result = run("repo", "add", str(linked))
    assert result.returncode == 1 and "linked worktree" in result.stderr
    assert not local.exists()


def test_setup_defaults_to_config(box):
    run, repo, local, _ = box
    result = run("config", "setup", "--dry-run", "--no-hosts")
    assert result.returncode == 0, result.stderr
    assert "DEV_REPOS[scripts]" in result.stdout
    assert not local.exists()


def test_repo_probe_failure(box, t_mod, monkeypatch):
    _, repo, _, cfg = box
    original = t_mod._run
    monkeypatch.setattr(t_mod, "_run", lambda cmd: SimpleNamespace(returncode=1, stdout="")
                        if cmd[-1] == "--absolute-git-dir" else original(cmd))
    with pytest.raises(ValueError, match="could not verify"):
        t_mod._repo_checkout_path(cfg, str(repo))
