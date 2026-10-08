"""AgentCore CLI integration uses disposable fake AWS processes, never live accounts."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
ARN = 'arn:aws:bedrock-agentcore:us-west-2:123456789012:runtime/Example-ABCDEFGHIJ'
SID = '00000000-0000-4000-8000-000000000001'

@pytest.fixture
def core():
    spec = importlib.util.spec_from_file_location('t_agentcore_test', ROOT / 'libexec/t_agentcore.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module

@pytest.fixture
def args(tmp_path):
    return SimpleNamespace(action='invoke', runtime_arn=ARN, session_id=SID, qualifier=None,
                           profile=None, timeout=5, dry_run=False, prompt='hello', payload=None,
                           output=str(tmp_path / 'response.json'))

@pytest.fixture
def aws(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    for name in ('HOME', 'XDG_CACHE_HOME', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME'):
        monkeypatch.setenv(name, str(home / name))
    bindir = tmp_path / 'bin'; bindir.mkdir()
    script = bindir / 'aws'
    script.write_text('''#!''' + sys.executable + '''
import json, os, pathlib, sys
a = sys.argv[1:]
assert os.environ['AWS_MAX_ATTEMPTS'] == '1'
assert os.environ['AWS_CLI_AUTO_PROMPT'] == 'off'
assert not any(k.startswith('COV_CORE_') for k in os.environ)
record = {'argv': a}
if a[1] == 'invoke-agent-runtime':
    payload = a[a.index('--payload') + 1]
    assert payload.startswith('fileb://')
    record['payload'] = json.loads(pathlib.Path(payload[8:]).read_bytes())
    pathlib.Path(a[-1]).write_bytes(b'{"reply":"ok"}')
with open(os.environ['AWS_TEST_LOG'], 'a') as f:
    f.write(json.dumps(record) + '\\n')
mode = os.environ.get('AWS_TEST_MODE', '')
if mode == 'error':
    print('PRIVATE_CREDENTIAL', file=sys.stderr)
    sys.exit(1)
if mode == 'badjson':
    print('PRIVATE_CREDENTIAL')
else:
    print(json.dumps({'statusCode': 403 if mode == 'status' else 200,
                     'runtimeSessionId': 'wrong' if mode == 'session' else a[a.index('--runtime-session-id') + 1]}))
''')
    script.chmod(0o755)
    monkeypatch.setenv('PATH', str(bindir) + os.pathsep + os.environ['PATH'])
    log = tmp_path / 'aws.jsonl'
    monkeypatch.setenv('AWS_TEST_LOG', str(log))
    return log


def test_cli_invoke_continue_stop(tmp_path, aws):
    env = {k:v for k,v in os.environ.items() if not k.startswith('COV_CORE_')}
    env['T_NO_UPDATE_CHECK'] = '1'
    for i, action in enumerate(('invoke', 'invoke', 'stop')):
        argv = [sys.executable, str(ROOT / 'bin/t'), 'agentcore', action,
                '--runtime-arn', ARN, '--session-id', SID, '--profile', 'sandbox']
        output = tmp_path / f'reply-{i}.json'
        if action == 'invoke':
            argv += ['--prompt', 'hello; $(touch SHOULD_NOT_EXIST)', '--output', str(output)]
        result = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        assert json.loads(result.stdout)['session_id'] == SID
        if action == 'invoke':
            assert json.loads(output.read_bytes()) == {'reply': 'ok'}
            assert output.stat().st_mode & 0o777 == 0o600
    records = [json.loads(line) for line in aws.read_text().splitlines()]
    assert len(records) == 3
    assert records[0]['payload'] == {'prompt': 'hello; $(touch SHOULD_NOT_EXIST)'}
    assert all('--profile' in r['argv'] for r in records)
    assert records[-1]['argv'][1] == 'stop-runtime-session'
    assert '--client-token' in records[-1]['argv']


def test_execute_success(core, args, aws, capsys):
    args.profile = "sandbox"
    assert core.execute(args) == 0
    assert json.loads(capsys.readouterr().out)['status_code'] == 200
    args.action = 'stop'
    assert core.execute(args) == 0
    assert json.loads(capsys.readouterr().out)['output'] is None


def test_dry_run_no_aws_or_writes(core, args, monkeypatch, capsys):
    args.dry_run = True; args.session_id = None; args.runtime_arn = None
    monkeypatch.setenv('T_AGENTCORE_RUNTIME_ARN', ARN)
    monkeypatch.setattr(core.shutil, 'which', lambda _: None)
    assert core.execute(args) == 0
    report = json.loads(capsys.readouterr().out)
    assert report['region'] == 'us-west-2' and len(report['session_id']) == 36
    assert not Path(args.output).exists()
    assert 'hello' not in str(report)


@pytest.mark.parametrize('field,value', [('runtime_arn','invalid'), ('session_id','short'),
    ('session_id','x'*257), ('qualifier','bad/name'), ('timeout',0), ('timeout',28801)])
def test_config_invalid(core, args, field, value):
    setattr(args, field, value)
    assert core.execute(args) == 2


def test_missing_aws(core, args, monkeypatch):
    monkeypatch.setattr(core.shutil, 'which', lambda _: None)
    assert core.execute(args) == 2
    assert not Path(args.output).exists()


def test_payload_file_and_limits(core, args, tmp_path, monkeypatch):
    path = tmp_path / 'payload.json'; args.prompt = None; args.payload = str(path)
    assert core.execute(args) == 2
    for value in ('', 'not json', '[1]', '{"n":NaN}', '{"n":Infinity}'):
        path.write_text(value)
        with pytest.raises(ValueError): core.payload_bytes(args)
    path.write_bytes(b'{"message":"hi"}')
    assert core.payload_bytes(args) == path.read_bytes()
    monkeypatch.setattr(core, 'MAX_PAYLOAD', 2)
    with pytest.raises(ValueError): core.payload_bytes(args)


@pytest.mark.parametrize('symlink', [False, True])
def test_refuse_existing_output_before_submit(core, args, aws, tmp_path, symlink):
    path = Path(args.output)
    if symlink:
        path.symlink_to(tmp_path / 'missing')
    else:
        path.write_text('keep me')
    assert core.execute(args) == 2
    assert not aws.exists()
    if not symlink: assert path.read_text() == 'keep me'


@pytest.mark.parametrize('mode', ['error', 'badjson', 'status', 'session'])
def test_failed_request_never_retried_or_exposed(core, args, aws, monkeypatch, capsys, mode):
    monkeypatch.setenv('AWS_TEST_MODE', mode)
    assert core.execute(args) == 1
    output = capsys.readouterr()
    assert 'PRIVATE_CREDENTIAL' not in output.out + output.err
    assert len(aws.read_text().splitlines()) == 1
    assert Path(args.output).read_bytes() == b''


def test_session_lock_prevents_concurrent_invoke_but_allows_stop(core, args, aws):
    with core.session_lock(core.configure(args)):
        assert core.execute(args) == 2
        assert not aws.exists()
        args.action = 'stop'
        assert core.execute(args) == 0
    args.action = 'invoke'
    assert core.execute(args) == 0


@pytest.mark.parametrize('error', [OSError('private path'), subprocess.TimeoutExpired('aws', 1), KeyboardInterrupt()])
def test_transport_errors(core, args, aws, monkeypatch, capsys, error):
    def fail(*a, **kw): raise error
    monkeypatch.setattr(core.subprocess, 'run', fail)
    assert core.execute(args) == (130 if isinstance(error, KeyboardInterrupt) else 1)
    assert 'private path' not in capsys.readouterr().err


def test_local_io_failure(core, args, aws, tmp_path, capsys):
    args.output = str(tmp_path / 'absent' / 'out')
    assert core.execute(args) == 1
    assert not aws.exists()
    assert str(tmp_path) not in capsys.readouterr().err


@pytest.mark.parametrize('metadata', [[], {}, {'statusCode':200}, {'statusCode':'200'}, None])
def test_invalid_metadata(core, args, monkeypatch, metadata):
    monkeypatch.setattr(core.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout=json.dumps(metadata)))
    with pytest.raises(RuntimeError): core.call_aws(['aws'], core.configure(args), 1)


@pytest.fixture
def runtime_http_server():
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    seen = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *unused): pass
        def do_POST(self):
            # Only the fixture's expected session is accepted. Never reflect input
            # into response headers, including folded or duplicate field values.
            if self.headers.get_all('X-Amzn-Bedrock-AgentCore-Runtime-Session-Id') != [SID]:
                self.send_response(400)
                self.send_header('Content-Length', '0')
                self.end_headers()
                return
            body = self.rfile.read(int(self.headers['Content-Length']))
            seen.append((self.path, dict(self.headers), body))
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('X-Amzn-Bedrock-AgentCore-Runtime-Session-Id', SID)
            response = b'{"reply":"local-fixture"}' if '/invocations?' in self.path else b'{}'
            self.send_header('Content-Length', str(len(response)))
            self.end_headers()
            self.wfile.write(response)
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
    try:
        yield server, seen
    finally:
        server.shutdown(); server.server_close(); thread.join(timeout=5)


@pytest.mark.parametrize('session_headers', [
    b'',
    b'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: unexpected\r\n',
    b'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: ' + SID.encode() + b'\r\n\tX-Injected: yes\r\n',
    (b'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id: ' + SID.encode() + b'\r\n') * 2,
])
def test_runtime_fixture_rejects_untrusted_session_headers(runtime_http_server, session_headers):
    import socket
    server, seen = runtime_http_server
    with socket.create_connection(('127.0.0.1', server.server_port), timeout=3) as connection:
        connection.sendall(b'POST /runtimes/test/invocations?qualifier=DEFAULT HTTP/1.0\r\n'
                           b'Host: localhost\r\n' + session_headers + b'Content-Length: 0\r\n\r\n')
        chunks = []
        while chunk := connection.recv(4096):
            chunks.append(chunk)
    response = b''.join(chunks)
    assert response.startswith(b'HTTP/1.0 400 ')
    assert b'X-Injected' not in response
    assert b'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id:' not in response
    assert not seen


def test_runtime_fixture_returns_only_expected_session(runtime_http_server):
    from http.client import HTTPConnection
    server, seen = runtime_http_server
    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
    try:
        connection.request('POST', '/runtimes/test/invocations?qualifier=DEFAULT', body=b'{}',
                           headers={'X-Amzn-Bedrock-AgentCore-Runtime-Session-Id': SID})
        response = connection.getresponse()
        assert response.status == 200
        assert response.getheader('X-Amzn-Bedrock-AgentCore-Runtime-Session-Id') == SID
        assert json.loads(response.read()) == {'reply': 'local-fixture'}
        assert len(seen) == 1
    finally:
        connection.close()


def test_real_aws_cli_against_local_http_server(core, args, tmp_path, monkeypatch, runtime_http_server):
    """Exercise AWS's real argument parser, SigV4 transport and streaming outfile.

    No real account: static fake credentials, empty config and loopback-only endpoint.
    Linux CI requires this check; elsewhere it skips if AWS CLI v2 is unavailable.
    """
    from urllib.parse import unquote, urlsplit, parse_qs
    executable = shutil.which('aws')
    if not executable:
        if os.environ.get('T_REQUIRE_AWS_CONTRACT') == '1':
            pytest.fail('AWS CLI v2 required for the AgentCore transport contract test')
        pytest.skip('AWS CLI v2 not installed')
    server, seen = runtime_http_server
    # Strip inherited AWS endpoint/profile/credential settings before injecting test data.
    for key in tuple(os.environ):
        if key.startswith('AWS_'): monkeypatch.delenv(key)
    home = tmp_path / 'aws-home'; home.mkdir()
    config = home / 'config'; config.write_text('')
    monkeypatch.setenv('HOME', str(home))
    monkeypatch.setenv('XDG_CACHE_HOME', str(home / '.cache'))
    for key in ('AWS_CONFIG_FILE', 'AWS_SHARED_CREDENTIALS_FILE'):
        monkeypatch.setenv(key, str(config))
    monkeypatch.setenv('AWS_ACCESS_KEY_ID', 'testing')
    monkeypatch.setenv('AWS_SECRET_ACCESS_KEY', 'testing')
    monkeypatch.setenv('AWS_EC2_METADATA_DISABLED', 'true')
    monkeypatch.setenv('AWS_ENDPOINT_URL', f'http://127.0.0.1:{server.server_port}')
    monkeypatch.setenv('NO_PROXY', '127.0.0.1,localhost')
    assert core.execute(args) == 0
    assert json.loads(Path(args.output).read_bytes()) == {'reply': 'local-fixture'}
    args.action = 'stop'
    assert core.execute(args) == 0
    assert len(seen) == 2
    for path, headers, body in seen:
        assert unquote(urlsplit(path).path).startswith('/runtimes/' + ARN + '/')
        assert parse_qs(urlsplit(path).query) == {'qualifier': ['DEFAULT']}
        lower = {k.lower(): v for k,v in headers.items()}
        assert lower['x-amzn-bedrock-agentcore-runtime-session-id'] == SID
        assert lower['authorization'].startswith('AWS4-HMAC-SHA256 ')
    assert json.loads(seen[0][2]) == {'prompt': 'hello'}
    assert len(json.loads(seen[1][2])['clientToken']) == 36
