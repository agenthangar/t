"""Terminal titles expose public actions, never remote worker environments."""
import io
import os
import shlex

import pytest

from test_agent_seam import zsh  # noqa: F401


class Terminal(io.StringIO):
    def isatty(self):
        return True


def test_title_emitter_sanitizes_and_bounds(t_mod, monkeypatch):
    terminal = Terminal()
    monkeypatch.setattr(t_mod.sys, 'stdout', terminal)
    monkeypatch.setenv('TERM', 'xterm')
    t_mod._set_term_title('host\x1b\x07\n\x9c' + 'a' * 200)
    assert terminal.getvalue() == '\x1b]0;' + ('host' + 'a' * 200)[:160] + '\x07'
    monkeypatch.setenv('TERM', 'dumb')
    terminal.seek(0)
    terminal.truncate()
    t_mod._set_term_title('quiet')
    assert not terminal.getvalue()


def test_every_public_route_and_alias_has_payload_free_title(t_mod):
    for route, alias in t_mod._command_routes():
        title = t_mod._command_title([*route, 'secret;payload'])
        assert title is None or title == 't ' + ' '.join(route)
        alias_title = t_mod._command_title([*alias, 'secret;payload'])
        assert alias_title is None or 'secret;payload' not in alias_title
    assert t_mod._command_title([]) == 't'
    assert t_mod._command_title(['unknown;payload']) == 't'
    assert t_mod._command_title(['hosts', 'run', 'mini', 'private-command']) == 't hosts run'


@pytest.mark.parametrize('argv', [
    ['__resume-candidates'], ['_app-workspace'], ['session-rows'], ['mcp'], ['todo'],
    ['session', 'list', '--json'], ['session', 'read', '-d'],
    ['read', '--dump'], ['todo', '--statusline'], ['share', 'read', '1'],
    ['config', 'workflow', 'instructions', 'show'],
])
def test_machine_titles_are_quiet(t_mod, argv):
    assert t_mod._command_title(argv) is None


@pytest.mark.parametrize('result', [0, 17, 130, 'interrupt'])
def test_python_title_lifecycle_preserves_result(t_mod, monkeypatch, result):
    terminal = Terminal()
    monkeypatch.setattr(t_mod.sys, 'stdout', terminal)
    monkeypatch.setenv('TERM', 'xterm')
    monkeypatch.delenv('T_TITLE_ACTIVE', raising=False)
    def run(argv):
        assert terminal.getvalue() == '\x1b]0;t remote resume\x07'
        if result == 'interrupt':
            raise KeyboardInterrupt
        return result
    monkeypatch.setattr(t_mod, '_main', run)
    if result == 'interrupt':
        with pytest.raises(KeyboardInterrupt):
            t_mod.main(['remote', 'resume'])
    else:
        assert t_mod.main(['remote', 'resume']) == result
    assert terminal.getvalue().endswith('\x1b]0;\x07')


@pytest.mark.parametrize('env', [{'TERM': 'dumb'}, {'TERM': 'xterm', 'T_TITLE_ACTIVE': str(os.getpid())}])
def test_python_title_suppressed(t_mod, monkeypatch, env):
    terminal = Terminal()
    monkeypatch.setattr(t_mod.sys, 'stdout', terminal)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(t_mod, '_main', lambda argv: 7)
    assert t_mod.main(['help']) == 7
    assert not terminal.getvalue()


@pytest.mark.parametrize('action', ['open', 'resume'])
@pytest.mark.parametrize('rc', [0, 17, 130])
def test_public_remote_title_lifecycle_and_worker_quoting(zsh, action, rc):
    result = zsh('''
REMOTE_HOSTS=(mini user@mini)
_dev_resolve_repo() { return 1; }
ssh() { print -r -- "SSH ${(j:|:)@}"; return ''' + str(rc) + '''; }
t remote ''' + action + ''' 'repo;$(literal)' 2 --host mini
print rc=$?
''', _tty=True, TERM='xterm')
    text = result.stdout + result.stderr
    assert f'rc={rc}' in text
    assert text.index('\x1b]0;t remote ' + action + '\x07') < text.index('SSH ')
    assert text.index('\x1b]0;mini: t remote ' + action + '\x07') < text.index('SSH ')
    assert text.rindex('\x1b]0;\x07') > text.index('SSH ')
    wire = text.split('SSH -t|user@mini|', 1)[1].splitlines()[0]
    assert wire.startswith("zsh -lic '")
    worker = shlex.split(wire)[2]
    assert shlex.split(worker)[-2:] == ['repo;$(literal)', '2']


def test_shell_wrapper_preserves_cwd_and_status_and_sanitizes(zsh):
    result = zsh('''
_t_dispatch() { cd "$HOME"; print HANDLER; return 19; }
t session cd api; print "rc=$? cwd=$PWD"
_term_title $'safe\\e\\a\\n'
''', _tty=True, TERM='xterm')
    text = result.stdout + result.stderr
    assert '\x1b]0;t session cd\x07' in text
    assert f'rc=19 cwd={zsh.home}' in text
    assert '\x1b]0;safe\x07' in text


@pytest.mark.parametrize('args', ['session list --json', '__resume-candidates', '_app-workspace', 'read --dump', 'config workflow instructions show', 'workflow instructions show'])
def test_shell_machine_commands_never_title(zsh, args):
    result = zsh('_t_dispatch() { print HANDLER; }; t ' + args, _tty=True, TERM='xterm')
    assert '\x1b]0;' not in result.stdout + result.stderr


