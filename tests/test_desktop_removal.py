"""Removed Desktop controls never launch; old settings cannot veto CLI moves."""
import pytest
from test_agent_seam import zsh  # noqa: F401


@pytest.mark.parametrize('argv', [
    ['app', 'api', '2'], ['session', 'open-app', 'api', '2'],
    ['session', 'open', 'api', '2', '--app'],
    ['session', 'resume', 'api', '2', '--app'],
])
def test_removed_app_routes_fail_before_actions(t_mod, argv):
    with pytest.raises(SystemExit) as error:
        t_mod.build_parser().parse_args(argv)
    assert error.value.code == 2


@pytest.mark.parametrize('verb', ['open', 'resume'])
def test_shell_app_flag_fails_before_discovery(zsh, verb):
    result = zsh('_dev_rows_all() { print UNEXPECTED_SCAN; }; '
                 f'_t_{verb} api 2 --app')
    assert result.returncode == 2
    assert 'support has been removed' in result.stderr
    assert 'UNEXPECTED' not in result.stdout
    assert not zsh.log.exists() or not zsh.log.read_text()


@pytest.mark.parametrize('setting', ['', 'DEV_HANDOFF[codex]=native; ', '_T_HANDOFF_CODEX=native; '])
def test_codex_remote_resume_reaches_guarded_transfer(zsh, setting):
    result = zsh(setting + '''
      _dev_repo_of_dir() { printf "api\t2\n"; }
      _t_lifecycle_run() { print -u2 GUARDED_TRANSFER; return 42; }
      _tbeam_sync_transcript() { print -u2 UNEXPECTED_COPY; }
      _t_resume_on_host mini selected /workspace codex detach
    ''')
    assert result.returncode == 42
    assert 'GUARDED_TRANSFER' in result.stderr
    assert 'UNEXPECTED_COPY' not in result.stderr
    assert 'native desktop' not in result.stderr


def test_legacy_config_is_ignored(t_mod, tmp_path, monkeypatch):
    config = tmp_path / 'local.zsh'
    config.write_text('DEV_HANDOFF[codex]=native\nDEV_OPEN_MODE_DEFAULT=app\nDEV_OPEN_MODE[api]=app\n')
    monkeypatch.setattr(t_mod, 'CONFIG', str(config))
    cfg = t_mod.Config()
    assert not hasattr(cfg, 'handoff')
    assert not hasattr(cfg, 'open_mode')


def test_legacy_workspace_cannot_start_fresh(zsh):
    result = zsh('''
      _dev_legacy_workspace_reserved() { return 0; }
      _dev_launch_preflight dev-api-2 /workspace ''
    ''')
    assert result.returncode == 1
    assert 't session resume' in result.stderr
    assert not zsh.log.exists() or not zsh.log.read_text()


def test_move_without_live_owner_points_to_saved_resume(zsh):
    result = zsh('''
      _t_remote_move_rows() { :; }
      t session move api 2
    ''')
    assert result.returncode == 0
    assert 'no current CLI conversations' in result.stderr
    assert 't session resume api 2' in result.stderr
