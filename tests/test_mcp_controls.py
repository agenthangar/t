"""MCP mutations are explicit, literal, noninteractive, and bound to a live owner."""
import io
import json
import os
from pathlib import Path
import subprocess
import shlex
import shutil
import tempfile
import sys
import time
from types import SimpleNamespace

import pytest

from conftest import load_script
from test_agent_seam import zsh  # noqa: F401

SID = '12345678-1234-1234-1234-123456789abc'


@pytest.fixture
def controls():
    return load_script(Path(__file__).parents[1] / 'libexec/t_mcp_controls.py', 'test_controls')


@pytest.fixture
def cfg(tmp_path):
    return SimpleNamespace(repos={'api': str(tmp_path)}, worktree_root=str(tmp_path / 'wt'), hosts={}, repo_of_dir=lambda p: (None, None))


def result(stdout='', code=0, stderr=''):
    return subprocess.CompletedProcess([], code, stdout, stderr)


@pytest.fixture
def recovery():
    target = dict(socket='/tmp/test-only', session='dev-api-1', pane='%1', pid='123456789',
                  sid=SID, agent='codex', pane_dead='0')
    calls = []
    return {'run': lambda a: result('/tmp/test-only'), 'panes': lambda s: [target],
            'owner': lambda s, p: target, 'descendant': lambda a, b: False,
            'current': lambda t: True, 'tmux': lambda s, *a: calls.append(a) or result(),
            'target': target, 'calls': calls}


@pytest.mark.parametrize('name,args', [
    ('open_session', []), ('open_session', {}),
    ('open_session', {'repo': 'api', 'slot': True}),
    ('open_session', {'repo': 'api', 'slot': 0}),
    ('open_session', {'repo': '--evil', 'slot': 1}),
    ('open_session', {'repo': 'missing', 'slot': 1}),
    ('open_session', {'repo': 'api', 'slot': 1, 'agent': 'shell'}),
    ('open_session', {'repo': 'api', 'slot': 1, 'extra': True}),
    ('kill_session', {'repo': 'api', 'slot': 1, 'confirm': False}),
    ('pause_session', {'repo': 'api', 'slot': 1, 'session_id': 'prefix'}),
    ('pause_session', {'repo': 'api', 'slot': 1, 'session_id': 2}),
    *[('send_instructions', {'repo': 'api', 'slot': 1, 'session_id': SID, 'instructions': text})
      for text in ('', '  ', 4, 'x' * 32001, '\x1b[201~', '\r', '\x85')],
])
def test_validation_precedes_mutations(controls, cfg, monkeypatch, name, args):
    monkeypatch.setattr(controls.subprocess, 'run', lambda *a, **k: pytest.fail('invalid input executed'))
    with pytest.raises(ValueError):
        controls.call(cfg, name, args)


@pytest.mark.parametrize('name,words', [
    ('open_session', ['_t_open', 'api', '1', '--cli', '--codex']),
    ('resume_session', ['_t_resume', 'api', '1', '--cli', '--days', 'all']),
    ('kill_session', ['_t_dev', 'kill', 'api', '1', '-y']),
    ('restart_session', None),
])
def test_lifecycle_uses_existing_guards_detached(controls, cfg, monkeypatch, name, words):
    calls = []
    monkeypatch.setattr(controls, 'shell_run', lambda argv: calls.append(argv) or 'done\n')
    args = {'repo': 'api', 'slot': 1}
    if name == 'open_session': args['agent'] = 'codex'
    if name == 'kill_session': args['confirm'] = True
    response = controls.call(cfg, name, args)
    assert calls[0] == (words or [str(controls.ROOT / 'bin/t'), 'session', 'restart', 'api', '1'])
    assert response == dict(status='completed', output='done', repo='api', slot=1,
                            host='local', open_command='t session open api 1')
    with pytest.raises(ValueError, match='unknown session control'):
        controls.call(cfg, 'bogus', {})


