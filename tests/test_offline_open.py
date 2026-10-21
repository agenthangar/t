"""Offline discovery must distinguish fresh work from saved ownership."""
import pytest
from test_agent_seam import zsh  # noqa: F401


@pytest.mark.parametrize('args', ['--new', '3', '--new --local', '--new --fg'])
def test_fresh_cli_open_with_offline_host(zsh, args):
    (zsh.home / 'code/api').mkdir(parents=True)
    result = zsh(r'''
      REMOTE_HOSTS[offline]=offline
      _dev_rows_all() { print -u2 'dev: offline unreachable — skipped'; return 1; }
      _dev_slot_fresh() { return 0; }
      _dev_worktree_enabled() { return 1; }
      _dev_agent_check() { return 0; }
      _dev_repo_prepare() { :; }
      _dev_auto_trust() { :; }
      _dev_with_launch_locks() { return 0; }
      _dev_new_session() { print LAUNCHED; }
      codex() { print -r -- "LAUNCHED $*"; }
      _t_open api --cli ''' + args)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'LAUNCHED' in result.stdout
    assert 'ownership could not be verified' not in result.stderr
    if '--fg' in args:
        assert '--no-daemon' in result.stdout


@pytest.mark.parametrize('fresh', [True, False])
@pytest.mark.parametrize('owner', ['', 'app', 'detached'])
def test_desktop_claim_offline_allows_only_fresh_unowned_slot(zsh, fresh, owner):
    result = zsh(r'''
      REMOTE_HOSTS[offline]=offline
      _dev_rows_all() {
        if [[ -n $OWNER ]]; then
          printf 'other\tsaved-id\t%s\tapi-3\t%s\tactive\tWork\n' "$HOME/code/api" "$OWNER"
        fi
        return 1
      }
      _dev_slot_fresh() { [[ $FRESH == 1 ]]; }
      _dev_cwd_pids() { :; }
      _dev_ps_snapshot() { :; }
      _dev_worktree_create() { mkdir -p "$3/.git"; print -r -- "$3"; }
      _dev_auto_trust() { :; }
      _dev_app_slot_reserve() { print RESERVED; }
      _t_open_app_claim api 3 "$HOME/worktrees/api/3"
    ''', FRESH='1' if fresh else '0', OWNER=owner)
    allowed = fresh and not owner
    assert (result.returncode == 0) == allowed, result.stdout + result.stderr
    assert ('RESERVED' in result.stdout) == allowed
    if allowed:
        assert 'ownership could not be verified' not in result.stderr


def test_verified_local_cli_attach_does_not_scan_offline_host(zsh):
    (zsh.home / 'code/api').mkdir(parents=True)
    result = zsh(r'''
      REMOTE_HOSTS[offline]=offline
      _dev_rows_all() { print -u2 UNEXPECTED_SCAN; return 1; }
      _t_open api 3 --cli
    ''', FAKE_HAS_SESSION='1', FAKE_SESSIONS='dev-api-3')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Reattaching dev-api-3' in result.stdout
    assert 'UNEXPECTED_SCAN' not in result.stderr
