"""Fresh local allocation skips remote desktop reservations from one snapshot."""
from pathlib import Path
import shlex
import subprocess

import pytest

from test_agent_seam import zsh  # noqa: F401


SETUP = r'''
REMOTE_HOSTS=(mini mini offline offline)
DEV_WORKTREE_ROOT=/Users/local/worktrees
_dev_rows_all() {
  print scan >> "$HOME/scans"
  printf 'mini\tid1\t%s\tdifferent-alias-1\tapp\tactive\tDesktop\n' "$HOME/code/api"
  printf 'mini\tid2\t/home/remote/worktrees/api/2\tother-2\tapp\tactive\tDesktop\n'
  printf 'mini\tid3\t/home/remote/code/unrelated\tapi-3\tapp\tactive\tOther repo\n'
  return ${SCAN_RC:-0}
}
_dev_slot_fresh() { (( $2 >= ${FIRST_FRESH:-1} )); }
_dev_agent_check() { return 0; }
_dev_new_session() { print -r -- "LAUNCH $1"; }
_dev_wait_ready() { :; }
_dev_recovery_watch() { :; }
_dev_worktree_enabled() { return 1; }
'''


@pytest.mark.parametrize('first,expected', [(1, 3), (21, 21)])
@pytest.mark.parametrize('scan_rc', [0, 1])
@pytest.mark.parametrize('slot', ['new', ''])
def test_automatic_allocation_skips_remote_reservations(zsh, first, expected, scan_rc, slot):
    (zsh.home / 'code/api').mkdir(parents=True)
    result = zsh(SETUP + f'''
_dev_remote_resolve() {{ return 1; }}
_t_dev api {slot} {'--local' if not slot else ''}; print rc=$?
''', FIRST_FRESH=str(first), SCAN_RC=str(scan_rc))
    assert f'LAUNCH dev-api-{expected}' in result.stdout, result.stdout + result.stderr
    assert 'rc=0' in result.stdout
    assert (zsh.home / 'scans').read_text().splitlines() == ['scan']


@pytest.mark.parametrize('slot', ['new', ''])
@pytest.mark.parametrize('scan_rc', [0, 1])
def test_fresh_foreground_skips_remote_reservations(zsh, slot, scan_rc):
    (zsh.home / 'code/api').mkdir(parents=True)
    result = zsh(SETUP + r'''
_dev_worktree_enabled() { return 0; }
_dev_worktree_create() { print -u2 "WORKTREE $2"; print -r -- "$HOME/code/api"; }
_dev_with_launch_locks() { return 0; }
_dev_agent_executable() { print /usr/bin/true; }
_dev_auto_trust() { :; }
''' + f'_t_dev api {slot} --fg; print rc=$?', SCAN_RC=str(scan_rc))
    assert 'WORKTREE 3' in result.stderr, result.stdout + result.stderr
    assert 'rc=0' in result.stdout
    assert (zsh.home / 'scans').read_text().splitlines() == ['scan']


def test_explicit_reserved_slot_still_refuses(zsh):
    (zsh.home / 'code/api').mkdir(parents=True)
    result = zsh(SETUP + '_t_dev api 1 --local; print rc=$?')
    assert 'rc=1' in result.stdout
    assert 'reserved by the Codex desktop app on mini' in result.stderr
    assert 'LAUNCH' not in result.stdout


def test_remote_allocation_uses_home_relative_path_identity(zsh):
    result = zsh(SETUP + r'''
DEV_REPOS[api]=/Users/local/code/api
_dev_remote_legacy_owner api 2 1; print rc=$?
''')
    assert 'mini' in result.stdout and 'rc=0' in result.stdout


def test_new_skips_t_slot_9_reserved_by_codex_desktop_on_mac(zsh):
    """The reported `t open t --new` case must allocate 10, without --local."""
    (zsh.home / 'code/t').mkdir(parents=True)
    result = zsh(SETUP + r'''
DEV_REPOS[t]="$HOME/code/t"
REMOTE_HOSTS=(mac mac)
_dev_slot_fresh() { (( $2 >= 9 )); }
_dev_rows_all() {
  print scan >> "$HOME/scans"
  printf 'mac\tdesktop-id\t%s\tt-9\tapp\tactive\tDesktop\tcodex\n' "$HOME/code/t"
}
t open t --new; print rc=$?
''')
    refusal = ('t open: t 9 is reserved by the Codex desktop app on mac; '
               'close it there and release the reservation before opening here')
    assert refusal not in result.stderr, result.stdout + result.stderr
    assert 'LAUNCH dev-t-10' in result.stdout, result.stdout + result.stderr
    assert 'rc=0' in result.stdout
    assert (zsh.home / 'scans').read_text().splitlines() == ['scan']