def test_shell_execution_is_quoted_and_has_no_terminal(controls, monkeypatch):
    def run(argv, **kw):
        assert argv[:2] == ['zsh', '-lic']
        assert "'$(touch /tmp/nope)'" in argv[2]
        assert 'T_MCP_DETACHED=1' in argv[2]
        assert kw['stdin'] == subprocess.DEVNULL and kw['start_new_session']
        assert argv[2].endswith('&& pwd -P')
        return result('/work\n')
    monkeypatch.setattr(controls.subprocess, 'run', run)
    assert controls.shell_run(['_t_cd', '$(touch /tmp/nope)'], report_cwd=True) == '/work\n'
    for response, message in [(result(code=1, stderr='denied'), 'denied'),
                              (result('failed', 1), 'failed'), (result(code=1), 'operation failed')]:
        with pytest.raises(ValueError, match=message): controls.checked(response)


@pytest.mark.parametrize('case', ['socket', 'multiple', 'dead', 'agent', 'sid', 'self_env', 'self_pid'])
def test_delivery_refuses_unverified_or_self_target(controls, recovery, monkeypatch, case):
    if case == 'socket': recovery['run'] = lambda a: result('bad')
    if case == 'multiple': recovery['panes'] = lambda s: []
    if case == 'dead': recovery['target']['pane_dead'] = '1'
    if case == 'agent': recovery['target']['agent'] = 'cursor'
    if case == 'sid': recovery['target']['sid'] = 'other'
    if case == 'self_env': monkeypatch.setenv('CODEX_THREAD_ID', SID)
    if case == 'self_pid': recovery['descendant'] = lambda a, b: True
    with pytest.raises(ValueError): controls.target_owner('dev-api-1', SID, recovery)
    assert not recovery['calls']


@pytest.mark.parametrize('name,key,prefix', [('send_instructions', 'instructions', 'Instruction: '),
                                         ('set_session_goal', 'goal', 'New goal: ')])
def test_instructions_are_literal_and_owner_rechecked(controls, recovery, monkeypatch, name, key, prefix):
    message = '/quit\n!echo `touch bad` $(secret)\t雪'
    loads = []
    monkeypatch.setattr(controls.subprocess, 'run', lambda a, **kw: loads.append((a, kw)) or result())
    response = controls.deliver(name, {'session_id': SID, key: message}, 'dev-api-1', recovery)
    assert loads[0][1]['input'] == prefix + message
    assert loads[0][0][-1] == '-'
    assert recovery['calls'][0][0:4] == ('paste-buffer', '-p', '-r', '-d')
    assert recovery['calls'][1] == ('send-keys', '-t', '%1', 'Enter')
    assert recovery['calls'][2][0] == 'delete-buffer'
    assert response['status'] == 'submitted' and response['acknowledged'] is False
    recovery['calls'].clear()
    checks = iter([True, False])
    recovery['current'] = lambda t: next(checks)
    with pytest.raises(ValueError, match='owner changed'):
        controls.deliver(name, {'session_id': SID, key: message}, 'dev-api-1', recovery)
    assert not any(a[0] == 'send-keys' for a in recovery['calls'])
    assert recovery['calls'][-1][0] == 'delete-buffer'


def test_pause_requests_interrupt_without_claiming_paused(controls, cfg, recovery, monkeypatch):
    monkeypatch.setattr(controls.runpy, 'run_path', lambda p: recovery)
    response = controls.call(cfg, 'pause_session', {'repo': 'api', 'slot': 1, 'session_id': SID})
    assert response['status'] == 'interrupt_requested'
    assert response['acknowledged'] is False
    assert recovery['calls'] == [('send-keys', '-t', '%1', 'Escape')]


def test_goal_command_and_contract(t_mod, cfg, monkeypatch, capsys):
    args = t_mod.build_parser().parse_args(t_mod._canonical_argv(['session', 'goal', 'api', '1', 'Fix tests', '--json']))
    calls = []
    monkeypatch.setattr(t_mod, 'zsh_capture', lambda s: f'{SID}\t{cfg.repos["api"]}\tapi-1\tdetached\tactive\tTask\tcodex\n')
    monkeypatch.setattr(t_mod, '_mcp_controls', lambda: {'call': lambda *a: calls.append(a) or {'status': 'submitted'}})
    assert t_mod.cmd_session_goal(cfg, args) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'submitted'
    assert calls[0][2]['goal'] == 'Fix tests' and calls[0][2]['session_id'] == SID
    args.json = False
    assert t_mod.cmd_session_goal(cfg, args) == 0
    assert 'acknowledgement' in capsys.readouterr().out
    monkeypatch.setattr(t_mod, 'zsh_capture', lambda s: '')
    assert t_mod.cmd_session_goal(cfg, args) == 1
    assert 't session goal' in capsys.readouterr().err
    assert 'session goal' in t_mod._group_help('session') or 'goal' in t_mod._group_help('session')


