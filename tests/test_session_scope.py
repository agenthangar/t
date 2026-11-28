"""Shared public selector parsing keeps source and destination independent."""
import runpy
from pathlib import Path

import pytest

SCOPE = runpy.run_path(str(Path(__file__).parents[1] / 'libexec/t_session_scope.py'))


@pytest.mark.parametrize('command', ['ls', 'open', 'kill', 'move', 'resume'])
def test_shared_selectors_normalize_without_changing_destination(command):
    arguments, source = SCOPE['normalize']([
        command, '--repo', 'api', '4', '--source', 'build', '--host', 'mini', '-w'])
    assert arguments == [command, 'api', '4', '--host', 'mini', '-w']
    assert source == 'build'


@pytest.mark.parametrize('arguments', [
    ['open', '--source'], ['kill', '--repo'], ['resume', 'web', '--repo', 'api'],
    ['ls', 'api', '--all'], ['move', '--source', 'local', '--source', 'remote'],
])
def test_conflicting_or_missing_selectors_fail(arguments):
    with pytest.raises(ValueError):
        SCOPE['normalize'](arguments)


def test_source_selection_and_reserved_host_alias():
    hosts = {'build': 'build.example', 'all': 'all.example'}
    assert SCOPE['targets'](hosts, 'local') == {'local': 'local'}
    assert SCOPE['targets'](hosts, 'remote') == hosts
    assert SCOPE['targets'](hosts, 'host:all') == {'all': 'all.example'}
    with pytest.raises(ValueError, match='unknown source'):
        SCOPE['targets'](hosts, 'missing')


def test_other_command_payload_is_unchanged():
    args = ['hosts', 'run', 'build', '--source', 'anything']
    assert SCOPE['normalize'](args) == (args, None)


@pytest.mark.parametrize('arguments', [
    ['open', '--source', 'remote', '--local'],
    ['ls', '--source', 'build', '-l'],
    ['kill', '--source', 'local', '-r'],
])
def test_legacy_source_conflicts(arguments):
    with pytest.raises(ValueError, match='conflict'):
        SCOPE['normalize'](arguments)


def test_move_destination_here_and_resume_live_are_independent_of_source():
    assert SCOPE['normalize'](['move', '--here', '--source', 'build']) == (['move', '--here'], 'build')
    assert SCOPE['normalize'](['resume', '-l', '--source', 'local']) == (['resume', '-l'], 'local')


@pytest.mark.parametrize('source,expected', [
    ('local', 'REMOTE_HOSTS=(); _t_dev kill api 2'),
    ('remote', "REMOTE_HOSTS=(build build.example); _dev_remote_kill api 2"),
    ('build', "REMOTE_HOSTS=(build build.example); _dev_remote_kill api 2"),
])
def test_explicit_close_limits_dispatch_to_selected_source(t_mod, monkeypatch, source, expected):
    from types import SimpleNamespace
    calls = []
    monkeypatch.setenv('T_SESSION_SOURCE', source)
    monkeypatch.setattr(t_mod, '_delegate', calls.append)
    monkeypatch.setattr(t_mod, '_t_source_root', lambda: str(Path(__file__).parents[1]))
    config = SimpleNamespace(repos={'api': '/example/api'}, hosts={'build': 'build.example'})
    args = SimpleNamespace(repo='api', slot='2', yes=False, remote=False, wait=False, all=False)
    t_mod.cmd_kill(config, args)
    assert calls == [expected]


def test_close_browse_infers_current_repository(t_mod, monkeypatch):
    from types import SimpleNamespace
    calls = []
    monkeypatch.setattr(t_mod, '_delegate', calls.append)
    monkeypatch.setattr(t_mod.os, 'getcwd', lambda: '/example/worktrees/api/api-9')
    config = SimpleNamespace(repos={'api': '/example/api'}, hosts={}, worktree_root='/example/worktrees')
    args = SimpleNamespace(repo=None, slot=None, yes=False, remote=False, wait=False, all=False)
    t_mod.cmd_kill(config, args)
    assert calls == ["_dev_kill_pick api '' ''"]


def test_public_source_scope_is_invocation_local(t_mod, monkeypatch):
    from types import SimpleNamespace
    observed = []
    monkeypatch.setattr(t_mod, 'Config', lambda: SimpleNamespace(hosts={'build': 'build.example'}))
    monkeypatch.setattr(t_mod, '_main', lambda args: observed.append(
        (args, t_mod.os.environ.get('T_SESSION_SOURCE'))) or 0)
    monkeypatch.delenv('T_SESSION_SOURCE', raising=False)
    assert t_mod.main(['ls', '--source', 'build']) == 0
    assert 'T_SESSION_SOURCE' not in t_mod.os.environ
    assert t_mod.main(['ls']) == 0
    assert observed == [(['ls'], 'build'), (['ls'], None)]