@pytest.mark.parametrize('legacy', [False, True])
def test_reservation_scan_never_collects_local_display_and_supports_old_peers(zsh, legacy):
    result = zsh(r'''
REMOTE_HOSTS=(peer peer)
_dev_session_rows() { print -u2 UNEXPECTED_LOCAL_INVENTORY; return 91; }
_dev_remote_scan_with_timeout() {
  local -a words; words=(${(z)2})
  local script=${(Q)words[3]}
  (
    if [[ $LEGACY == 1 ]]; then
      unfunction _dev_legacy_workspace_reserved
      _dev_session_rows() { printf 'id\t/remote/api\tapi-8\tapp\tactive\tLegacy\tcodex\n'; }
    fi
    eval "$script"
  )
}
_dev_rows_all --reservations api
''', LEGACY=str(int(legacy)))
    assert result.returncode == 0, result.stderr
    assert 'UNEXPECTED_LOCAL_INVENTORY' not in result.stderr
    assert ('peer\tid\t/remote/api\tapi-8\tapp' in result.stdout) == legacy


def test_reservations_read_only_matching_worktree_markers(zsh, tmp_path):
    repo = zsh.home / 'code/api'
    repo.mkdir(parents=True)
    root = tmp_path / 'worktrees'
    for command in (['init', '-q'], ['-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                                     'commit', '-q', '--allow-empty', '-m', 'base']):
        subprocess.run(['git', '-C', str(repo), *command], check=True, capture_output=True)
    for leaf in ('1', 'api-2', 'api-3'):
        wt = root / 'api' / leaf
        subprocess.run(['git', '-C', str(repo), 'worktree', 'add', '-q', '-b', leaf, str(wt)],
                       check=True, capture_output=True)
        if leaf != 'api-3':
            gitdir = subprocess.check_output(['git', '-C', str(wt), 'rev-parse', '--absolute-git-dir'], text=True).strip()
            (Path(gitdir) / 't-app-slot').write_text('codex-app\n')
    result = zsh(f'''
DEV_WORKTREE_ROOT={shlex.quote(str(root))}
DEV_REPOS[alias]="$HOME/code/api"
_dev_session_rows() {{ print -u2 UNEXPECTED_INVENTORY; return 91; }}
_dev_slot_reservation_rows api
_dev_slot_reservation_rows unrelated
''')
    assert result.returncode == 0, result.stderr
    rows = [row.split('\t') for row in result.stdout.splitlines()]
    assert sorted(row[1] for row in rows) == sorted(str(root / 'api' / leaf) for leaf in ('1', 'api-2'))
    assert all(row[3] == 'app' for row in rows)
    assert 'UNEXPECTED_INVENTORY' not in result.stderr


def test_fresh_allocation_requests_reservations_and_honors_cancellation(zsh):
    (zsh.home / 'code/api').mkdir(parents=True)
    result = zsh(SETUP + r'''
_dev_rows_all() {
  [[ $* == '--reservations api' ]] || return 91
  return 130
}
_t_dev api new
''')
    assert result.returncode == 130, result.stdout + result.stderr
    assert 'LAUNCH' not in result.stdout


def test_foreground_preflight_keeps_conflict_without_display_work(zsh, tmp_path):
    (tmp_path / 'ps.txt').write_text('1 0 launchd\n70001 1 claude\n')
    registry = zsh.home / '.cache/claude-sessions'
    registry.mkdir()
    (registry / '70001').write_text(f'known-session\t{zsh.home}/code/api\n')
    result = zsh(r'''
_dev_legacy_workspace_reserved() { return 1; }
_dev_repo_of_dir() { print -u2 UNEXPECTED_LABEL; return 91; }
_dev_agent_transcript() { print -u2 UNEXPECTED_TRANSCRIPT; return 91; }
_dev_fg_rows --ownership
_dev_launch_preflight dev-api-1 "$HOME/code/api"
''')
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'known-session\t' in result.stdout
    assert 'foreground agent already owns' in result.stderr
    assert 'UNEXPECTED' not in result.stderr
