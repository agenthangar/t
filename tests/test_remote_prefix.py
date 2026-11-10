"""Generic remote commands never use real SSH, agent homes, or terminal state."""
import argparse
import base64
import io
import json
import os
import shlex
from types import SimpleNamespace

import pytest

from test_agent_seam import zsh  # noqa: F401


@pytest.fixture
def remote(t_mod):
    return t_mod._REMOTE


@pytest.fixture
def cfg(tmp_path):
    return SimpleNamespace(hosts={'beta': 'user@beta', 'alpha': 'user@alpha', 'alias': 'user@beta'},
                           repos={'api': str(tmp_path / 'checkout')},
                           resolve_repo=lambda name: 'api' if name in ('api', 'application') else None,
                           repo_of_dir=lambda cwd: ('api', '2'))


def test_registry_policy_and_parser(t_mod, remote):
    parser = t_mod.build_parser()
    for path in remote['leaves'](t_mod.COMMAND_GROUPS['remote']):
        if len(path) == 1:
            continue
        assert remote['policy'](path) in ('query', 'single', 'session', 'unsupported')
        args = parser.parse_args(['remote', *path])
        assert args.verb == 'remote' and args.action == 'execute'
        assert args.rest == list(path)
        if path[-1] == 'list':
            assert vars(parser.parse_args(['remote', *path[:-1], 'ls'])) == vars(args)


@pytest.mark.parametrize('words,expected', [
    (['eval', 'config', 'list'], 'query'), (['eval', 'runs', 'list'], 'query'),
    (['eval', 'benchmarks', 'list'], 'query'),
    (['session', 'list'], 'query'), (['session', 'goal', 'api', '1', 'hello', '--json'], 'single'),
    (['runners', 'runs', 'start', '--json'], 'single'),
    (['session', 'history', 'api', '1', '--json'], 'query'),
    (['session', 'history', 'api', '1'], 'single'),
    (['system', 'diagnose', '--json'], 'query'), (['system', 'diagnose', '--repair'], 'single'),
    (['system', 'update', '--check'], 'query'), (['system', 'update'], 'single'),
    (['hosts', 'default'], 'query'), (['hosts', 'default', 'alpha'], 'single'),
    (['agent', 'trust', '--status'], 'query'), (['agent', 'login', '--status'], 'query'),
    (['config', 'local', '--show'], 'query'), (['config', 'local', '--model', 'x'], 'single'),
    (['repo', 'cd', 'api'], 'unsupported'), (['session', 'open', 'api'], 'session'),
    (['system', 'advisories'], 'query'), (['system', 'advisories', 'refresh'], 'single'),
])
def test_mode_classification(t_mod, remote, words, expected):
    path, tail, _ = remote['split'](words, t_mod.COMMAND_GROUPS)
    assert remote['policy'](path, tail) == expected


@pytest.mark.parametrize('prefix', [['remote'], ['remote', '--host', 'alpha']])
@pytest.mark.parametrize('words', [
    ['config', 'local', '--show'], ['config', 'local', '--edit'],
    ['workflow', 'instructions', 'show', '--edit'],
])
def test_remote_inherits_native_action_boundary(t_mod, prefix, words):
    assert t_mod._canonical_error([*prefix, *words]) == t_mod._canonical_error(words)


def test_remote_action_boundary_rejects_incomplete_command(t_mod):
    assert 'complete public command' in t_mod._canonical_error(['remote', 'session'])


@pytest.mark.parametrize('words,path,tail,host', [
    (['session', 'ls', 'alpha', 'api'], ('session', 'list'), ['alpha', 'api'], None),
    (['--host', 'alpha', 'session', 'ls', 'api'], ('session', 'list'), ['api'], 'alpha'),
    (['--host=alpha', 'runners', 'runs', 'start', '--', 'echo', 'ls'], ('runners', 'runs', 'start'), ['--', 'echo', 'ls'], 'alpha'),
    (['kill', 'api', '1'], ('session', 'close'), ['api', '1'], None),
])
def test_split_preserves_child_arguments(t_mod, remote, words, path, tail, host):
    assert remote['split'](words, t_mod.COMMAND_GROUPS) == (path, tail, host)


