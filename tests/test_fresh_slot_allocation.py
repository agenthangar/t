"""Fresh local opening ignores retired remote desktop reservations."""
import pytest
from test_agent_seam import zsh  # noqa: F401


@pytest.mark.parametrize('flags,mode', [('', 'tmux'), ('--fg', 'foreground')])
def test_new_uses_local_allocator_without_remote_probe(zsh, flags, mode):
    (zsh.home / 'code/api').mkdir(parents=True)
    result = zsh(r'''
REMOTE_HOSTS=(offline offline)
_dev_rows_all() { print -u2 UNEXPECTED_SCAN; return 91; }
_dev_remote_resolve() { print -u2 UNEXPECTED_REMOTE; return 91; }
_dev_worktree_enabled() { return 0; }
_dev_fresh_open() { print -r -- "LOCAL_FRESH $1 $2 ${3:-tmux}"; _T_FRESH_SESSION=dev-api-43; }
_t_session_attach() { print -r -- "ATTACH $1"; }
''' + f'_t_dev api new --claude {flags}; print rc=$?')
    assert result.returncode == 0, result.stderr
    assert f'LOCAL_FRESH api claude {mode}' in result.stdout
    assert 'rc=0' in result.stdout and 'UNEXPECTED' not in result.stderr
    assert ('ATTACH dev-api-43' in result.stdout) == (mode == 'tmux')


@pytest.mark.parametrize('scan_rc,owner', [(3, ''), (0, 'mini\tapi-3')])
def test_numbered_foreground_still_checks_actual_remote_cli(zsh, scan_rc, owner):
    (zsh.home / 'code/api').mkdir(parents=True)
    result = zsh(r'''
REMOTE_HOSTS=(mini mini)
_dev_slot_fresh() { return 1; }
_dev_remote_resolve() { print -r -- "$OWNER"; return $SCAN_RC; }
_dev_new_session() { print UNEXPECTED_LAUNCH; }
_t_dev api 3 --fg --local; print rc=$?
''', OWNER=owner, SCAN_RC=str(scan_rc))
    assert 'rc=0' not in result.stdout and 'UNEXPECTED_LAUNCH' not in result.stdout


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