@pytest.mark.parametrize('verb', ['push', 'pop', 'move', 'search', 'paste'])
def test_terminal_modes_return_handoff_without_execution(controls, cfg, t_mod, monkeypatch, verb):
    monkeypatch.setattr(t_mod, '_t_version', lambda: 'test')
    monkeypatch.setattr(controls.subprocess, 'run', lambda *a, **kw: pytest.fail('terminal operation ran'))
    response = controls.call(cfg, 'session_command', {'command': verb}, vars(t_mod))
    assert response['status'] == 'terminal_required'
    assert response['command'] == 't session ' + verb


@pytest.mark.parametrize('verb,arguments,expected', [
    ('open', ['api', '1', '--codex'], 'open_session'),
    ('resume', ['api', '1'], 'resume_session'),
    ('close', ['api', '1', '-y'], 'kill_session'),
    ('restart', ['api', '1'], 'restart_session')])
def test_session_dispatch_lifecycle(controls, cfg, t_mod, monkeypatch, verb, arguments, expected):
    calls = []
    monkeypatch.setattr(controls, 'call', lambda *a: calls.append(a) or {})
    controls.session_command(cfg, {'command': verb, 'arguments': arguments}, vars(t_mod))
    assert calls[0][1] == expected and calls[0][2]['slot'] == 1


@pytest.mark.parametrize('args', [{}, [], {'command': 'kill'}, {'command': 'list', 'arguments': 'bad'},
                                 {'command': 'list', 'arguments': [1]}, {'command': 'list', 'extra': True},
                                 {'command': 'list', 'arguments': ['\x00']},
                                 {'command': 'list', 'arguments': ['--bogus']},
                                 {'command': 'open'}, {'command': 'open', 'arguments': ['api', 'all']},
                                 {'command': 'cd'}, {'command': 'read'}, {'command': 'view-plan'},
                                 {'command': 'open-app'}])
def test_session_dispatch_invalid_arguments(controls, cfg, t_mod, args):
    with pytest.raises(ValueError): controls.session_command(cfg, args, vars(t_mod))


def test_all_public_session_verbs_are_exposed(controls, t_mod):
    assert set(controls.SESSION_VERBS) == set(t_mod.COMMAND_GROUPS['session'])
    assert len(t_mod._mcp_tools()) == 11


def test_generic_read_help_and_conditional_modes(controls, cfg, t_mod, monkeypatch):
    calls = []
    monkeypatch.setattr(controls.subprocess, 'run', lambda a, **kw: calls.append((a, kw)) or result('output'))
    for verb, args, added in [('read', ['api', '1'], '--dump'), ('history', ['api', '1'], '--json'),
                              ('goal', ['api', '1', 'new objective'], 'new objective'),
                              ('view-plan', ['api', '1'], '1'), ('open-app', ['api', '1'], '1')]:
        assert controls.session_command(cfg, {'command': verb, 'arguments': args}, vars(t_mod))['output'] == 'output'
        assert calls[-1][0][-1] == added
        assert calls[-1][1]['stdin'] == subprocess.DEVNULL
    for args in [['api', '1', '--fg'], ['api', '1', '--app'], ['api', '1', '--host=mini']]:
        assert controls.session_command(cfg, {'command': 'open', 'arguments': args}, vars(t_mod))['status'] == 'terminal_required'
    assert 'usage:' in controls.session_command(cfg, {'command': 'list', 'arguments': ['--help']}, vars(t_mod))['output'].lower()
    monkeypatch.setattr(controls, 'shell_run', lambda *a, **kw: '/some/worktree\n')
    assert controls.session_command(cfg, {'command': 'cd', 'arguments': ['api', '1']}, vars(t_mod))['cwd'] == '/some/worktree'
    monkeypatch.setattr(t_mod, '_ls_scope', lambda *a: ('', ''))
    monkeypatch.setattr(controls, 'shell_run', lambda *a, **kw: f'{SID}\t/repo\tapi-1\tdetached\tactive\ttask\tcodex\n')
    rows = controls.session_command(cfg, {'command': 'list'}, vars(t_mod))['sessions']
    assert rows[0]['sid'] == SID and rows[0]['agent'] == 'codex'


