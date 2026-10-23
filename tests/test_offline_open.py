"""Offline discovery must distinguish fresh work from saved ownership."""
import pytest
from test_agent_seam import zsh  # noqa: F401
from test_resume_app import saved_rollout, SID


@pytest.mark.parametrize('mode', ['issue', 'standalone', 'local_failure', 'known_owner', 'moved_owner', 'local_guard'])
def test_issue_resume_skips_offline_host(zsh, mode):
    cwd, _ = saved_rollout(zsh)
    result = zsh(r'''
      REMOTE_HOSTS[offline]=offline
      _dev_rows_all() {
        if [[ $MODE == known_owner || $MODE == moved_owner ]]; then
          local owner_slot=3
          [[ $MODE == moved_owner ]] && owner_slot=9
          printf 'online\t%s\t%s\tapi-%s\tdetached\tactive\tWork\tcodex\n' "$SID" "$CWD" "$owner_slot"
        fi
        print -u2 'dev: offline unreachable — skipped'
        return 1
      }
      _dev_session_rows() { [[ $MODE != local_failure ]]; }
      _dev_ensure_session_cwd() { print -r -- "$1"; }
      _dev_resume_session() {
        if [[ $MODE == local_guard ]]; then print -u2 LOCAL_GUARD; return 1; fi
        print -r -- "RESUMED $*"
      }
      _dev_remote_attach() { print -r -- "REMOTE $*"; }
      if [[ $MODE == standalone ]]; then
        _t_resume api 3 --days all
      else
        _T_ISSUE_SKIP_UNAVAILABLE=1 _t_resume api 3 --days all
      fi
    ''', MODE=mode, SID=SID, CWD=str(cwd))
    assert ('RESUMED' in result.stdout) == (mode == 'issue'), result.stdout + result.stderr
    assert (result.returncode == 0) == (mode in ('issue', 'known_owner')), result.stdout + result.stderr
    if mode == 'known_owner': assert 'REMOTE online' in result.stdout
    if mode == 'moved_owner': assert 'on online' in result.stderr
    if mode == 'local_guard': assert 'LOCAL_GUARD' in result.stderr
    if mode == 'local_failure': assert 'local ownership check failed' in result.stderr
    assert 'offline unreachable' in result.stderr


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
