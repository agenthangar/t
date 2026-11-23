"""Offline discovery must distinguish fresh work from saved ownership."""
import pytest
from test_agent_seam import zsh  # noqa: F401
from codex_history_helpers import saved_rollout, SID


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
      _t_resume_history_launch() {
        _dev_resume_session dev-api-3 "$CWD" "$2" "$3" >&2 || return $?
        print dev-api-3
      }
      _dev_remote_attach() { print -r -- "REMOTE $*"; }
      if [[ $MODE == standalone ]]; then
        _t_resume api 3 --days all
      else
        _T_ISSUE_SKIP_UNAVAILABLE=1 _t_resume api 3 --days all
      fi
    ''', MODE=mode, SID=SID, CWD=str(cwd))
    assert ('RESUMED' in result.stdout + result.stderr) == (mode == 'issue'), result.stdout + result.stderr
    assert (result.returncode == 0) == (mode == 'issue'), result.stdout + result.stderr
    if mode == 'known_owner':
        assert 'on online' in result.stderr
        assert 'REMOTE online' not in result.stdout  # no implicit nonterminal attachment
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


def test_unverified_numbered_open_points_to_saved_resume(zsh):
    (zsh.home / 'code/api').mkdir(parents=True)
    result = zsh(r'''
      REMOTE_HOSTS[offline]=offline
      _dev_local_slot_live() { return 1; }
      _dev_remote_resolve() { return 3; }
      _dev_new_session() { print UNEXPECTED_LAUNCH; }
      _t_open api 3 --cli
    ''')
    assert result.returncode != 0, result.stdout + result.stderr
    assert 't session resume api 3' in result.stderr
    assert 'UNEXPECTED_LAUNCH' not in result.stdout


@pytest.mark.parametrize('outcome, expected', [('selected', 0), ('canceled', 130), ('empty', 1), ('remote', 0)])
def test_interactive_numbered_open_on_incomplete_scan_uses_resume_picker(zsh, outcome, expected):
    (zsh.home / 'code/api').mkdir(parents=True)
    result = zsh(r'''
      REMOTE_HOSTS[offline]=offline
      _dev_worktree_enabled() { return 0; }
      _dev_slot_fresh() { return 1; }
      _dev_local_slot_live() { return 1; }
      _dev_rows_all() {
        if [[ $OUTCOME == remote ]]; then
          printf 'online\tremote-id\t%s\tapi-3\tdetached\tactive\tWork\tcodex\n' "$HOME/code/api"
        fi
        print -u2 'dev: offline unreachable'
        return 1
      }
      fzf() { print UNEXPECTED_OPEN_PICKER; }
      _t_resume() {
        print -r -- "RESUME ${_T_OPEN_SAVED_ONLY:-0} $*"
        case $OUTCOME in
          selected) return 0 ;;
          canceled) return 130 ;;
          empty) return 1 ;;
        esac
      }
      _dev_open_picks() { print -r -- "ATTACH $1"; }
      _dev_new_session() { print UNEXPECTED_LAUNCH; }
      _t_open_recover() { print UNEXPECTED_REPAIR; }
      t open api 3
    ''', _tty=True, OUTCOME=outcome)
    output = result.stdout + result.stderr
    assert result.returncode == expected, output
    assert 'UNEXPECTED_' not in output
    assert 'picker_alias=' not in output
    if outcome == 'remote':
        assert 'ATTACH online\tapi\t3' in output
        assert 'RESUME ' not in output
    else:
        assert 'RESUME 1 api 3 --cli' in output
        assert 'ATTACH ' not in output


def test_open_fallback_picker_offers_only_stopped_history(zsh):
    cwd, _ = saved_rollout(zsh)
    (zsh.home / 'code/api').mkdir(parents=True)
    result = zsh(r'''
      REMOTE_HOSTS[offline]=offline
      REMOTE_HOSTS[online]=online
      _dev_slot_fresh() { return 1; }
      _dev_local_slot_live() { return 1; }
      _dev_rows_all() {
        printf 'online\tlive-sid\t%s\tapi-9\tdetached\tactive\tOwner elsewhere\tcodex\n' "$CWD"
        [[ $1 == --best-effort ]]
      }
      _t_resume_history_candidates() {
        printf '%s\tapi\t3\t%s\t%s\tNov 22\t%s\tlocal\tcodex\n' \
          "$EPOCHSECONDS" "$SID" "$CWD" 'Stopped fixture' \
          "$EPOCHSECONDS" live-sid "$CWD" 'Live fixture'
      }
      fzf() { print UNEXPECTED_OPEN_PICKER; }
      _t_fzf_filtered() {
        [[ $2 == *'Stopped fixture'* && $2 != *'Owner elsewhere'* ]] || return 98
        print -u2 PICKER_ONLY_STOPPED
        return 130
      }
      _t_resume_history_launch() { print UNEXPECTED_LAUNCH; }
      _t_open_recover() { print UNEXPECTED_REPAIR; }
      T_DISCOVERY_COLLECTOR=1 t open api 3
    ''', _tty=True, SID=SID, CWD=str(cwd))
    assert result.returncode == 130, result.stdout
    assert 'PICKER_ONLY_STOPPED' in result.stdout
    assert 'UNEXPECTED_' not in result.stdout