def test_shell_entry_covers_every_public_route_and_alias(zsh, t_mod):
    calls = []
    for route, alias in t_mod._command_routes():
        for words in (route, alias):
            calls.append('t ' + shlex.join([*words, 'private payload']))
    calls += ['t', 't --help', 't help', 't unknown-private']
    result = zsh('_t_dispatch() { print HANDLER; }; ' + '; '.join(calls), _tty=True, TERM='xterm')
    text = result.stdout + result.stderr
    assert text.count('HANDLER') == len(calls)
    titles = [piece.split('\x07', 1)[0] for piece in text.split('\x1b]0;')[1:]]
    assert titles and all('private' not in title for title in titles)
    assert 't remote open' in titles and 't session resume' in titles
    assert titles[-1] == ''


@pytest.mark.parametrize('status', [0, 17, 130, -2])
def test_standalone_exec_keeps_title_cleanup_parent(t_mod, monkeypatch, status):
    terminal = Terminal()
    monkeypatch.setattr(t_mod.sys, 'stdout', terminal)
    monkeypatch.setenv('TERM', 'xterm')
    monkeypatch.delenv('T_TITLE_ACTIVE', raising=False)
    calls = []
    def foreground(argv, executable):
        calls.append((executable, argv))
        assert terminal.getvalue() == '\x1b]0;t hosts run\x07'
        return status
    monkeypatch.setattr(t_mod.subprocess, 'call', foreground)
    monkeypatch.setattr(t_mod, '_main', lambda argv: t_mod._exec_foreground('ssh', ['ssh', '-t', 'lab']))
    with pytest.raises(SystemExit) as exited:
        t_mod.main(['hosts', 'run', 'lab'])
    assert exited.value.code == (status if status >= 0 else 128 - status)
    assert calls == [('ssh', ['ssh', '-t', 'lab'])]
    assert terminal.getvalue().endswith('\x1b]0;\x07')
    assert not t_mod._TITLE_OWNED


def test_machine_handler_cannot_emit_titles(t_mod, monkeypatch):
    terminal = Terminal()
    monkeypatch.setattr(t_mod.sys, 'stdout', terminal)
    monkeypatch.setenv('TERM', 'xterm')
    monkeypatch.setattr(t_mod, '_main', lambda argv: t_mod._set_term_title('payload'))
    t_mod.main(['hosts', 'run', 'lab', 'command', '--json'])
    assert not terminal.getvalue()


def test_shell_machine_handler_cannot_emit_titles(zsh):
    result = zsh('_t_dispatch() { _term_title payload; }; t session list --json', _tty=True, TERM='xterm')
    assert '\x1b]0;' not in result.stdout + result.stderr


def test_inventory_machine_modes_are_quiet(t_mod):
    import json
    from pathlib import Path
    inventory = json.loads((Path(__file__).parents[1] / 'ui/command-contract.json').read_text())
    for command in inventory['commands'].values():
        for mode in command.get('modes', {}).values():
            if 'machine' in mode.get('patterns', []):
                assert t_mod._command_title(mode['argv']) is None


def test_redirected_python_output_has_no_title(t_mod, monkeypatch):
    output = io.StringIO()
    monkeypatch.setattr(t_mod.sys, 'stdout', output)
    monkeypatch.setenv('TERM', 'xterm')
    monkeypatch.setattr(t_mod, '_main', lambda argv: t_mod._set_term_title('nested'))
    t_mod.main(['help'])
    assert output.getvalue() == ''


def test_redirected_shell_output_has_no_title(zsh):
    result = zsh('_t_dispatch() { _term_title nested; print DATA; }; t help', TERM='xterm')
    assert '\x1b]0;' not in result.stdout + result.stderr
    assert 'DATA' in result.stdout


@pytest.mark.parametrize('marker', ['T_TITLE_ACTIVE', 'T_TITLE_QUIET'])
def test_python_ignores_stale_inherited_title_markers(t_mod, monkeypatch, marker):
    terminal = Terminal()
    monkeypatch.setattr(t_mod.sys, 'stdout', terminal)
    monkeypatch.setenv('TERM', 'xterm')
    monkeypatch.setenv(marker, 'stale-owner')
    monkeypatch.setattr(t_mod, '_main', lambda argv: 0)
    assert t_mod.main(['help']) == 0
    assert terminal.getvalue() == '\x1b]0;t help\x07\x1b]0;\x07'


@pytest.mark.parametrize('machine', [False, True])
def test_tmux_panes_do_not_inherit_title_suppression(zsh, tmp_path, machine):
    import shutil
    import subprocess
    import tempfile
    import time
    from pathlib import Path
    tmux = shutil.which('tmux')
    if not tmux:
        pytest.skip('tmux unavailable')
    root = Path(__file__).resolve().parents[1]
    pane = tmp_path / 'pane.zsh'
    pane.write_text('source ' + shlex.quote(str(root / 't.plugin.zsh')) +
                    '\n_t_dispatch() { sleep 30; }\nt help\n')
    # A short private socket, never the developer's server.
    with tempfile.TemporaryDirectory(prefix='t-title-', dir='/tmp') as directory:
        socket = directory + '/socket'
        command = [tmux, '-S', socket, '-f', '/dev/null']
        try:
            launch = shlex.join([*command, 'new-session', '-d', '-s', 'title',
                                 shutil.which('zsh'), '-f', str(pane)])
            result = zsh('_t_dispatch() { ' + launch + '; }; t session open' +
                         (' --json' if machine else ''), _tty=True, TERM='xterm')
            assert result.returncode == 0, result.stdout + result.stderr
            deadline = time.monotonic() + 5
            title = ''
            while time.monotonic() < deadline:
                title = subprocess.run([*command, 'display-message', '-p', '-t', 'title',
                                        '#{pane_title}'], capture_output=True, text=True).stdout.strip()
                if title == 't help':
                    break
                time.sleep(0.05)
            assert title == 't help'
        finally:
            subprocess.run([*command, 'kill-server'], capture_output=True)