def test_discovery_source_supports_different_remote_paths(monkeypatch):
    import sys
    from types import SimpleNamespace
    root = Path(__file__).parents[1]
    sys.path.insert(0, str(root / 'libexec'))
    cache = runpy.run_path(str(root / 'libexec/t_session_discovery.py'))
    def request(operation, payload, **options):
        assert [host['host'] for host in payload['hosts']] == ['build']
        return {'hosts': [dict(host='build', value={
            'rows': 'sid\t/srv/work/api/7\tapi-7\tdetached\tactive\tTask\tcodex\n',
            'catalog': {'repositories': [dict(repo='github:org/api', alias='api', path='/srv/repos/api')]},
            'scopes': {'github:org/api': ['/srv/repos/api', '/srv/work/api']},
        })]}
    monkeypatch.setenv('T_SESSION_SOURCE', 'build')
    monkeypatch.delenv('T_DISCOVERY_COLLECTOR', raising=False)
    monkeypatch.setitem(cache['discover'].__globals__, 'request', request)
    config = SimpleNamespace(hosts={'build': 'build.example'}, repos={'api': '/local/api'})
    view = cache['discover'](config, fields=['rows', 'catalog', 'scopes'])
    assert '/srv/work/api/7' in cache['rows'](view, repo='api', local_roots=['/local/api'])


def test_close_wait_and_source_reach_delegate(t_mod, monkeypatch):
    from types import SimpleNamespace
    calls = []
    monkeypatch.setenv('T_SESSION_SOURCE', 'build')
    monkeypatch.setattr(t_mod, '_delegate', lambda command: calls.append(
        (command, t_mod.os.environ['T_SESSION_SOURCE'])))
    monkeypatch.setattr(t_mod, '_t_source_root', lambda: str(Path(__file__).parents[1]))
    cfg = SimpleNamespace(repos={'api': '/example/api'}, hosts={'build': 'build.example'})
    args = SimpleNamespace(repo='api', slot='2', yes=True, remote=False, wait=True, all=False)
    t_mod.cmd_kill(cfg, args)
    assert calls == [('export T_DISCOVERY_EXPLICIT_WAIT=1; REMOTE_HOSTS=(build build.example); _dev_remote_kill api 2 1', 'build')]


def test_close_wait_is_exported_after_source_assignments(t_mod, monkeypatch):
    import subprocess
    from types import SimpleNamespace
    captured = []
    def execute(command):
        script = 'typeset -A REMOTE_HOSTS; _dev_remote_kill() { print -r -- "$T_DISCOVERY_EXPLICIT_WAIT/${REMOTE_HOSTS[build]}"; }; ' + command
        result = subprocess.run(['zsh', '-f', '-c', script], capture_output=True, text=True, check=True)
        captured.append(result.stdout.strip())
    monkeypatch.setenv('T_SESSION_SOURCE', 'build')
    monkeypatch.setattr(t_mod, '_delegate', execute)
    monkeypatch.setattr(t_mod, '_t_source_root', lambda: str(Path(__file__).parents[1]))
    cfg = SimpleNamespace(repos={'api': '/example/api'}, hosts={'build': 'build.example'})
    args = SimpleNamespace(repo='api', slot='2', yes=True, remote=False, wait=True, all=False)
    t_mod.cmd_kill(cfg, args)
    assert captured == ['1/build.example']


from test_agent_seam import zsh  # noqa: E402,F401


@pytest.mark.parametrize('target', ['deadbeef', 'api:deadbeef', 'api fg', 'fg'])
def test_remote_source_foreground_handles_never_try_local(zsh, target):
    result = zsh('''
        _dev_remote_fg_open() { print -r -- "REMOTE_HANDLE $1"; return 2; }
        _t_dev() { print WRONG_LOCAL; }
        T_SESSION_SOURCE=remote
        _t_open ''' + target + '''; print -r -- "status=$?"
    ''')
    assert 'REMOTE_HANDLE' in result.stdout
    assert 'status=2' in result.stdout
    assert 'WRONG_LOCAL' not in result.stdout


@pytest.mark.parametrize('target', ['deadbeef', 'api:deadbeef', 'api fg', 'fg'])
def test_named_source_foreground_handles_route_to_exact_host(zsh, target):
    result = zsh('''
        REMOTE_HOSTS[build]=build.example
        _dev_remote_open() { print -r -- "EXACT_HOST $1"; }
        _t_dev() { print WRONG_LOCAL; }
        T_SESSION_SOURCE=build
        _t_open ''' + target)
    assert 'EXACT_HOST build' in result.stdout
    assert 'WRONG_LOCAL' not in result.stdout


def test_local_source_removes_remote_foreground_fallback(zsh):
    result = zsh('''
        REMOTE_HOSTS[build]=build.example
        _t_dev() { print -r -- "LOCAL_HOSTS ${#REMOTE_HOSTS}"; }
        T_SESSION_SOURCE=local
        _t_open deadbeef
    ''')
    assert 'LOCAL_HOSTS 0' in result.stdout
