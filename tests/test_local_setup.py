"""All installers, model downloads, and Codex registration are mocked."""
from contextlib import contextmanager
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import load_script


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path / 'home'))
    monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'config'))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    return load_script(Path(__file__).parents[1] / 'libexec/t_local_setup.py', 'local_setup_test')


def args(**kw):
    return SimpleNamespace(show=False, local_model=None, disable_local=False, yes=False, **kw)


def test_status_readonly(setup, t_mod, monkeypatch, capsys):
    monkeypatch.setattr(setup.subprocess, 'run', lambda *a, **kw: pytest.fail('status ran a command'))
    assert setup.run(SimpleNamespace(show=True), vars(t_mod)) == 0
    assert 'enabled: False' in capsys.readouterr().out
    assert not setup.worker()['config_path']().exists()
    assert t_mod._command_readonly(['config', 'local', '--show'])
    assert not t_mod._command_readonly(['config', 'local', '--model', 'gpt-oss:20b', '--yes'])


def test_installation_plans(setup, t_mod, monkeypatch):
    w = setup.worker()
    w['executable'] = lambda name: None
    monkeypatch.setattr(setup, 'worker', lambda: w)
    monkeypatch.setattr(setup.shutil, 'which', lambda name: '/brew')
    monkeypatch.setattr(setup.sys, 'platform', 'darwin')
    plan = setup.installation_plan(vars(t_mod), 'gpt-oss:20b')
    assert plan[0][1] == ['sh', '-c', 'brew install --cask codex']
    assert plan[1][1] == ['brew', 'install', 'ollama']
    monkeypatch.setattr(setup.sys, 'platform', 'linux')
    assert 'https://ollama.com/install.sh' in setup.installation_plan(vars(t_mod), 'test')[1][1][-1]
    monkeypatch.setattr(setup.sys, 'platform', 'unsupported')
    with pytest.raises(ValueError): setup.installation_plan(vars(t_mod), 'test')


def test_setup_and_disable(setup, t_mod, monkeypatch, capsys):
    w = setup.worker()
    w['executable'] = lambda name: '/fake/' + name
    @contextmanager
    def server(): yield 'http://127.0.0.1:9999', {'HOME': '/isolated'}
    w['ollama_server'] = server
    w['check_model'] = lambda base, model: {'capabilities': ['tools']}
    monkeypatch.setattr(setup, 'worker', lambda: w)
    calls, registered = [], []
    def run(command, **kw):
        calls.append(command)
        if 'list' in command:
            return SimpleNamespace(returncode=0, stdout=json.dumps(registered), stderr='')
        if 'add' in command:
            registered.append({'name': setup.SERVER, 'transport': {'command': str(setup.ROOT / 'bin/t'),
                                                                   'args': ['mcp', '--local-worker']}})
        return SimpleNamespace(returncode=0, stdout='--ignore-user-config', stderr='')
    monkeypatch.setattr(setup.subprocess, 'run', run)
    assert setup.run(SimpleNamespace(show=False, local_model='gpt-oss:20b', yes=True), vars(t_mod)) == 0
    assert w['settings']()['enabled']
    assert any(command[1:3] == ['pull', 'gpt-oss:20b'] for command in calls)
    assert any(command[1:4] == ['mcp', 'add', setup.SERVER] for command in calls)
    assert setup.run(SimpleNamespace(show=False, disable_local=True, yes=True), vars(t_mod)) == 0
    assert not w['settings']()['enabled']
    assert 'disabled' in capsys.readouterr().out


def test_registration_conflicts_and_errors(setup, monkeypatch):
    entry = {'name': setup.SERVER, 'transport': {'command': '/unrelated', 'args': []}}
    monkeypatch.setattr(setup.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps([entry]), stderr=''))
    with pytest.raises(ValueError, match='different'): setup.check_registration('codex', '/t')
    monkeypatch.setattr(setup.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='', stderr='denied'))
    with pytest.raises(ValueError, match='denied'): setup.registration('codex')
    monkeypatch.setattr(setup.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout='{}', stderr=''))
    with pytest.raises(ValueError, match='unexpected'): setup.registration('codex')