def test_legacy_allow_migrates_without_widening_custom_rules(t_mod, tmp_path):
    path = tmp_path / 'settings.json'
    path.write_text(json.dumps({'permissions': {'allow': ['Bash(ls)', 'mcp__sessions', 'mcp__sessions__pause_session'], 'deny': ['x']}}))
    assert t_mod._mcp_allow_write(str(path))
    perms = json.loads(path.read_text())['permissions']
    assert perms['allow'] == ['Bash(ls)', 'mcp__sessions__pause_session', *t_mod._MCP_READ_RULES]
    assert perms['deny'] == ['x']
    assert not t_mod._mcp_allow_write(str(path))


@pytest.mark.parametrize('machine,tty', [(True, True), (False, False), (False, True)])
def test_spinner_output_and_exception_cleanup(t_mod, monkeypatch, machine, tty):
    class Stream(io.StringIO):
        def isatty(self): return tty
    out, err = Stream(), Stream()
    monkeypatch.setattr(t_mod.sys, 'stdout', out)
    monkeypatch.setattr(t_mod.sys, 'stderr', err)
    monkeypatch.setenv('TERM', 'xterm-256color')
    with pytest.raises(KeyboardInterrupt):
        with t_mod._spinner('Waiting', machine=machine):
            time.sleep(.55)
            raise KeyboardInterrupt
    assert out.getvalue() == ''
    if not machine and tty:
        assert 'Waiting' in err.getvalue() and '⠋' in err.getvalue() and '⠙' in err.getvalue()
        assert err.getvalue().endswith('\r\x1b[2K')
        before = err.getvalue()
        time.sleep(.2)
        assert err.getvalue() == before
    else:
        assert err.getvalue() == ''


def test_shell_spinner_preserves_status_and_capture(zsh):
    script = '''
TERM=xterm-256color
_T_PROGRESS=1
slow() { sleep 0.5; print data; return 7; }
value=$(_t_with_progress 'Waiting for sessions' slow)
rc=$?
print -r -- "RESULT=$value RC=$rc"
'''
    normal = zsh(script)
    assert normal.stdout == 'RESULT=data RC=7\n' and not normal.stderr
    tty = zsh(script, _tty=True)
    assert 'Waiting for sessions' in tty.stdout and 'RESULT=data RC=7' in tty.stdout
    assert '\x1b[2K' in tty.stdout
    diagnostic = zsh(script.replace('print data;', 'print -u2 failure; print data;'), _tty=True)
    assert '\x1b[2Kfailure' in diagnostic.stdout
    assert 'RESULT=data RC=7' in diagnostic.stdout


def test_remote_scan_does_not_wait_for_its_spinner(zsh):
    response = zsh('''
TERM=xterm-256color
_T_PROGRESS=1
REMOTE_HOSTS=(mini test.invalid)
_dev_session_rows_impl() { sleep 0.4; print local-row; }
_dev_remote_scan_with_timeout() { print remote-row; }
rows=$(_dev_rows_all)
print -r -- "ROWS=$rows RC=$?"
''', _tty=True)
    assert response.returncode == 0
    assert 'Checking sessions and remote hosts' in response.stdout
    assert 'local\tlocal-row' in response.stdout and 'mini\tremote-row' in response.stdout
    assert 'RC=0' in response.stdout


