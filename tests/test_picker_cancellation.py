"""Aborted command pickers never launch or fall through to a fresh owner."""

from test_agent_seam import zsh  # noqa: F401 — isolated shell fixture


def test_explicit_remote_open_preserves_cancel_and_does_not_attach(zsh):
    result = zsh(r'''
      _dev_remote_resolve() { return 130; }
      _dev_open_picks() { print UNEXPECTED_ATTACH; }
      t session open api -r
    ''', _tty=True)
    assert result.returncode == 130, result.stdout
    assert 'UNEXPECTED_ATTACH' not in result.stdout


def test_automatic_remote_open_preserves_cancel_and_does_not_start(zsh):
    result = zsh(r'''
      mkdir -p "$HOME/code/api"
      REMOTE_HOSTS[mini]=mini
      _dev_remote_resolve() { return 130; }
      _dev_local_slot_live() { return 1; }
      _dev_new_session() { print UNEXPECTED_FRESH; }
      t session open api 3 --cli
    ''', _tty=True)
    assert result.returncode == 130, result.stdout
    assert 'UNEXPECTED_FRESH' not in result.stdout


def test_remote_pop_cancel_does_not_fall_through_to_missing_session(zsh):
    result = zsh(r'''
      _dev_remote_delegate() { return 130; }
      t session pop api 3
    ''', _tty=True)
    assert result.returncode == 130, result.stdout
    assert 'No such session' not in result.stdout


def test_foreground_open_cancel_does_not_adopt(zsh):
    result = zsh(r'''
      _dev_fg_handle() { return 0; }
      _dev_attach_fg() { return 130; }
      _dev_adopt_fg() { print UNEXPECTED_ADOPT; }
      t session open api:abcdef12
    ''', _tty=True)
    assert result.returncode == 130, result.stdout
    assert 'UNEXPECTED_ADOPT' not in result.stdout


def test_search_cancel_remains_distinct_from_picker_error(zsh):
    for picker_rc in (130, 2):
        result = zsh(f'''
          _claude_sessions_fzf() {{ return {picker_rc}; }}
          _dev_ensure_session_cwd() {{ print UNEXPECTED_RESUME; }}
          t session search
        ''', _tty=True)
        assert result.returncode == picker_rc, result.stdout
        assert 'UNEXPECTED_RESUME' not in result.stdout
