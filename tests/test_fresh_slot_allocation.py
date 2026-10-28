"""Fresh local allocation skips remote desktop reservations from one snapshot."""
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
_dev_remote_app_owner api 2 1; print rc=$?
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