def test_real_tmux_literal_bracketed_paste_and_pause(controls, tmp_path, monkeypatch):
    if not shutil.which('tmux'):
        pytest.skip('tmux unavailable')
    for name in list(os.environ):
        if name.startswith('COV_CORE_'):
            monkeypatch.delenv(name)
    monkeypatch.setenv('HOME', str(tmp_path))
    for name in ('XDG_CONFIG_HOME', 'XDG_CACHE_HOME', 'XDG_STATE_HOME'):
        monkeypatch.setenv(name, str(tmp_path / name))
    monkeypatch.delenv('TMUX', raising=False)
    captured = tmp_path / 'bytes'
    ready = tmp_path / 'ready'
    program = ("import os,tty,pathlib; tty.setraw(0); os.write(1,b'\\x1b[?2004h'); "
               f"pathlib.Path({str(ready)!r}).touch(); f=open({str(captured)!r},'ab',buffering=0); "
               "exec('while True: f.write(os.read(0,4096))')")
    with tempfile.TemporaryDirectory(prefix='t188-') as directory:
        socket = directory + '/tmux'
        def tmux(*args):
            return subprocess.run(['tmux', '-S', socket, *args], capture_output=True, text=True, timeout=5)
        try:
            assert tmux('-f', '/dev/null', 'new-session', '-d', '-s', 'dev-api-1',
                        shlex.join([sys.executable, '-c', program])).returncode == 0
            deadline = time.monotonic() + 5
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(.02)
            assert ready.exists()
            pane = tmux('display-message', '-p', '-t', '=dev-api-1:', '#{pane_id}').stdout.strip()
            target = dict(socket=socket, session='dev-api-1', pane=pane, pid='99999999',
                          sid=SID, agent='claude', pane_dead='0')
            recovery = dict(run=lambda a: result(socket), panes=lambda s: [target],
                            owner=lambda s, p: target, descendant=lambda a, b: False,
                            current=lambda t: True, tmux=lambda s, *a: tmux(*a))
            text = '/quit\n!echo $(touch never) `uname`\t雪'
            controls.deliver('send_instructions', {'session_id': SID, 'instructions': text}, 'dev-api-1', recovery)
            controls.deliver('pause_session', {'session_id': SID}, 'dev-api-1', recovery)
            expected = b'\x1b[200~' + ('Instruction: ' + text).encode() + b'\x1b[201~\r\x1b'
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline and (not captured.exists() or captured.stat().st_size < len(expected)):
                time.sleep(.02)
            assert captured.read_bytes() == expected
            assert 't-instruction-' not in tmux('list-buffers').stdout
        finally:
            tmux('kill-server')


def test_detached_attachment_and_self_close_guards(zsh):
    script = f'''
T_MCP_DETACHED=1
tmux() {{ print -r -- "TMUX $*"; }}
_t_session_attach dev-api-1
_dev_self_sid() {{ print {SID}; }}
_dev_session_sid() {{ print {SID}; }}
_dev_tmux_session_of_pid() {{ print dev-api-1; }}
_dev_kill_one dev-api-1 1
'''
    response = zsh(script)
    assert response.returncode == 1
    assert 'has-session' in response.stdout and 'attach-session' not in response.stdout
    assert 'kill-session' not in response.stdout
    assert 'cannot close the calling agent' in response.stderr


def test_mcp_control_wire_and_cli_dispatch(t_mod, cfg, monkeypatch, capsys):
    calls = []
    specs = t_mod._mcp_controls()['tool_specs']
    monkeypatch.setattr(t_mod, '_mcp_controls', lambda: {'call': lambda *a: calls.append(a) or {'status': 'completed'}, 'tool_specs': specs})
    monkeypatch.setattr(t_mod, '_mcp_context', lambda *a: pytest.fail('control indexed transcripts'))
    request = {'id': 9, 'method': 'tools/call', 'params': {'name': 'open_session', 'arguments': {'repo': 'api', 'slot': 1}}}
    reply = t_mod._mcp_handle(request, t_mod._mcp_tools(), lambda n, a: t_mod._mcp_call(cfg, {}, n, a))
    assert not reply['result']['isError']
    assert json.loads(reply['result']['content'][0]['text'])['status'] == 'completed'
    assert calls[0][1:3] == ('open_session', {'repo': 'api', 'slot': 1})
    args = t_mod.build_parser().parse_args(['mcp', '--call', 'open_session', '{"repo":"api","slot":1}'])
    assert t_mod.cmd_mcp(cfg, args) == 0
    assert json.loads(capsys.readouterr().out)['status'] == 'completed'