def test_invalid_and_nonterminal(setup, t_mod, monkeypatch):
    monkeypatch.setattr(setup.sys.stdin, 'isatty', lambda: False)
    assert setup.run(args(), vars(t_mod)) == 1
    assert setup.run(SimpleNamespace(yes=True), vars(t_mod)) == 1
    assert setup.run(SimpleNamespace(yes=True, local_model='--bad'), vars(t_mod)) == 1
    monkeypatch.setattr(setup, 'setup', lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    assert setup.run(SimpleNamespace(yes=True, local_model='test'), vars(t_mod)) == 130


def test_interactive_review(setup, t_mod, monkeypatch):
    monkeypatch.setattr(setup.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(setup.sys.stdout, 'isatty', lambda: True)
    class UI:
        def raw(self): pass
        def restore(self): pass
        def done(self, *a): pass
        def cols(self): return 80
        def page(self, *a, **kw): return 'y'
        def pick(self, label, rows, **kw): return rows[0][0]
    api = dict(vars(t_mod), _RailUI=UI)
    called = []
    monkeypatch.setattr(setup, 'setup', lambda *a: called.append(a))
    assert setup.run(args(), api) == 0
    assert called[0][1] == 'gpt-oss:20b'
    monkeypatch.setattr(UI, 'page', lambda *a, **kw: 'n')
    assert setup.run(args(), api) == 130
    monkeypatch.setattr(UI, 'pick', lambda *a, **kw: None)
    assert setup.run(args(), api) == 130


def test_cli_and_mcp_dispatch(t_mod, setup, monkeypatch, capsys):
    canonical = t_mod._canonical_argv(['config', 'local', '--show'])
    assert canonical == ['config', '--local', '--show']
    assert t_mod._canonical_error(['config', 'local', '--show']) is None
    parsed = t_mod.build_parser().parse_args(canonical)
    monkeypatch.setattr(t_mod, '_local_setup', lambda args: 37)
    assert t_mod.cmd_config(None, parsed) == 37
    assert t_mod.cmd_config(None, SimpleNamespace(local_model='model')) == 1
    parsed = t_mod.build_parser().parse_args(['mcp', '--local-worker', '--list-tools'])
    assert t_mod.cmd_mcp(None, parsed) == 0
    assert 'start_task' in capsys.readouterr().out
    parsed = t_mod.build_parser().parse_args(['mcp', '--local-worker', '--install'])
    assert t_mod.cmd_mcp(None, parsed) == 1
    parsed = t_mod.build_parser().parse_args(['mcp', '--local-worker'])
    monkeypatch.setattr(t_mod, '_local_worker', lambda: {'serve': lambda api: 31})
    assert t_mod.cmd_mcp(None, parsed) == 31


def test_setup_failures_do_not_enable(setup, t_mod, monkeypatch):
    w = setup.worker()
    monkeypatch.setattr(setup, 'worker', lambda: w)
    calls = []
    monkeypatch.setattr(setup.subprocess, 'run', lambda command, **kw:
                        calls.append(command) or SimpleNamespace(returncode=0, stdout='', stderr=''))
    w['executable'] = lambda name: None
    with pytest.raises(ValueError, match='did not expose'):
        setup.setup(vars(t_mod), 'test', [('install', ['fake-install'])])
    assert calls == [['fake-install']]
    assert not w['settings']()['enabled']
    w['executable'] = lambda name: '/fake/' + name
    with pytest.raises(ValueError, match='update Codex'):
        setup.setup(vars(t_mod), 'test', [])
    assert not w['settings']()['enabled']


def test_config_hub_local_route(t_mod, monkeypatch):
    # Opening setup from the existing config hub passes through after terminal restore.
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(t_mod.sys.stdout, 'isatty', lambda: True)
    monkeypatch.setattr(t_mod, '_config_read', lambda *a: '')
    monkeypatch.setattr(t_mod, '_config_wizard', lambda cfg: 'local')
    monkeypatch.setattr(t_mod, '_local_setup', lambda args: 29)
    assert t_mod.cmd_config(object(), SimpleNamespace(show=False)) == 29
    monkeypatch.setattr(t_mod, '_local_setup', lambda args: 0)


def test_local_help(t_mod, capsys):
    assert t_mod.main(['config', 'local', '--help']) == 0
    output = capsys.readouterr().out
    assert '--model NAME' in output and '--show' in output and '--yes' in output