@pytest.mark.parametrize('words', [[], ['--host'], ['--host='], ['--host', '--json'], ['remote', 'session', 'list'], ['session'], ['__resolve'], ['share', '--to', 'x']])
def test_split_rejects_nonpublic_or_incomplete(t_mod, remote, words):
    with pytest.raises(ValueError):
        remote['split'](words, t_mod.COMMAND_GROUPS)


def test_host_boundary(remote, cfg):
    assert remote['select_host'](('session', 'list'), ['alpha', 'api'], None, cfg) == (['api'], 'alpha')
    assert remote['select_host'](('session', 'list'), ['api'], None, cfg) == (['api'], None)
    cfg.hosts['api'] = 'other'
    with pytest.raises(ValueError, match='both'):
        remote['select_host'](('session', 'list'), ['api'], None, cfg)
    for host in ('-bad', 'bad host', 'local'):
        with pytest.raises(ValueError):
            remote['select_host'](('session', 'list'), [], host, cfg)


def mock_api(t_mod, cfg):
    api = dict(vars(t_mod))
    api['Config'] = lambda: cfg
    api['_remote_history'] = lambda cfg: SimpleNamespace(
        identity=lambda alias: 'org/project', repository_alias=lambda identity: 'api' if identity == 'org/project' else None,
        store=SimpleNamespace(close=lambda: None))
    return api


def test_repository_context_maps_only_selector(t_mod, remote, cfg):
    api = mock_api(t_mod, cfg)
    path = ('session', 'goal')
    tail = ['application', '2', 'application --json; $(nope)']
    args = remote['child_args'](api, path, tail)
    context = remote['repository_context'](api, cfg, path, tail, args)
    assert context == {'identity': 'org/project', 'index': 0, 'option': False}
    assert tail[-1] == 'application --json; $(nope)'
    for tail in (['--repo', 'application'], ['--repo=application']):
        args = remote['child_args'](api, ('workflow', 'list'), tail)
        context = remote['repository_context'](api, cfg, ('workflow', 'list'), tail, args)
        assert context['index'] == (1 if len(tail) == 2 else 0)
        assert context['option'] == (len(tail) == 1)
    assert remote['repository_context'](api, cfg, ('session', 'list'), [], SimpleNamespace()) is None
    assert remote['repository_context'](api, cfg, ('repo', 'locate'), [], SimpleNamespace())['index'] is None
    assert remote['repository_context'](api, cfg, ('repo', 'locate'), ['--all'], SimpleNamespace()) is None


@pytest.mark.parametrize('words', [['session', 'ls'], ['workflow', 'list', '--json']])
def test_aggregate_queries_are_sorted_deduplicated_and_noninteractive(t_mod, remote, cfg, monkeypatch, capsys, words):
    seen = []
    def run(host, target, path, tail, context, **kw):
        seen.append((host, target, path, kw))
        return host, 0, '{"ok":true}\n' if '--json' in tail else 'rows\n', ''
    monkeypatch.setitem(remote['execute'].__globals__, 'run_host', run)
    rc = remote['execute'](cfg, SimpleNamespace(rest=words), mock_api(t_mod, cfg))
    assert rc == 0 and sorted(x[0] for x in seen) == ['alias', 'alpha']
    assert all(x[3] == {'query': True, 'terminal': False} for x in seen)
    out = capsys.readouterr().out
    if '--json' in words:
        assert json.loads(out) == {'results': {'alias': {'ok': True}, 'alpha': {'ok': True}}, 'errors': {}}
    else:
        assert out.index('alias:') < out.index('alpha:')


def test_partial_failures_and_single_output(remote, capsys):
    assert remote['render_results']([('a', 0, 'ok\n', ''), ('b', 255, '', 'offline')]) == 1
    out = capsys.readouterr()
    assert 'ok' in out.out and 'b' in out.err
    assert remote['render_results']([('a', 7, 'raw', 'err')], single=True) == 7
    out = capsys.readouterr()
    assert (out.out, out.err) == ('raw', 'err')
    assert remote['render_results']([('a', 0, 'bad', ''), ('b', 3, '', '')], machine=True) == 1
    data = json.loads(capsys.readouterr().out)
    assert data['results'] == {} and data['errors']['a']['status'] == 1 and data['errors']['b']['status'] == 3


