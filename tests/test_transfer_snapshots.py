"""Moves publish independent revisions without replacing another host's branch."""
from pathlib import Path
import os
import shlex
import subprocess

import pytest

from test_plugin import shell
from test_agent_seam import zsh  # noqa: F401
from test_move_local_receive import RECEIVE, output


def git(repo, *args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True,
                                   stderr=subprocess.PIPE).strip()


def commit(repo, message):
    git(repo, 'add', '-A')
    git(repo, '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', message)
    return git(repo, 'rev-parse', 'HEAD')


@pytest.fixture
def repos(tmp_path):
    origin = tmp_path/'origin.git'
    subprocess.run(['git', 'init', '-q', '--bare', str(origin)], check=True)
    source = tmp_path/'source'/'repo'; source.parent.mkdir()
    subprocess.run(['git', 'clone', '-q', str(origin), str(source)], check=True)
    git(source, 'checkout', '-qb', 'main')
    (source/'tracked').write_text('base\n'); (source/'deleted').write_text('old\n')
    commit(source, 'base'); git(source, 'push', '-q', 'origin', 'main')
    git(origin, 'symbolic-ref', 'HEAD', 'refs/heads/main')
    destination = tmp_path/'destination'/'repo'; destination.parent.mkdir()
    subprocess.run(['git', 'clone', '-q', str(origin), str(destination)], check=True)
    roots = [tmp_path/'source-trees', tmp_path/'destination-trees']
    worktrees = [root/'repo'/'repo-2' for root in roots]
    for repo, wt in zip((source, destination), worktrees):
        git(repo, 'worktree', 'add', '-qb', 'dev/repo-2', str(wt), 'main')
    (worktrees[1]/'other-host').write_text('preserve me\n')
    other = commit(worktrees[1], 'independent destination work')
    git(worktrees[1], 'push', '-q', 'origin', 'HEAD:dev/repo-2')
    (worktrees[0]/'tracked').write_text('latest source\n')
    (worktrees[0]/'untracked').write_text('carry me too\n')
    (worktrees[0]/'deleted').unlink()
    def run(index, command):
        repo = (source, destination)[index]
        return shell(tmp_path/('sender' if index == 0 else 'receiver'),
            'tmux() { return 1; }; ' + command,
            local_text=f'DEV_REPOS[repo]={shlex.quote(str(repo))}\nDEV_WORKTREE_ROOT={shlex.quote(str(roots[index]))}\n',
            extra_env={'T_NO_AUTORELOAD':'1', 'T_NO_UPDATE_CHECK':'1',
                       'GIT_CONFIG_GLOBAL':os.devnull, 'GIT_CONFIG_NOSYSTEM':'1',
                       'GIT_AUTHOR_NAME':'Test', 'GIT_AUTHOR_EMAIL':'test@example.invalid',
                       'GIT_COMMITTER_NAME':'Test', 'GIT_COMMITTER_EMAIL':'test@example.invalid'})
    return origin, source, destination, worktrees, other, run


def publish(repos):
    origin, source, destination, wts, other, run = repos
    prepared = run(0, f'_dev_worktree_beam_prepare {shlex.quote(str(wts[0]))}')
    assert prepared.returncode == 0, prepared.stderr
    ref = prepared.stdout.strip()
    assert ref.startswith('refs/heads/t-transfer/')
    pushed = run(0, f'TB_TRANSFER_REF={ref}; _dev_worktree_beam_push {shlex.quote(str(wts[0]))} receiver')
    assert pushed.returncode == 0, pushed.stderr
    assert git(origin, 'rev-parse', 'dev/repo-2') == other
    assert not git(wts[0], 'status', '--porcelain')
    assert git(origin, 'rev-parse', ref) == git(wts[0], 'rev-parse', 'HEAD')
    return ref


