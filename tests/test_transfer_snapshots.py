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


def test_receive_preflight_failure_does_not_stop_owner(zsh):
    result = zsh(RECEIVE + r'''
      DEV_WORKTREE_ROOT="$HOME"
      _dev_worktree_accept_cwd() { return 0; }
      _dev_worktree_beam_prepare() { print -u2 'push access denied'; return 1; }
      t session move api 2 --host local --detach
    ''')
    assert result.returncode == 1
    assert 'source was not stopped' in output(result)
    assert not (zsh.home/'stop').exists()
    assert 'START ' not in output(result)


def test_send_preflight_failure_does_not_stop_owner(zsh):
    from test_move_picker import SETUP
    result = zsh(SETUP + r'''
      _dev_self_sid() { :; }
      _codex_thread_lookup() { printf 'codex-id\trollout\t%s\n' "$HOME/code/api"; }
      _dev_codex_rollout_integrity() { return 0; }
      _dev_worktree_beam_prepare() { print -u2 'preflight denied'; return 1; }
      _tbeam_kill_owner() { print -u2 UNSAFE_STOP; }
      t session move --session codex-id --host mini --detach
    ''')
    assert result.returncode == 1
    assert 'preflight denied' in output(result)
    assert 'UNSAFE_STOP' not in output(result)


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


def test_receive_carries_prepared_ref_through_publish_and_landing(zsh):
    ref = 'refs/heads/t-transfer/' + 'a' * 32
    result = zsh(RECEIVE + r'''
      DEV_WORKTREE_ROOT="$HOME"
      _dev_worktree_accept_cwd() { return 0; }
      _dev_worktree_beam_prepare() { print -r -- "$EXPECTED_REF"; }
      functions[_snapshot_ssh]=$functions[ssh]
      ssh() { print -r -- "$*" >> "$HOME/ssh.log"; _snapshot_ssh "$@"; }
      _dev_beam_land_cwd() {
        [[ $TB_TRANSFER_REF == $EXPECTED_REF ]] || return 71
        print -r -- "$1"
      }
      _dev_worktree_beam_sync() { [[ $TB_TRANSFER_REF == $EXPECTED_REF ]]; }
      _dev_worktree_beam_finish() { [[ $TB_TRANSFER_REF == $EXPECTED_REF ]]; }
      t session move api 2 --host local --detach
    ''', EXPECTED_REF=ref)
    assert result.returncode == 0, output(result)
    commands = (zsh.home/'ssh.log').read_text().splitlines()
    assert next(i for i,c in enumerate(commands) if '_dev_worktree_beam_prepare' in c) < next(i for i,c in enumerate(commands) if '_tbeam_kill_owner' in c)
    assert any('TB_TRANSFER_REF='+ref in c and '_dev_worktree_beam_push' in c for c in commands)
    assert 'START remote-id' in output(result)


@pytest.mark.parametrize('supported', [True, False])
def test_send_requires_snapshot_peer_and_transmits_exact_ref(zsh, supported):
    from test_move_picker import SETUP
    ref = 'refs/heads/t-transfer/' + 'b' * 32
    result = zsh(SETUP + r'''
      _dev_self_sid() { :; }
      _codex_thread_lookup() { printf 'codex-id\trollout\t%s\n' "$HOME/code/api"; }
      _dev_codex_rollout_integrity() { return 0; }
      _dev_worktree_beam_prepare() { print -r -- "$EXPECTED_REF"; }
      _dev_worktree_beam_push() { [[ $TB_TRANSFER_REF == $EXPECTED_REF ]]; }
      ssh() {
        print -r -- "$*" >> "$HOME/ssh.log"
        if [[ $* == *_dev_worktree_transfer_ref_valid* ]]; then [[ $SUPPORTED == yes ]]; return; fi
        [[ $* != *_tbeam_land* ]] || print dev-api-2
        return 0
      }
      t session move --session codex-id --host mini --detach
    ''', EXPECTED_REF=ref, SUPPORTED='yes' if supported else 'no')
    assert result.returncode == (0 if supported else 1), output(result)
    commands = (zsh.home/'ssh.log').read_text()
    if supported:
        assert 'TB_TRANSFER_REF='+ref in commands
        assert 'STOP codex-id' in output(result)
    else:
        assert 'STOP ' not in output(result) and '_tbeam_land' not in commands