def test_host_actions_never_fan_out_or_treat_json_as_readonly(t_mod, remote, cfg, monkeypatch, capsys):
    seen = []
    monkeypatch.setitem(remote['execute'].__globals__, 'run_host', lambda *a, **k: seen.append((a, k)) or 19)
    api = mock_api(t_mod, cfg)
    argv = ['session', 'goal', 'alpha', 'api', '2', 'hello', '--json']
    assert remote['execute'](cfg, SimpleNamespace(rest=argv), api) == 19
    assert len(seen) == 1 and seen[0][1] == {'query': False, 'terminal': False}
    assert remote['execute'](cfg, SimpleNamespace(rest=['session', 'goal', 'api', '2', 'hello', '--json']), api) == 2
    assert 'name a host' in capsys.readouterr().err
    assert len(seen) == 1


def test_unsupported_and_recursive_fail_before_ssh(t_mod, remote, cfg, monkeypatch, capsys):
    monkeypatch.setitem(remote['execute'].__globals__, 'run_host', lambda *a, **k: pytest.fail('SSH'))
    api = mock_api(t_mod, cfg)
    assert remote['execute'](cfg, SimpleNamespace(rest=['repo', 'cd', 'alpha', 'api']), api) == 2
    monkeypatch.setenv('T_REMOTE_EXEC_DEPTH', str(os.getpid()))
    assert remote['execute'](cfg, SimpleNamespace(rest=['repo', 'list']), api) == 2
    assert 'nested remote' in capsys.readouterr().err


@pytest.mark.parametrize('machine', [False, True])
def test_empty_hosts(t_mod, remote, cfg, capsys, machine):
    cfg.hosts = {}
    assert remote['execute'](cfg, SimpleNamespace(rest=['workflow', 'list'] + (['--json'] if machine else [])), mock_api(t_mod, cfg)) == 0
    out = capsys.readouterr().out
    assert (json.loads(out) == {'results': {}, 'errors': {}}) if machine else 'No remote hosts' in out


@pytest.mark.parametrize('action,host,expected', [('move', 'alpha', '--from'), ('open', 'alpha', '--host'), ('resume', 'alpha', '--host'), ('close', None, 'kill')])
def test_lifecycle_uses_existing_controller_handlers(t_mod, remote, cfg, monkeypatch, action, host, expected):
    calls = []
    monkeypatch.setattr(remote['subprocess'], 'call', lambda args: calls.append(args) or 130)
    argv = ['session', action, *([host] if host else []), 'api', '2']
    assert remote['execute'](cfg, SimpleNamespace(rest=argv), mock_api(t_mod, cfg)) == 130
    assert calls[0][:2] == ['zsh', '-lic']
    assert expected in shlex.split(calls[0][2])


def test_peer_maps_context_and_quotes_payload(t_mod, remote, cfg, monkeypatch):
    calls = []
    monkeypatch.setattr(remote['subprocess'], 'call', lambda argv: calls.append(argv) or 17)
    payload = {'version': 1, 'command': ['session', 'goal'], 'args': ['application', '2', '$(nope); a b'],
               'context': {'identity': 'org/project', 'index': 0, 'option': False}}
    assert remote['peer'](remote['encode'](payload), mock_api(t_mod, cfg)) == 17
    words = shlex.split(calls[0][2])
    assert words[-5:] == ['session', 'goal', 'api', '2', '$(nope); a b']
    assert cfg.repos['api'] in words and 'T_REMOTE_QUERY_LOCAL=1;' not in calls[0][2]