@pytest.mark.parametrize('destination_exists', [True, False])
def test_snapshot_relands_divergent_slot_without_overwriting_branch(repos, destination_exists):
    origin, source, destination, wts, other, run = repos
    ref = publish(repos)
    if not destination_exists:
        git(destination, 'worktree', 'remove', str(wts[1]))
        git(destination, 'branch', '-D', 'dev/repo-2')
    landed = run(1, f'''TB_TRANSFER_REF={ref}
      land=$(_dev_beam_land_cwd {shlex.quote(str(wts[1]))} '' sender codex) || exit $?
      _dev_worktree_beam_sync "$land" || exit $?
      print -r -- "$land"
    ''')
    assert landed.returncode == 0, landed.stderr
    land = Path(landed.stdout.splitlines()[-1])
    assert land != wts[1]
    assert (land/'tracked').read_text() == 'latest source\n'
    assert (land/'untracked').read_text() == 'carry me too\n'
    assert not (land/'deleted').exists()
    assert git(land, 'rev-parse', 'HEAD') == git(wts[0], 'rev-parse', 'HEAD')
    assert git(origin, 'rev-parse', 'dev/repo-2') == other
    if destination_exists:
        assert git(wts[1], 'rev-parse', 'HEAD') == other
        assert (wts[1]/'other-host').read_text() == 'preserve me\n'
    done = run(1, f'TB_TRANSFER_REF={ref}; _dev_worktree_beam_finish {shlex.quote(str(land))}')
    assert done.returncode == 0, done.stderr
    assert not git(origin, 'for-each-ref', '--format=%(refname)', ref)
    assert git(land, 'rev-parse', f'refs/t/transfers/{ref.rsplit("/",1)[1]}') == git(wts[0], 'rev-parse', 'HEAD')


def test_missing_snapshot_never_falls_back_to_unrelated_origin_branch(repos):
    origin, source, destination, wts, other, run = repos
    ref = publish(repos)
    git(origin, 'update-ref', '-d', ref)
    result = run(1, f'TB_TRANSFER_REF={ref}; _dev_beam_land_cwd {shlex.quote(str(wts[1]))} "" sender codex')
    assert result.returncode == 1 and 'exact transfer snapshot' in result.stderr
    assert git(wts[1], 'rev-parse', 'HEAD') == other


def test_failed_preflight_preserves_uncommitted_source(repos):
    origin, source, destination, wts, other, run = repos
    before = git(wts[0], 'rev-parse', 'HEAD')
    git(source, 'remote', 'set-url', 'origin', str(origin.parent/'missing.git'))
    result = run(0, f'_dev_worktree_beam_prepare {shlex.quote(str(wts[0]))}')
    assert result.returncode == 1
    assert 'source was not stopped' in result.stderr and 'missing.git' in result.stderr
    assert git(wts[0], 'rev-parse', 'HEAD') == before
    assert (wts[0]/'untracked').read_text() == 'carry me too\n'


def test_existing_snapshot_cannot_be_overwritten(repos):
    origin, source, destination, wts, other, run = repos
    ref = publish(repos)
    old = git(origin, 'rev-parse', ref)
    (wts[0]/'later').write_text('second revision')
    result = run(0, f'TB_TRANSFER_REF={ref}; _dev_worktree_beam_push {shlex.quote(str(wts[0]))} receiver')
    assert result.returncode == 1
    assert 'couldn\'t publish' in result.stderr
    assert git(origin, 'rev-parse', ref) == old
    assert (wts[0]/'later').read_text() == 'second revision'


@pytest.mark.parametrize('reuse', [False, True])
def test_snapshot_materializes_or_fast_forwards_exact_revision(repos, reuse):
    origin, source, destination, wts, other, run = repos
    # With no unrelated branch on origin, an empty/ancestral local slot is reusable.
    git(origin, 'update-ref', '-d', 'refs/heads/dev/repo-2')
    git(wts[1], 'reset', '--hard', 'main')
    if not reuse:
        git(destination, 'worktree', 'remove', str(wts[1]))
        git(destination, 'branch', '-D', 'dev/repo-2')
    git(destination, 'fetch', '--prune', 'origin')
    prepared = run(0, f'_dev_worktree_beam_prepare {shlex.quote(str(wts[0]))}')
    ref = prepared.stdout.strip()
    assert not git(origin, 'for-each-ref', '--format=%(refname)', ref)
    pushed = run(0, f'TB_TRANSFER_REF={ref}; _dev_worktree_beam_push {shlex.quote(str(wts[0]))} receiver')
    assert pushed.returncode == 0, pushed.stderr
    result = run(1, f'''TB_TRANSFER_REF={ref}
      land=$(_dev_beam_land_cwd {shlex.quote(str(wts[1]))} '' sender codex) || exit $?
      _dev_worktree_beam_sync "$land" || exit $?
      print -r -- "$land"
    ''')
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == str(wts[1])
    assert git(wts[1], 'rev-parse', 'HEAD') == git(wts[0], 'rev-parse', 'HEAD')
