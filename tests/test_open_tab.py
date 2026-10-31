"""Exercise tab automation without opening windows or sending native keystrokes."""
import pytest

from test_agent_seam import zsh  # noqa: F401


@pytest.mark.parametrize('terminal', ['Apple_Terminal', 'iTerm.app'])
def test_open_tab_targets_origin_and_new_tab(zsh, tmp_path, terminal):
    script = tmp_path / 'automation'
    result = zsh('''
      osascript() { cat > "$SCRIPT"; }
      _dev_open_tab 't open "api" 2'; print rc=$?
    ''', _tty=True, TERM_PROGRAM=terminal, SCRIPT=str(script))
    assert 'rc=0' in result.stdout
    source = script.read_text()
    assert 'activate' not in source
    assert 'Origin terminal is unavailable' in source
    assert 'tty of candidate' in source
    assert 'do script "t open \\"api\\" 2" in front window' not in source
    if terminal == 'Apple_Terminal':
        assert 'do script "t open \\"api\\" 2" in (first tab of window id originID whose tty is newTTY)' in source
        assert 'tabs of window id originID' in source
        assert 'if newTTY is missing value then error' in source
        assert 'if (count newTabs) > 1 then error' in source
        assert source.index('if not frontmost then error') < source.index('keystroke')
        assert 'tty of candidateTab is not in oldTTYs' in source
    else:
        assert 'tell originWindow to set newTab to create tab' in source
        assert 'tell current session of newTab to write text' in source
        assert 'current window' not in source


@pytest.mark.parametrize('terminal', ['Apple_Terminal', 'iTerm.app'])
def test_open_tab_automation_failure_preserves_fallback(zsh, terminal):
    result = zsh('''
      osascript() { cat >/dev/null; return 1; }
      _dev_open_tab 't open api 2' || print 'Attach with: t open api 2'
    ''', _tty=True, TERM_PROGRAM=terminal)
    assert 'Attach with: t open api 2' in result.stdout


@pytest.mark.parametrize('environment', [
    {'SSH_CONNECTION': 'remote'}, {'TERM_PROGRAM': 'unknown'},
    {'TMUX': 'test', 'CLIENT_TTY': ''},
])
def test_open_tab_unavailable_never_automates(zsh, environment):
    result = zsh('''
      tmux() { print -rn -- "$CLIENT_TTY"; }
      osascript() { print -u2 WRONG_AUTOMATION; }
      _dev_open_tab 't open api 2'; print rc=$?
    ''', _tty=True, **({'TERM_PROGRAM': 'Apple_Terminal'} | environment))
    assert 'rc=1' in result.stdout
    assert 'WRONG_AUTOMATION' not in result.stderr


def test_open_tab_tmux_uses_client_terminal(zsh, tmp_path):
    script = tmp_path / 'automation'
    result = zsh('''
      tmux() {
        if [[ $1 == show-environment ]]; then print TERM_PROGRAM=Apple_Terminal
        else print /dev/ttys042; fi
      }
      osascript() { cat > "$SCRIPT"; }
      _dev_open_tab 't open api 2'; print rc=$?
    ''', TMUX='test', TERM_PROGRAM='tmux', SCRIPT=str(script))
    assert 'rc=0' in result.stdout
    assert 'if tty of candidateTab is "/dev/ttys042"' in script.read_text()