@pytest.mark.parametrize('path,args,local', [(['session','list'], [], True), (['hosts','list'], [], False), (['session','close'], ['api','2'], False), (['session','resume'], ['api','2'], False)])
def test_peer_query_confinement_preserves_host_inventory_and_lifecycle(t_mod, remote, cfg, monkeypatch, path, args, local):
    calls = []
    monkeypatch.setattr(remote['subprocess'], 'call', lambda argv: calls.append(argv) or 0)
    request = {'version': 1, 'command': path, 'args': args, 'context': None}
    assert remote['peer'](remote['encode'](request), mock_api(t_mod, cfg)) == 0
    assert ('T_REMOTE_QUERY_LOCAL=1' in calls[0][2]) == local
    if path[-1] == 'close':
        assert 'REMOTE_HOSTS=();' in calls[0][2]
    if path[-1] == 'resume':
        assert '_T_RESUME_REQUIRE_PICKER=1 _t_resume' in calls[0][2]


@pytest.mark.parametrize('value', ['invalid', {}, {'version':2,'command':[],'args':[]},
    {'version':1,'command':['remote','session','list'],'args':[]},
    {'version':1,'command':['repo','cd'],'args':['api']},
    {'version':1,'command':['repo','list'],'args':[],'context':{'identity':'missing'}},
    {'version':1,'command':['repo','list'],'args':[],'context':{'identity':'org/project','index':99}},
    {'version':1,'command':['repo','list'],'args':[],'context':'bad'}])
def test_invalid_peer_request_never_executes(t_mod, remote, cfg, monkeypatch, value):
    monkeypatch.setattr(remote['subprocess'], 'call', lambda args: pytest.fail('execute'))
    encoded = value if value == 'invalid' else remote['encode'](value)
    assert remote['peer'](encoded, mock_api(t_mod, cfg)) == 2


def test_transport_quotes_and_checks_protocol(remote, monkeypatch):
    calls = []
    monkeypatch.setattr(remote['subprocess'], 'run', lambda argv, **kw: calls.append((argv, kw)) or SimpleNamespace(returncode=0, stdout='rows', stderr=''))
    assert remote['run_host']('a', 'user@a', ('repo', 'list'), [], None, query=True, terminal=False) == ('a', 0, 'rows', '')
    argv, kw = calls[0]
    assert argv[:5] == ['ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=3']
    worker = shlex.split(argv[-1])
    assert worker[:2] == ['zsh','-lic'] and '__remote-protocol' in worker[2]
    assert kw['stdin'] == remote['subprocess'].DEVNULL and kw['timeout'] == 30
    monkeypatch.setattr(remote['subprocess'], 'run', lambda *a, **k: (_ for _ in ()).throw(remote['subprocess'].TimeoutExpired('ssh', 30)))
    assert remote['run_host']('a', 'a', ('repo','list'), [], None, query=True, terminal=False)[1] == 124


def test_help_never_loads_config_or_connects(t_mod, monkeypatch, capsys):
    monkeypatch.setattr(t_mod, 'Config', lambda: pytest.fail('config'))
    for argv in (['remote','session','ls','--help'], ['help','remote','runners','runs','list'], ['remote','workflow','--help']):
        assert t_mod.main(argv) == 0
        assert 'remote' in capsys.readouterr().out


@pytest.mark.parametrize('command', ['t remote move alpha api 2 --to beta --detach', 't remote move alpha api 2 --host beta --detach'])
def test_move_host_first_means_source(zsh, command):
    result = zsh('''
REMOTE_HOSTS=(alpha user@alpha beta user@beta)
_t_beam() { print -r -- "MOVE ${(j:|:)@}"; }
''' + command + '; print rc=$?')
    assert 'rc=0' in result.stdout
    assert 'MOVE api|2|--detach|--from|alpha|--host|beta' in result.stdout


@pytest.mark.parametrize('choice,expected', [('alpha', 7), (None, 130), ('gone', 2), ('interrupt', 130)])
def test_action_host_picker_restores_terminal_before_execution(t_mod, remote, cfg, monkeypatch, choice, expected):
    events = []
    class Rail:
        def raw(self): events.append('raw')
        def restore(self): events.append('restore')
        def pick(self, title, choices, values):
            assert 'which host' in title and values == cfg.hosts
            if choice == 'interrupt': raise KeyboardInterrupt
            return choice
    api = mock_api(t_mod, cfg)
    api['_RailUI'] = Rail
    monkeypatch.setattr(remote['sys'].stdin, 'isatty', lambda: True)
    monkeypatch.setattr(remote['sys'].stdout, 'isatty', lambda: True)
    def run(*a, **k):
        assert events[-1] == 'restore'
        events.append('execute')
        return 7
    monkeypatch.setitem(remote['execute'].__globals__, 'run_host', run)
    assert remote['execute'](cfg, SimpleNamespace(rest=['workflow','use','standard']), api) == expected
    assert events[:2] == ['raw','restore']
    assert ('execute' in events) == (choice == 'alpha')


