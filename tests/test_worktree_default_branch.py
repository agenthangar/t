"""Default-branch discovery and worktree safety against disposable Git remotes."""
import shlex
import subprocess

import pytest

from test_plugin import shell


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repository(tmp_path):
    def create(branch):
        repo = tmp_path / "code with spaces" / "repo"
        repo.mkdir(parents=True)
        git(repo, "init", "-q", "-b", branch)
        git(repo, "config", "user.name", "Test")
        git(repo, "config", "user.email", "test@example.invalid")
        (repo / "tracked").write_text("committed\n")
        git(repo, "add", ".")
        git(repo, "commit", "-qm", "initial")
        remote = tmp_path / "remote.git"
        subprocess.run(["git", "clone", "-q", "--bare", str(repo), str(remote)], check=True)
        git(repo, "remote", "add", "origin", str(remote))
        git(repo, "fetch", "-q", "origin")
        git(repo, "remote", "set-head", "origin", "-a")
        return repo
    return create


def run(tmp_path, repo, code):
    return shell(tmp_path, 'tmux() { return 1; }; gh() { return 1; }; '
                 '_dev_auto_trust() { :; }; ' + code,
                 local_text=f"DEV_REPOS[api]={shlex.quote(str(repo))}\n"
                            'DEV_WORKTREE_ROOT="$HOME/work trees"\n')


@pytest.mark.parametrize("branch,head,main,expected", [
    ("master", "master", False, "master"),
    ("release/stable", "release/stable", True, "release/stable"),
    ("main", None, False, "main"),
    ("master", None, False, "master"),
    ("master", "missing", False, "master"),
    ("master", None, True, "main"),
    ("release/stable", None, False, None),
    ("master", "local", False, "master"),
])
def test_fetched_default_resolvers_agree(tmp_path, repository, t_mod, branch, head, main, expected):
    repo = repository(branch)
    if main:
        git(repo, "update-ref", "refs/remotes/origin/main", "HEAD")
    if head is None:
        git(repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    else:
        target = "refs/heads/master" if head == "local" else f"refs/remotes/origin/{head}"
        git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", target)
    result = run(tmp_path, repo, '_dev_origin_base "$DEV_REPOS[api]"')
    assert (result.returncode == 0) == (expected is not None), result.stderr
    assert result.stdout.strip() == (f"origin/{expected}" if expected else "")
    assert t_mod._new_sh_origin_base(str(repo)) == (f"origin/{expected}" if expected else None)


@pytest.mark.parametrize("branch", ["main", "master", "release/stable"])
@pytest.mark.parametrize("offline", [False, True])
def test_new_worktree_uses_published_default_and_preserves_dirty_checkout(tmp_path, repository, branch, offline):
    repo = repository(branch)
    (repo / "tracked").write_text("uncommitted work\n")
    if offline:
        git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    result = run(tmp_path, repo, '_dev_worktree_create api 1')
    assert result.returncode == 0, result.stdout + result.stderr
    wt = tmp_path / "home/work trees/repo/repo-1"
    assert result.stdout.strip() == str(wt)
    assert git(wt, "rev-parse", "HEAD") == git(repo, "rev-parse", f"origin/{branch}")
    assert git(wt, "branch", "--show-current") == "dev/repo-1"
    assert (repo / "tracked").read_text() == "uncommitted work\n"
    assert (wt / "tracked").read_text() == "committed\n"
    guidance = run(tmp_path, repo, '_dev_worktree_refuse api 2')
    assert f"origin/{branch}" in guidance.stderr


@pytest.mark.parametrize("lingering", [False, True])
def test_missing_default_never_deletes_a_lingering_branch(tmp_path, repository, lingering):
    repo = repository("release/stable")
    git(repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    if lingering:
        git(repo, "branch", "dev/repo-1")
    result = run(tmp_path, repo, '_dev_branch_merged() { return 0; }; _dev_worktree_create api 1')
    assert result.returncode != 0
    assert not (tmp_path / "home/work trees/repo/repo-1/.git").exists()
    if lingering:
        assert git(repo, "rev-parse", "refs/heads/dev/repo-1") == git(repo, "rev-parse", "HEAD")


@pytest.mark.parametrize("remote_only", [False, True])
def test_unmerged_branch_resumes_without_default(tmp_path, repository, remote_only):
    repo = repository("release/stable")
    git(repo, "branch", "dev/repo-1")
    if remote_only:
        git(repo, "update-ref", "refs/remotes/origin/dev/repo-1", "HEAD")
        git(repo, "branch", "-D", "dev/repo-1")
    git(repo, "symbolic-ref", "--delete", "refs/remotes/origin/HEAD")
    git(repo, "remote", "set-url", "origin", str(tmp_path / "missing.git"))
    result = run(tmp_path, repo, '_dev_worktree_create api 1')
    assert result.returncode == 0, result.stderr
    wt = tmp_path / "home/work trees/repo/repo-1"
    assert git(wt, "rev-parse", "HEAD") == git(repo, "rev-parse", "HEAD")
    assert git(wt, "branch", "--show-current") == "dev/repo-1"


def test_local_branch_cannot_shadow_origin_default(tmp_path, repository):
    repo = repository("master")
    git(repo, "branch", "origin/master")
    result = run(tmp_path, repo, '_dev_worktree_create api 1')
    assert result.returncode == 0, result.stderr
    wt = tmp_path / "home/work trees/repo/repo-1"
    (repo / "tracked").write_text("published update\n")
    git(repo, "commit", "-qam", "advance default")
    git(repo, "push", "-q", "origin", "master")
    result = run(tmp_path, repo, '_dev_worktree_create api 1')
    assert result.returncode == 0, result.stderr
    assert git(wt, "rev-parse", "HEAD") == git(repo, "rev-parse", "refs/remotes/origin/master")
    assert git(wt, "rev-parse", "HEAD") != git(repo, "rev-parse", "refs/heads/origin/master")


@pytest.mark.parametrize("branch", ["master", "release/stable"])
@pytest.mark.parametrize("guard", ["clean", "dirty", "live", "unmerged", "equal"])
def test_refresh_and_merge_proof_preserve_work(tmp_path, repository, branch, guard):
    repo = repository(branch)
    result = run(tmp_path, repo, '_dev_worktree_create api 1')
    assert result.returncode == 0, result.stderr
    wt = tmp_path / "home/work trees/repo/repo-1"
    initial = git(wt, "rev-parse", "HEAD")
    if guard != "equal":
        (repo / "tracked").write_text("published update\n")
        git(repo, "commit", "-qam", "advance default")
        git(repo, "push", "-q", "origin", branch)
    if guard == "dirty":
        (wt / "tracked").write_text("keep my work\n")
    if guard == "unmerged":
        (wt / "new").write_text("unmerged\n")
        git(wt, "add", ".")
        git(wt, "commit", "-qm", "unmerged work")
    before = git(wt, "rev-parse", "HEAD")
    live = f'tmux() {{ print -r -- {shlex.quote(str(wt))}; }}; ' if guard == "live" else ""
    result = run(tmp_path, repo, live + '_dev_worktree_create api 1')
    assert result.returncode == 0, result.stderr
    assert git(wt, "rev-parse", "HEAD") == (git(repo, "rev-parse", "HEAD") if guard == "clean" else before)
    if guard == "clean":
        assert f"reset to origin/{branch}" in result.stderr
        assert git(wt, "rev-parse", "HEAD") != initial
    if guard == "dirty":
        assert (wt / "tracked").read_text() == "keep my work\n"
