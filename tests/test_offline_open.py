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


@pytest.mark.parametrize('workspace', ['fresh', 'reserved', 'saved'])
@pytest.mark.parametrize('owner', ['', 'app', 'detached'])
def test_desktop_claim_offline_allows_fresh_or_reserved_local_slot(zsh, workspace, owner):
    worktree = zsh.home / 'worktrees/api/3'
    if workspace != 'fresh':
        (worktree / '.git').mkdir(parents=True)
    result = zsh(r'''
      REMOTE_HOSTS[offline]=offline
      _dev_rows_all() {
        print scan >> "$HOME/scans"
        if [[ -n $OWNER ]]; then
          printf 'other\tsaved-id\t%s\tother-alias-3\t%s\tactive\tWork\n' "$HOME/worktrees/api/api-3" "$OWNER"
        fi
        print -u2 'dev: offline unreachable — skipped'
        return 1
      }
      _dev_session_rows() { :; }
      DEV_WORKTREE_ROOT="$HOME/worktrees"
      _dev_slot_fresh() { [[ $WORKSPACE == fresh ]]; }
      _dev_app_slot_reserved() { [[ $WORKSPACE == reserved ]]; }
      _dev_cwd_pids() { :; }
      _dev_ps_snapshot() { :; }
      _dev_worktree_create() { print created >> "$HOME/created"; mkdir -p "$3/.git"; print -r -- "$3"; }
      _dev_auto_trust() { :; }
      _dev_app_slot_reserve() { print RESERVED; }
      reopen=
      _t_open_app_claim api 3 "$HOME/worktrees/api/3"
      rc=$?
      print -r -- "reopen=$reopen"
      exit $rc
    ''', WORKSPACE=workspace, OWNER=owner)
    allowed = workspace != 'saved' and not owner
    assert (result.returncode == 0) == allowed, result.stdout + result.stderr
    assert ('RESERVED' in result.stdout) == allowed
    assert (zsh.home / 'scans').read_text().splitlines() == ['scan']
    assert result.stderr.count('offline unreachable') == 1
    assert (zsh.home / 'created').exists() == (allowed and workspace == 'fresh')
    if allowed:
        assert 'ownership could not be verified' not in result.stderr
        assert ('reopen=1' in result.stdout) == (workspace == 'reserved')
    elif not owner:
        assert result.returncode == 3
        assert result.stderr.count('ownership could not be verified') == 1


@pytest.mark.parametrize('failure', ['local_scan', 'cwd_scan', 'process_scan', 'foreground'])
def test_desktop_offline_reopen_retains_local_owner_checks(zsh, failure):
    result = zsh(r'''
      REMOTE_HOSTS[offline]=offline
      _dev_rows_all() { return 1; }
      _dev_session_rows() { [[ $FAILURE != local_scan ]]; }
      _dev_app_slot_reserved() { return 0; }
      _dev_cwd_pids() {
        [[ $FAILURE == cwd_scan ]] && return 1
        [[ $FAILURE == foreground ]] && print 123
        return 0
      }
      _dev_ps_snapshot() {
        _DEV_PS_COMM[123]=codex
        [[ $FAILURE != process_scan ]]
      }
      _dev_agent_is_service() { return 1; }
      _dev_worktree_create() { print MUTATED; }
      _dev_app_slot_reserve() { print MUTATED; }
      _t_open_app_claim api 3 "$HOME/worktrees/api/3"
    ''', FAILURE=failure)
    assert result.returncode != 0, result.stdout + result.stderr
    assert 'MUTATED' not in result.stdout
    assert 'retry before opening' in result.stderr or 'terminal agent still owns' in result.stderr


def test_desktop_remote_check_ignores_unrelated_owners_with_complete_scan(zsh):
    result = zsh(r'''
      _dev_rows_all() {
        printf 'other\tsaved-id\t%s\tweb-3\tapp\tactive\tWork\n' "$HOME/code/web"
        printf 'other\tsaved-id\t%s\tapi-4\tdetached\tactive\tWork\n' "$HOME/code/api"
        printf 'local\tsaved-id\t%s\tapi-3\tapp\tnone\tWork\n' "$HOME/code/api"
      }
      _dev_session_rows() { print -u2 UNEXPECTED_RETRY; return 1; }
      _dev_remote_app_check api 3 1
    ''')
    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stderr == ''


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