def test_action_without_hosts_fails_without_opening_ui(t_mod, remote, cfg, capsys):
    cfg.hosts = {}
    assert remote['execute'](cfg, SimpleNamespace(rest=['workflow','use','standard']), mock_api(t_mod, cfg)) == 2
    assert 'no remote hosts' in capsys.readouterr().err


@pytest.mark.parametrize('path,tail,index', [
    (('session','history'), ['application','2','--json'], 0),
    (('session','read'), ['--dump','application','2'], 1),
    (('session','read'), ['--tail','20','application','2','--dump'], 2),
    (('session','goal'), ['--','application','2','application'], 1),
    (('session','read'), ['2','--dump'], None),
    (('issue','list'), ['application','--json'], 0),
    (('issue','status'), ['application','2'], 0),
])
def test_repository_context_argument_boundaries(t_mod, remote, cfg, path, tail, index):
    api = mock_api(t_mod, cfg)
    parsed = remote['child_args'](api, path, tail)
    context = remote['repository_context'](api, cfg, path, tail, parsed)
    assert context['index'] == index and context['identity'] == 'org/project'


def test_transport_action_uses_terminal_and_preserves_errors(remote, monkeypatch):
    calls = []
    monkeypatch.setattr(remote['subprocess'], 'call', lambda argv: calls.append(argv) or 23)
    assert remote['run_host']('alpha','user@alpha',('workflow','use'),['standard'],None,query=False,terminal=True) == 23
    assert calls[0][:3] == ['ssh','-t','user@alpha']
    worker = shlex.split(calls[0][-1])[2]
    assert 'else T_NO_UPDATE_CHECK=1 t workflow' not in worker
    assert 'update t on alpha' in worker
    def missing(*a, **k): raise OSError('missing ssh')
    monkeypatch.setattr(remote['subprocess'], 'call', missing)
    monkeypatch.setattr(remote['subprocess'], 'run', missing)
    assert remote['run_host']('a','a',('repo','list'),[],None,query=True,terminal=False)[1] == 1
    assert remote['run_host']('a','a',('workflow','use'),['x'],None,query=False,terminal=False) == 1


def test_readonly_legacy_fallback_never_executes_a_mutation(remote, monkeypatch):
    commands = []
    monkeypatch.setattr(remote['subprocess'], 'run', lambda argv, **kw: commands.append(shlex.split(argv[-1])[2]) or SimpleNamespace(returncode=0,stdout='',stderr=''))
    remote['run_host']('a','a',('session','list'),[],None,query=True,terminal=False)
    assert 'else T_NO_UPDATE_CHECK=1 t session list' in commands[-1]
    remote['run_host']('a','a',('session','list'),['--remote'],None,query=True,terminal=False)
    assert 'else T_NO_UPDATE_CHECK=1 t session list' not in commands[-1]
    remote['run_host']('a','a',('session','list'),[],{'identity':'org/project'},query=True,terminal=False)
    assert 'else T_NO_UPDATE_CHECK=1 t session list' not in commands[-1]


def test_prefix_host_help_and_protocol_need_no_config(t_mod, monkeypatch, capsys):
    monkeypatch.setattr(t_mod,'Config',lambda: pytest.fail('config'))
    assert t_mod.main(['remote','--host','alpha','session','list','--help']) == 0
    assert 'remote session list' in capsys.readouterr().out
    assert t_mod.main(['__remote-protocol']) == 0
    assert capsys.readouterr().out == '1\n'
    assert t_mod.main(['__remote-completions']) == 0
    assert 'runners runs list' in capsys.readouterr().out


