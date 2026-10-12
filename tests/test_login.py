"""Login planning and isolated CLI flows; no real accounts or vendor sign-ins."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


def jwt(payload):
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip('=')
    return 'fixture.' + encoded + '.fixture'


def put(root, name, content):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    return path


@pytest.mark.parametrize('agent,rc,text,state', [
    ('claude', 0, '{"loggedIn":true}', 'logged-in'),
    ('claude', 1, '{"loggedIn":false}', 'logged-out'),
    ('claude', 1, '{"loggedIn":true}', 'error'),
    ('claude', 0, '[]', 'error'),
    ('claude', 0, 'garbage', 'error'),
    ('codex', 0, 'Logged in using ChatGPT', 'logged-in'),
    ('codex', 1, 'Not logged in', 'logged-out'),
    ('cursor', 0, '\033[32mLogin successful!\033[0m', 'logged-in'),
    ('cursor', 1, 'Not authenticated', 'logged-out'),
    ('codex', 124, 'Not logged in', 'error'),
    ('codex', 127, 'missing binary', 'error'),
    ('codex', 1, 'network failed', 'error'),
])
def test_status_classification(t_mod, agent, rc, text, state):
    assert t_mod._login_state(agent, rc, text) == state


@pytest.mark.parametrize('value', [None, True, '123', -1, 0, float('nan'), float('inf'), 10**400])
def test_bad_timestamps_are_unknown(t_mod, value):
    assert t_mod._login_timestamp(value) is None


def test_cached_expiry_and_alternate_locations(t_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(t_mod.sys, 'platform', 'linux')
    claude = put(tmp_path, '.claude/.credentials.json', json.dumps({'claudeAiOauth': {'expiresAt': 2000000}}))
    codex = put(tmp_path, '.codex/auth.json', json.dumps({'tokens': {'access_token': jwt({'exp': 3000})}}))
    assert t_mod._login_cached_expiry('claude', str(tmp_path), {}) == 2000
    assert t_mod._login_cached_expiry('codex', str(tmp_path), {}) == 3000
    assert t_mod._login_cached_expiry('cursor', str(tmp_path), {}) is None
    env = {'CLAUDE_CONFIG_DIR': str(claude.parent), 'CODEX_HOME': str(codex.parent)}
    assert t_mod._login_cached_expiry('claude', '/unused', env) == 2000
    assert t_mod._login_cached_expiry('codex', '/unused', env) == 3000
    assert t_mod._login_cached_expiry('claude', str(tmp_path), {'ANTHROPIC_API_KEY': 'fixture'}) is None
    assert t_mod._login_cached_expiry('codex', str(tmp_path), {'OPENAI_API_KEY': 'fixture'}) is None
    monkeypatch.setattr(t_mod.sys, 'platform', 'darwin')
    assert t_mod._login_cached_expiry('claude', str(tmp_path), {}) is None


@pytest.mark.parametrize('data', [
    {}, [], {'tokens': None}, {'tokens': {'access_token': 'opaque'}},
    {'tokens': {'access_token': 'bad.!.bad'}},
    {'tokens': {'access_token': jwt([])}},
    {'tokens': {'access_token': jwt({'exp': '123'})}},
    {'OPENAI_API_KEY': 'fixture'}, {'auth_mode': 'apikey'},
])
def test_unknown_or_malformed_auth(t_mod, tmp_path, data):
    put(tmp_path, '.codex/auth.json', json.dumps(data))
    assert t_mod._login_cached_expiry('codex', str(tmp_path), {}) is None


def test_missing_unreadable_and_alternate_storage(t_mod, tmp_path, monkeypatch):
    assert t_mod._login_cached_expiry('codex', str(tmp_path), {}) is None
    auth = put(tmp_path, '.codex/auth.json', '{')
    assert t_mod._login_cached_expiry('codex', str(tmp_path), {}) is None
    auth.write_bytes(b'\xff')
    assert t_mod._login_cached_expiry('codex', str(tmp_path), {}) is None
    auth.write_text(json.dumps({'tokens': {'access_token': jwt({'exp': 3000})}}))
    config = put(tmp_path, '.codex/config.toml', 'cli_auth_credentials_store = "keyring"\n')
    assert t_mod._login_cached_expiry('codex', str(tmp_path), {}) is None
    config.write_text('cli_auth_credentials_store = "file"\n')
    assert t_mod._login_cached_expiry('codex', str(tmp_path), {}) == 3000
    config.write_text('malformed = [')
    assert t_mod._login_cached_expiry('codex', str(tmp_path), {}) is None
    config.write_text('')
    monkeypatch.setitem(sys.modules, 'tomllib', None)
    assert t_mod._login_cached_expiry('codex', str(tmp_path), {}) is None


@pytest.mark.parametrize('state,expiry,force,run,reason', [
    ('missing', None, True, False, 'not installed'),
    ('error', None, False, False, 'status unavailable'),
    ('error', None, True, True, 'explicitly'),
    ('logged-out', None, False, True, 'logged out'),
    ('logged-in', None, False, False, 'expiry unknown'),
    ('logged-in', 100, False, True, 'automatic refresh'),
    ('logged-in', 87400, False, True, 'within'),
    ('logged-in', 87401, False, False, 'beyond'),
])
def test_plan_boundaries(t_mod, state, expiry, force, run, reason):
    result = t_mod._login_decision({'state': state, 'expires': expiry}, 1000, 24, force)
    assert result[0] == run and reason in result[1]


@pytest.mark.parametrize('value', ['nan', 'inf', '-1', '8761', 'text'])
def test_invalid_hours(t_mod, value):
    with pytest.raises(t_mod.argparse.ArgumentTypeError):
        t_mod._login_hours(value)


def test_parser_defaults_and_alias(t_mod):
    for argv in (['login'], ['agent', 'login']):
        args = t_mod.build_parser().parse_args(argv)
        assert args.verb == 'login' and args.within_hours == 24 and not args.agents
    assert t_mod._login_hours('0') == 0
    assert t_mod._login_hours('0.5') == .5


def test_parser_rejects_unknown_agents(t_mod):
    with pytest.raises(SystemExit) as exc:
        t_mod.build_parser().parse_args(['login', 'unknown'])
    assert exc.value.code == 2


def test_login_checklist_discovers_and_selects_authenticated_agents(t_mod):
    probed = {
        'claude': {'state': 'logged-in', 'expires': None, 'bin': '/fixture/claude'},
        'codex': {'state': 'logged-out', 'bin': '/fixture/codex'},
        'cursor': {'state': 'missing', 'bin': None},
    }
    items = t_mod._login_items(probed, 1000, 24)
    assert [it['checked'] for it in items[1:]] == [False, True, False]
    assert items[-1]['t'] == 'locked'
    items[1]['checked'], items[2]['checked'] = True, False
    steps = t_mod._login_plan(items, probed, False)
    assert [s['cmd'] for s in steps] == [['/fixture/claude', 'auth', 'login']]
    probed['cursor'] = {'state': 'logged-out', 'bin': '/fixture/cursor-agent'}
    steps = t_mod._login_plan(t_mod._login_items(probed, 1000, 24), probed, True)
    assert steps[-1]['cmd'] == ['env', 'NO_OPEN_BROWSER=1', '/fixture/cursor-agent', 'login']


def test_empty_agent_list_on_system_python(tmp_path):
    python = Path('/usr/bin/python3')
    if not python.exists():
        pytest.skip('no system Python')
    env = cli_sandbox(tmp_path)
    result = subprocess.run([str(python), str(ROOT / 'bin/t'), 'login', '--dry-run'],
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert 'claude: logged out' in result.stdout


def test_probe_paths_and_timeout(t_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(t_mod, 'HOME', str(tmp_path))
    monkeypatch.setattr(t_mod.shutil, 'which', lambda _: None)
    assert t_mod._login_probe('claude')['state'] == 'missing'
    exe = put(tmp_path, '.local/bin/claude', '')
    calls = []
    def run(cmd, **kw):
        calls.append((cmd, kw))
        return SimpleNamespace(returncode=124, stdout='', stderr='private diagnostic')
    monkeypatch.setattr(t_mod, '_run', run)
    assert t_mod._login_probe('claude')['state'] == 'error'
    assert calls == [([str(exe), 'auth', 'status'], {'timeout': 30})]
    monkeypatch.setattr(t_mod, '_run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout='{"loggedIn":true,"authMethod":"claude.ai"}', stderr=''))
    monkeypatch.setattr(t_mod, '_login_cached_expiry', lambda *a: 123)
    assert t_mod._login_probe('claude')['expires'] == 123


def cli_sandbox(tmp_path):
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    for agent in ('claude', 'codex', 'cursor-agent'):
        exe = bindir / agent
        exe.write_text('''#!/usr/bin/env python3
import json, os, pathlib, sys
name = pathlib.Path(sys.argv[0]).name
root = pathlib.Path(os.environ['HOME'])
with (root / 'calls').open('a') as f: f.write(name + ' ' + ' '.join(sys.argv[1:]) + '\\n')
if 'status' in sys.argv:
    if name == 'codex' and os.environ.get('FAKE_BROKEN'):
        print('PRIVATE DIAGNOSTIC', file=sys.stderr); sys.exit(1)
    ok = (root / (name + '-logged-in')).exists()
    print(json.dumps({'loggedIn': ok}) if name == 'claude' else ('Logged in using ChatGPT' if ok else 'Not logged in'))
    sys.exit(0 if ok else 1)
if os.environ.get('FAKE_FAIL') == name: sys.exit(3)
if not os.environ.get('FAKE_NO_AUTH'): (root / (name + '-logged-in')).touch()
''')
        exe.chmod(0o755)
    # Minimal inherited environment: never pass real agent credentials to fixtures.
    return {'HOME': str(tmp_path), 'PATH': str(bindir) + os.pathsep + os.environ['PATH'],
            'XDG_CONFIG_HOME': str(tmp_path / '.config'), 'T_NO_UPDATE_CHECK': '1'}


def invoke(env, *args):
    return subprocess.run([sys.executable, str(ROOT / 'bin/t'), *args], env=env,
                          input='', capture_output=True, text=True, timeout=20)


def test_cli_dry_run_ignore_and_no_credential_output(tmp_path):
    env = cli_sandbox(tmp_path)
    env['FAKE_BROKEN'] = '1'
    result = invoke(env, 'login', '--ignore', 'codex', '--dry-run')
    assert result.returncode == 0, result.stderr
    calls = (tmp_path / 'calls').read_text()
    assert 'codex' not in calls and 'login' not in calls
    assert 'ignored for this invocation' in result.stdout
    result = invoke(env, 'agent', 'login', '--status')
    assert result.returncode == 1 and 'status unavailable' in result.stdout
    assert 'PRIVATE' not in result.stdout + result.stderr


def test_cli_login_headless_failure_continues_and_rechecks(tmp_path):
    env = cli_sandbox(tmp_path)
    env['FAKE_FAIL'] = 'claude'
    env['SSH_CONNECTION'] = 'fixture'
    result = invoke(env, 'login', '-y')
    assert result.returncode == 1, result.stderr
    calls = (tmp_path / 'calls').read_text()
    assert 'claude auth login' in calls
    assert 'codex login --device-auth' in calls and 'cursor-agent login' in calls
    assert calls.count('codex login status') == 2
    assert 'codex: login completed' in result.stdout
    assert not (tmp_path / 'claude-logged-in').exists()


def test_cli_force_and_confirmation_guards(tmp_path):
    env = cli_sandbox(tmp_path)
    result = invoke(env, 'login', '--force')
    assert result.returncode == 2 and not (tmp_path / 'calls').exists()
    result = invoke(env, 'login', 'codex')
    assert result.returncode == 2
    assert (tmp_path / 'calls').read_text() == 'codex login status\n'
    (tmp_path / 'codex-logged-in').touch()
    result = invoke(env, 'login', 'codex', '-y')
    assert result.returncode == 0 and 'expiry unknown' in result.stdout
    result = invoke(env, 'login', 'codex', 'codex', '--force', '-y')
    assert result.returncode == 0
    assert (tmp_path / 'calls').read_text().splitlines().count('codex login') == 1


def test_cli_does_not_trust_successful_exit(tmp_path):
    env = cli_sandbox(tmp_path)
    env['FAKE_NO_AUTH'] = '1'
    result = invoke(env, 'login', 'codex', '-y')
    assert result.returncode == 1 and 'status is not logged in' in result.stderr


def test_cli_cached_expiry_selects_login_without_writing_auth(tmp_path):
    env = cli_sandbox(tmp_path)
    (tmp_path / 'codex-logged-in').touch()
    auth = put(tmp_path, '.codex/auth.json', json.dumps({'tokens': {'access_token': jwt({'exp': 10})}}))
    before = auth.read_bytes()
    result = invoke(env, 'login', 'codex', '--dry-run')
    assert result.returncode == 0 and 'cached access token expired' in result.stdout
    assert auth.read_bytes() == before
    assert jwt({'exp': 10}) not in result.stdout


@pytest.mark.parametrize('agent,output,expected', [
    ('codex', 'Logged in using ChatGPT', True),
    ('codex', 'Logged in using an API key', False),
    ('claude', '{"authMethod":"claude.ai"}', True),
    ('claude', '{"authMethod":"api_key"}', False),
    ('claude', '[]', False),
    ('claude', 'bad', False),
    ('cursor', 'Logged in', False),
])
def test_cache_must_match_reported_auth_source(t_mod, agent, output, expected):
    assert t_mod._login_cache_matches(agent, output) == expected


def test_declining_logins_preserves_probe_failure(t_mod, monkeypatch):
    args = t_mod.build_parser().parse_args(['login', 'claude', 'codex'])
    monkeypatch.setattr(t_mod, '_login_probe', lambda agent: {
        'state': 'error' if agent == 'codex' else 'logged-out', 'bin': '/fixture/' + agent})
    monkeypatch.setattr(t_mod.sys, 'stdin', SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(t_mod.sys.stdout, 'isatty', lambda: True)
    monkeypatch.setattr(t_mod, '_install_wizard', lambda *a, **kw: (_ for _ in ()).throw(OSError('no raw terminal')))
    monkeypatch.setattr('builtins.input', lambda _: 'n')
    assert t_mod.cmd_login(None, args) == 1


@pytest.mark.parametrize('answers,expected,status', [
    (['n', 'y', 'n'], [['/fixture/codex', 'login']], 0),
    (['n', 'n', 'n'], [], 0),
    (['n', 'y', 'q'], [], 130),
    (['n', 'y', None], [], 130),
])
def test_login_prompt_fallback_runs_selection_directly(t_mod, monkeypatch, answers, expected, status):
    args = t_mod.build_parser().parse_args(['login'])
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(t_mod.sys.stdout, 'isatty', lambda: True)
    def unavailable(*args, **kwargs):
        raise OSError('no raw terminal')
    monkeypatch.setattr(t_mod, '_install_wizard', unavailable)
    monkeypatch.setattr(t_mod, '_login_probe', lambda agent: {
        'state': 'logged-in', 'bin': '/fixture/' + agent, 'expires': None})
    answers = iter(answers)
    def answer(_):
        value = next(answers)
        if value is None:
            raise EOFError
        return value
    monkeypatch.setattr('builtins.input', answer)
    calls = []
    def run(cmd):
        calls.append(cmd)
        return SimpleNamespace(returncode=0)
    monkeypatch.setattr(t_mod, '_login_run', run)
    assert t_mod.cmd_login(None, args) == status
    assert calls == expected


@pytest.mark.parametrize('answer,expected', [('', True), ('y', True), ('YES', True),
                                          ('n', False), ('q', False), ('invalid', False)])
def test_command_confirmation_defaults_to_enter(t_mod, monkeypatch, answer, expected):
    monkeypatch.setattr('builtins.input', lambda _: answer)
    assert t_mod._confirm_run('Run?') is expected


def test_command_confirmation_eof_and_interrupt_never_run(t_mod, monkeypatch):
    def eof(_):
        raise EOFError
    monkeypatch.setattr('builtins.input', eof)
    assert not t_mod._confirm_run('Run?')
    def interrupt(_):
        raise KeyboardInterrupt
    monkeypatch.setattr('builtins.input', interrupt)
    with pytest.raises(KeyboardInterrupt):
        t_mod._confirm_run('Run?')
    monkeypatch.setattr('builtins.input', lambda _: '')
    assert not t_mod._confirm('Change configuration?')