def test_nested_completion_uses_registry_and_child_flags(zsh):
    result = zsh('''
REMOTE_HOSTS=(alpha user@alpha)
_values() { print -r -- "VALUES $*"; }
_describe() { print -r -- "CHOICES ${(P)4}"; }
_message() { print -r -- "MESSAGE $*"; }
local -a words
words=(t remote runners runs ''); CURRENT=5; _t
words=(t remote session ls ''); CURRENT=5; _t
words=(t remote workflow list alpha --); CURRENT=6; _t
words=(t remote session move alpha api 2 --to ''); CURRENT=9; _t
''')
    assert result.returncode == 0, result.stderr
    assert 'CHOICES start list ls status watch logs artifacts cancel cleanup' in result.stdout
    assert 'source host or command argument alpha' in result.stdout
    assert '--json' in result.stdout
    assert 'destination host local alpha' in result.stdout


@pytest.mark.parametrize('flags', ['--to alpha --host beta', '--to=alpha --host=beta'])
def test_remote_move_rejects_conflicting_destinations(zsh, flags):
    result = zsh('_t_beam() { print WRONG; }; t remote move api 2 ' + flags + '; print rc=$?')
    assert 'rc=2' in result.stdout and 'WRONG' not in result.stdout


def test_remote_kill_host_first_with_interleaved_confirmation(zsh, tmp_path):
    stub = tmp_path / 'stubbin/t'
    stub.write_text('#!/bin/sh\nprintf "ARG:%s\\n" "$@"\n')
    stub.chmod(0o755)
    result = zsh('''
REMOTE_HOSTS=(alpha user@alpha)
t remote kill --yes alpha api 2; print rc=$?
''')
    assert result.stdout.splitlines() == [
        'ARG:remote', 'ARG:--host', 'ARG:alpha', 'ARG:session', 'ARG:close',
        'ARG:api', 'ARG:2', 'ARG:--yes', 'rc=0']
    assert 'unknown flag' not in result.stderr and 'expected [repo]' not in result.stderr


def test_host_first_move_asks_for_destination(zsh):
    result = zsh('''
REMOTE_HOSTS=(alpha user@alpha beta user@beta)
_t_remote_host_pick() { print -u2 -r -- "PICK:$2:$3"; print local; }
_t_beam() { print -r -- "MOVE ${(j:|:)@}"; }
t remote move alpha api 2; print rc=$?
''')
    assert 'PICK:move:alpha' in result.stderr
    assert 'MOVE api|2|--from|alpha|--host|local' in result.stdout
    assert 'rc=0' in result.stdout


def test_remote_query_shell_does_not_scan_another_host(zsh):
    result = zsh('''
T_REMOTE_QUERY_LOCAL=1
REMOTE_HOSTS=(alpha user@alpha)
_dev_session_rows() { printf 'id\\t/code/api\\tapi-2\\tdetached\\tactive\\ttask\\tcodex\\n'; }
_dev_remote_scan_with_timeout() { print -u2 WRONG_SCAN; return 1; }
_dev_rows_all; print rc=$?
''')
    assert 'local\tid\t' in result.stdout and 'rc=0' in result.stdout
    assert 'WRONG_SCAN' not in result.stderr


def test_remote_close_shell_never_falls_through_to_another_host(zsh):
    result = zsh('''
T_REMOTE_ACTION_LOCAL=1
REMOTE_HOSTS=(alpha user@alpha)
_dev_remote_resolve() { print -u2 WRONG_SCAN; print 'alpha api 2'; }
_dev_remote_kill() { print -u2 WRONG_KILL; }
_dev_kill api 2 1; print rc=$?
''')
    assert 'rc=1' in result.stdout
    assert 'WRONG_' not in result.stderr


def test_inherited_remote_marker_does_not_disable_future_agent_commands(t_mod, remote, cfg, monkeypatch):
    monkeypatch.setenv('T_REMOTE_EXEC_DEPTH', 'stale-parent')
    monkeypatch.setitem(remote['execute'].__globals__, 'run_host',
                        lambda host, *a, **k: (host, 0, 'rows', ''))
    assert remote['execute'](cfg, SimpleNamespace(rest=['repo','list','alpha']), mock_api(t_mod, cfg)) == 0
