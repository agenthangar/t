"""Exercise model gateway and orchestration without credentials or external inference."""
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import http.client
import json
from pathlib import Path
import threading
import time
from types import SimpleNamespace
import urllib.error
import urllib.request

import pytest
from conftest import load_script
from test_eval import evaluation, ROOT


def test_gateway_revocation_stops_inflight_and_queued_inference(evaluation):
    m, _ = evaluation
    p = load_script(ROOT / 'libexec/t_eval_proxy.py', 'eval_proxy_cancel')
    entered, release = threading.Event(), threading.Event()
    calls = []
    class SlowEndpoint(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            calls.append(True)
            self.send_response(200)
            self.send_header('Connection', 'close')
            self.end_headers()
            self.wfile.flush()
            entered.set()
            release.wait(10)
    upstream = ThreadingHTTPServer(('127.0.0.1', 0), SlowEndpoint)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    config = {**m.defaults(), 'modes': ['local-only']}
    journal = m.Journal(m.create_run(config))
    local = (f'http://127.0.0.1:{upstream.server_port}', 'fixed-model', {'revision': 'fixed'})
    try:
        with p.Gateway(config, journal, local) as gateway:
            lease = gateway.lease('cancel-test', 'local-only')
            url = f'http://127.0.0.1:{gateway.server.server_port}/{lease["token"]}/local/v1/responses'
            def request():
                try:
                    with urllib.request.urlopen(urllib.request.Request(url, data=b'{"model":"fixed-model"}'), timeout=5) as response:
                        response.read()
                except (OSError, http.client.HTTPException):
                    pass  # Revoked requests may receive EOF or an invalid-capability response.
            first = threading.Thread(target=request); first.start()
            assert entered.wait(3)
            queued = threading.Thread(target=request); queued.start()
            gateway.close_lease(lease['token'])
            assert len(gateway.records) == 1  # Usage is settled before final trial totals.
            first.join(3); queued.join(3)
            assert not first.is_alive() and not queued.is_alive()
            deadline = time.monotonic() + 3
            while gateway.connections and time.monotonic() < deadline:
                threading.Event().wait(.01)
            assert not gateway.connections
            assert calls == [True]
            assert gateway.totals('cancel-test')['usage']['local']['requests'] == 0
    finally:
        release.set()
        upstream.shutdown(); upstream.server_close()


def test_gateway_usage_pricing_auth(evaluation, monkeypatch, tmp_path):
    m, _ = evaluation
    p = load_script(ROOT / 'libexec/t_eval_proxy.py', 'eval_proxy_test')
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    with pytest.raises(ValueError): p.frontier_connection()
    home = Path(p.os.environ['CODEX_HOME']); home.mkdir(parents=True)
    for auth in ({}, {'tokens': {'access_token': 'fixture', 'account_id': 'account'}}, {'OPENAI_API_KEY': 'fixture'}):
        (home / 'auth.json').write_text(json.dumps(auth))
        if not auth:
            with pytest.raises(ValueError): p.frontier_connection()
        else: assert p.frontier_connection()[2] in ('api-estimate', 'api-equivalent')
    monkeypatch.setenv('OPENAI_API_KEY', 'fixture')
    assert p.frontier_connection()[0].endswith('/v1')
    assert p.usage_record({}) is None
    assert p.usage_record({'usage': {'input_tokens': 1, 'output_tokens': 1, 'input_tokens_details': {'cached_tokens': 2}}}) is None
    usage = p.usage_record({'response': {'usage': {'input_tokens': 100, 'output_tokens': 50,
        'input_tokens_details': {'cached_tokens': 20}, 'output_tokens_details': {'reasoning_tokens': 10}}}})
    rates = dict(input_per_million=2, cached_input_per_million=.2, output_per_million=10)
    assert p.price(usage, rates) == pytest.approx(.000664)
    assert p.price(None, rates) is None and p.price(usage, None) is None
    config = {**m.defaults(), 'modes': ['local-only']}
    journal = m.Journal(m.create_run(config))
    seen = []
    class Endpoint(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_POST(self):
            seen.append(json.loads(self.rfile.read(int(self.headers['Content-Length']))))
            self.send_response(200); self.send_header('Content-Type', 'text/event-stream'); self.end_headers()
            self.wfile.write(b'data: {"response":{"usage":{"input_tokens":100,"output_tokens":50}}}\n\ndata: [DONE]\n\n')
    upstream = ThreadingHTTPServer(('127.0.0.1', 0), Endpoint)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True); thread.start()
    local = (f'http://127.0.0.1:{upstream.server_port}', 'exact-alias', {'revision': 'fixed-revision'})
    def request(gateway, lease, suffix, data=None):
        url = f'http://127.0.0.1:{gateway.server.server_port}/{lease["token"]}/' + suffix
        body = json.dumps(data).encode() if data is not None else None
        with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=5) as response:
            return response.read()
    try:
        with p.Gateway(config, journal, local) as gateway:
            lease = gateway.lease('trial', 'local-only')
            frontier = gateway.lease('frontier-trial', 'frontier-only')
            assert 'local' not in frontier
            with pytest.raises(urllib.error.HTTPError) as blocked:
                request(gateway, frontier, 'local/identity')
            assert blocked.value.code == 403
            with pytest.raises(urllib.error.HTTPError) as blocked:
                request(gateway, frontier, 'local/v1/responses', {'model': 'exact-alias'})
            assert blocked.value.code == 403
            assert json.loads(request(gateway, lease, 'local/identity'))['revision'] == 'fixed-revision'
            assert b'exact-alias' in request(gateway, lease, 'local/v1/models')
            assert b'50' in request(gateway, lease, 'local/v1/responses', {'model': 'exact-alias'})
            for suffix, data in [('frontier/v1/responses', {'model': config['frontier_model']}),
                                 ('local/v1/responses', {'model': 'wrong'}),
                                 ('local/v1/responses', {'model': 'exact-alias', 'tools': [{'type': 'web_search'}]}),
                                 ('control/unknown', {}), ('local/unknown', None)]:
                with pytest.raises(urllib.error.HTTPError): request(gateway, lease, suffix, data)
            assert json.loads(request(gateway, lease, 'control/checkpoint', {'kind': 'tool_completed'})) == {'saved': True}
            with pytest.raises(urllib.error.HTTPError): request(gateway, lease, 'control/checkpoint', {'kind': 'arbitrary'})
            # The body has finished streaming; the recorded usage is journaled synchronously.
            for _ in range(100):
                if gateway.totals('trial')['usage']['local']['requests']: break
                threading.Event().wait(.01)
            assert gateway.totals('trial')['usage']['local']['output_tokens'] == 50
            assert gateway.totals('trial')['cost_usd'] == 0
            assert not gateway.budget_exhausted()
            gateway.config['spend_limit_usd'] = 0
            assert gateway.budget_exhausted()
            gateway.close_lease(lease['token'])
            with pytest.raises(urllib.error.HTTPError): request(gateway, lease, 'local/v1/responses', {})
        restored = p.Gateway(config, journal, local)
        assert restored.totals('trial')['usage']['local']['output_tokens'] == 50
        restored.config['spend_limit_usd'] = 1
        assert not restored.budget_exhausted()
    finally:
        upstream.shutdown(); upstream.server_close(); thread.join()
    assert seen == [{'model': 'exact-alias'}]


def test_task_selection_checksums_and_image_pin(evaluation, tmp_path, monkeypatch):
    m, _ = evaluation
    e = load_script(ROOT / 'libexec/t_eval_engine.py', 'eval_engine_test')
    monkeypatch.setattr(e, 'core', lambda: vars(m))
    tasks = tmp_path / 'benchmark/v2/tasks'; tasks.mkdir(parents=True)
    for repository in range(11):
        for commit in (1, 2):
            task = tasks / f'instance_repo{repository}-{commit:040x}-vtest'
            task.mkdir(); (task / 'task.toml').write_text('[environment]\ndocker_image="example/image:fixed"\n')
            (task / 'environment').mkdir()
    root = tasks.parent.parent
    assert len(e.selected_tasks(root, 'standard')) == 11
    assert len(e.selected_tasks(root, 'quick')) == 3
    assert e.selected_tasks(root, 'quick') == e.selected_tasks(root, 'quick')
    with pytest.raises(ValueError): e.selected_tasks(root, 'full')
    (root / 'v2/hard51_ids.txt').write_text('missing')
    with pytest.raises(ValueError): e.selected_tasks(root, 'advanced')
    checksum = root / 'v2/SHA256SUMS'
    data = root / 'v2/data'; data.write_text('pinned')
    checksum.write_text(e.hashlib.sha256(b'pinned').hexdigest() + '  data\n')
    e.verify_checksums(root)
    data.write_text('changed')
    with pytest.raises(ValueError, match='checksum mismatch'): e.verify_checksums(root)
    checksum.write_text('a  ../../outside\n')
    with pytest.raises(ValueError, match='unsafe'): e.verify_checksums(root)
    monkeypatch.setattr(e, 'run', lambda *a, **kw: json.dumps([{'Architecture': 'amd64', 'RepoDigests': ['example/image@sha256:abc']}]))
    destination = tmp_path / 'pinned'
    task = next(tasks.iterdir())
    assert e.pin_image(task, destination) == 'example/image@sha256:abc'
    assert e.pin_image(task, destination) == 'example/image@sha256:abc'
    (destination / 'task.toml').write_text('changed')
    with pytest.raises(ValueError, match='digest changed'): e.pin_image(task, destination)


def test_engine_resume_preserves_completed_attempts(evaluation, tmp_path, monkeypatch):
    m, _ = evaluation
    e = load_script(ROOT / 'libexec/t_eval_engine.py', 'eval_engine_resume')
    monkeypatch.setattr(e, 'core', lambda: vars(m))
    config = {**m.defaults(), 'profile': 'quick', 'modes': ['frontier-only']}
    j = m.Journal(m.create_run(config))
    tasks = [tmp_path / name for name in ('a', 'b', 'c')]
    monkeypatch.setattr(e, 'prepare_runtime', lambda *a: ('1.0.0', 8))
    monkeypatch.setattr(e, 'native_codex', lambda *a: '/fixture/native-codex')
    monkeypatch.setattr(e, 'harbor_python', lambda *a: Path('/fake/python'))
    monkeypatch.setattr(e, 'prepare_benchmark', lambda *a: (tmp_path, tasks))
    monkeypatch.setattr(e, 'pin_image', lambda *a: 'sha256:fixed')
    calls = []
    def child(python, job, directory, journal):
        calls.append(job['task'])
        assert journal.state['trials'][-1]['status'] == 'running'
        return {'status': 'completed', 'resolved': True, 'workflow_valid': True}
    monkeypatch.setattr(e, 'child_job', child)
    class Gateway:
        def __init__(self, *a): pass
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def lease(self, trial, mode): return {'token': 'fixture'}
        def close_lease(self, token): pass
        def totals(self, trial): return {'cost_usd': 1, 'usage': {}}
    monkeypatch.setattr(m, 'helper', lambda name: {'Gateway': Gateway})
    e.execute(j)
    assert len(calls) == 3 and j.state['status'] == 'completed'
    e.execute(j)
    assert len(calls) == 3
    j.state['trials'][1].update(status='interrupted', resolved=None)
    e.execute(j)
    assert len(calls) == 3 and j.state['status'] == 'failed'
    e.execute(j, retry_interrupted=True)
    assert len(calls) == 4 and len(j.state['trials']) == 4 and j.state['status'] == 'completed'
    assert m.summarize(m.load_run(j.state['id']))['modes']['frontier-only']['estimated_cost_usd'] == 4
    for limit in (0, 1):
        j.manifest['config']['spend_limit_usd'] = limit
        with pytest.raises(ValueError): e.execute(j)


def test_preparation_blocks_missing_dependencies_and_pins_runtime(evaluation, tmp_path, monkeypatch):
    m, _ = evaluation
    e = load_script(ROOT / 'libexec/t_eval_engine.py', 'eval_preparation_test')
    monkeypatch.setattr(e, 'core', lambda: vars(m))
    j = m.Journal(m.create_run(m.defaults()))
    monkeypatch.setattr(e.shutil, 'which', lambda name: None)
    with pytest.raises(ValueError, match='Colima'): e.prepare_runtime(m.defaults(), j)
    monkeypatch.setattr(e.shutil, 'which', lambda name: None if name == 'codex' else name)
    with pytest.raises(ValueError, match='Codex'): e.prepare_runtime(m.defaults(), j)
    monkeypatch.setattr(e.shutil, 'which', lambda name: name)
    monkeypatch.setattr(e.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0))
    commands = []
    def run(command, **kw):
        commands.append(command)
        if command[:3] == ['docker', 'context', 'ls']: return ''
        if command[:2] == ['docker', 'info']: return json.dumps({'OSType': 'linux', 'MemTotal': 8*1024**3, 'Architecture': 'aarch64'})
        if command[:2] == ['colima', 'ssh']: return 'enabled\ninterpreter /mnt/lima-rosetta/rosetta\n'
        if command[0] == 'codex': return 'codex-cli 0.155.1'
        return ''
    monkeypatch.setattr(e, 'run', run)
    assert e.prepare_runtime(m.defaults(), j) == ('0.155.1', 8)
    assert any(c[:4] == ['colima', 'start', '--profile', 't-eval'] for c in commands)
    assert any('--vz-rosetta' in c and '--binfmt=false' in c for c in commands)
    assert e.os.environ['COLIMA_HOME'] == str(m.paths()['cache'] / 'colima')
    assert j.state['runtime']['emulation'] == 'rosetta'
    monkeypatch.setattr(e, 'run', lambda c, **kw: 'disabled\n' if c[:2] == ['colima', 'ssh'] else run(c, **kw))
    with pytest.raises(ValueError, match='Rosetta is not active') as missing_rosetta:
        e.prepare_runtime(m.defaults(), j)
    assert 'COLIMA_HOME=' in str(missing_rosetta.value) and 'DOCKER_CONFIG=' in str(missing_rosetta.value)
    assert 'colima stop --profile t-eval' in str(missing_rosetta.value)
    monkeypatch.setattr(e, 'run', lambda c, **kw: json.dumps({'OSType':'linux', 'MemTotal':16*1024**3, 'Architecture':'x86_64'}) if c[:2] == ['docker', 'info'] else run(c, **kw))
    assert e.prepare_runtime(m.defaults(), j) == ('0.155.1', 16)
    assert j.state['runtime']['emulation'] == 'native'
    for plugin in ('compose', 'buildx'):
        def missing(command, **kw):
            if command == ['docker', plugin, 'version']: raise ValueError('unavailable')
            return run(command, **kw)
        monkeypatch.setattr(e, 'run', missing)
        with pytest.raises(ValueError, match=f'Docker {plugin} is missing'): e.prepare_runtime(m.defaults(), j)
    monkeypatch.setattr(e, 'run', lambda *a, **kw: json.dumps({'OSType': 'windows'}))
    with pytest.raises(ValueError, match='Linux'): e.prepare_runtime(m.defaults(), j)
    monkeypatch.setattr(e, 'run', lambda *a, **kw: json.dumps({'OSType': 'linux', 'MemTotal': 1}))
    with pytest.raises(ValueError, match='GiB'): e.prepare_runtime(m.defaults(), j)
    monkeypatch.setattr(e, 'run', lambda c, **kw: 'unknown' if c[0] == 'codex' else run(c, **kw))
    with pytest.raises(ValueError, match='pin'): e.prepare_runtime(m.defaults(), j)
    monkeypatch.setattr(e, 'run', run)
    monkeypatch.setenv('T_EVAL_PYTHON', '/explicit/python')
    assert str(e.harbor_python(j)) == '/explicit/python'
    monkeypatch.delenv('T_EVAL_PYTHON')
    assert str(e.harbor_python(j)).endswith('harbor-0.25.0/bin/python')
    monkeypatch.setattr(e.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1))
    e.harbor_python(j)
    assert any('pip' in c for c in commands)
    monkeypatch.setattr(e.sys, 'version_info', (3, 11))
    with pytest.raises(ValueError, match='3.12'): e.harbor_python(j)


def test_cached_preparation_identity_and_fit(evaluation, tmp_path, monkeypatch):
    m, _ = evaluation
    e = load_script(ROOT / 'libexec/t_eval_engine.py', 'eval_cache_test')
    monkeypatch.setattr(e, 'core', lambda: vars(m))
    j = m.Journal(m.create_run(m.defaults()))
    root = m.paths()['cache'] / m.BENCHMARK_REVISION
    def run(command, **kwargs):
        if 'checkout' in command:
            (root / 'v2').mkdir(exist_ok=True)
            (root / 'v2/SHA256SUMS').write_text('')
        return m.BENCHMARK_REVISION
    monkeypatch.setattr(e, 'run', run)
    monkeypatch.setattr(e, 'selected_tasks', lambda *a: [tmp_path / 'a'])
    assert e.prepare_benchmark(j)[0] == root
    assert j.state['task_ids'] == ['a']
    e.prepare_benchmark(j)
    j.state['task_ids'] = ['other']
    with pytest.raises(ValueError, match='changed'): e.prepare_benchmark(j)
    monkeypatch.setattr(e, 'run', lambda *a, **kw: 'wrong')
    with pytest.raises(ValueError, match='revision'): e.prepare_benchmark(j)
    row = {'model': m.defaults()['local_model'], 'provider': 'lmstudio', 'ready': True, 'fit': True, 'bytes': 30*1024**3}
    local = {'policy': lambda v:v, 'catalogue': lambda **kw: {'models': [row], 'hardware': {'ram_bytes': 128*1024**3}}}
    monkeypatch.setattr(m, 'helper', lambda name: local)
    assert e.ready_local(m.defaults(), 8)[1]['model'] == row['model']
    row['ready'] = False
    with pytest.raises(ValueError, match='ready'): e.ready_local(m.defaults(), 8)
    row['ready'] = True; row['bytes'] = 200*1024**3
    with pytest.raises(ValueError, match='fit'): e.ready_local(m.defaults(), 8)


def test_child_cleanup_is_scoped_to_its_trial(evaluation, monkeypatch, tmp_path):
    m, _ = evaluation
    e = load_script(ROOT / 'libexec/t_eval_engine.py', 'eval_cleanup_test')
    monkeypatch.setattr(e, 'core', lambda: vars(m))
    calls = []
    ident = 'a'*32
    def run(command, **kw):
        calls.append(command)
        return 'ours\t' + ident + '-candidate__env\nother\tunrelated\n'
    monkeypatch.setattr(e, 'run', run)
    e.cleanup_containers(ident)
    assert calls[-1] == ['docker', 'rm', '-f', 'ours']
    with pytest.raises(ValueError): e.cleanup_containers('')
    j = m.Journal(m.create_run(m.defaults()))
    directory = j.directory / 'trials' / ident
    job = {'trial_id': ident, 'config': m.defaults()}
    monkeypatch.setenv('OPENAI_API_KEY', 'must-not-forward')
    monkeypatch.setenv('COV_CORE_SOURCE', 'must-not-forward')
    class Process:
        def __init__(self, command, **kw):
            assert 'OPENAI_API_KEY' not in kw['env'] and 'COV_CORE_SOURCE' not in kw['env']
            self.pid = 999999
        def poll(self): return 0
    monkeypatch.setattr(e.subprocess, 'Popen', Process)
    assert e.child_job('/python', job, directory, j)['status'] == 'failed'
    m.atomic(directory / 'outcome.json', {'status': 'completed', 'resolved': True})
    assert e.child_job('/python', job, directory, j)['resolved']
    m.atomic(j.directory / 'cancel.json', {})
    class Running(Process):
        def poll(self): return None
        def wait(self, **kw): return 0
    signals = []
    monkeypatch.setattr(e.subprocess, 'Popen', Running)
    monkeypatch.setattr(e.os, 'killpg', lambda *a: signals.append(a))
    assert e.child_job('/python', job, directory, j)['status'] == 'interrupted'
    assert signals[0][0] == 999999


def test_frontier_gateway_budget_outage_and_nested_tools(evaluation, monkeypatch):
    m, _ = evaluation
    p = load_script(ROOT / 'libexec/t_eval_proxy.py', 'eval_frontier_gateway')
    assert p.client_tools_only({'input': [{'type': 'additional_tools', 'tools': [{'type': 'namespace', 'tools': [{'type': 'custom'}]}]}]})
    assert not p.client_tools_only({'tools': [None]})
    assert not p.client_tools_only({'input': [{'type': 'additional_tools', 'tools': [{'type': 'namespace', 'tools': [{'type': 'web_search'}]}]}]})
    class Upstream(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def do_POST(self):
            self.rfile.read(int(self.headers['Content-Length']))
            body = b'{"model":"gpt-6-astra","service_tier":"priority","usage":{"input_tokens":10,"output_tokens":2}}'
            self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
    upstream = ThreadingHTTPServer(('127.0.0.1', 0), Upstream)
    thread = threading.Thread(target=upstream.serve_forever, daemon=True);thread.start()
    monkeypatch.setattr(p, 'frontier_connection', lambda: (f'http://127.0.0.1:{upstream.server_port}/v1', {}, 'api-equivalent'))
    config = {**m.defaults(), 'modes': ['outage']}
    j = m.Journal(m.create_run(config))
    (j.directory / 'events.jsonl').write_text('torn\n')
    def send(gateway, lease, suffix, value):
        body = value if isinstance(value, bytes) else json.dumps(value).encode()
        url = f'http://127.0.0.1:{gateway.server.server_port}/{lease["token"]}/{suffix}'
        with urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=3) as response:return response.read()
    try:
        with p.Gateway(config, j) as gateway:
            lease = gateway.lease('cloud', 'outage')
            payload = {'model': config['frontier_model']}
            assert b'output_tokens' in send(gateway, lease, 'frontier/v1/responses', payload)
            for _ in range(100):
                if gateway.records: break
                threading.Event().wait(.01)
            assert gateway.totals('cloud')['cost_kind'] == 'api-equivalent'
            assert gateway.totals('cloud')['cost_usd'] is None
            config['spend_limit_usd'] = 1
            with pytest.raises(urllib.error.HTTPError) as error:send(gateway, lease, 'frontier/v1/responses', payload)
            assert error.value.code == 429
            config['spend_limit_usd'] = None
            with pytest.raises(urllib.error.HTTPError): send(gateway, lease, 'frontier/v1/responses', b'')
            with pytest.raises(urllib.error.HTTPError): send(gateway, lease, 'control/checkpoint', b'x'*4097)
            send(gateway, lease, 'control/outage', {})
            with pytest.raises((http.client.HTTPException, urllib.error.URLError, ConnectionResetError)):send(gateway, lease, 'frontier/v1/responses', payload)
            second = gateway.lease('boundary', 'outage')
            with pytest.raises((http.client.HTTPException, urllib.error.URLError, ConnectionResetError)):
                send(gateway, second, 'frontier/v1/responses', {**payload,'input':[{'type':'function_call_output','output':'Process exited with code 0'}]})
            assert gateway.routes[second['token']]['offline']
    finally:
        upstream.shutdown();upstream.server_close();thread.join()


def test_native_codex_package_pins_architecture_and_integrity(evaluation, tmp_path, monkeypatch):
    import base64
    import hashlib
    import io
    import tarfile
    m, _ = evaluation
    e = load_script(ROOT / 'libexec/t_eval_engine.py', 'eval_native_package')
    monkeypatch.setattr(e, 'core', lambda: vars(m))
    monkeypatch.setattr(e, 'run', lambda *a, **kw: 'aarch64')
    journal = m.Journal(m.create_run(m.defaults()))
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w:gz') as archive:
        for name in ('bin/codex', 'bin/codex-code-mode-host', 'codex-resources/bwrap', 'codex-path/rg'):
            member = tarfile.TarInfo('package/vendor/aarch64-unknown-linux-musl/' + name)
            member.size = 7; archive.addfile(member, io.BytesIO(b'fixture'))
    payload = stream.getvalue()
    metadata = {'dist': {'tarball':'https://registry.npmjs.org/@openai/codex/-/fixture.tgz',
                         'integrity':'sha512-'+base64.b64encode(hashlib.sha512(payload).digest()).decode()}}
    def open_url(url, **kw):
        return io.BytesIO(payload if url.endswith('.tgz') else json.dumps(metadata).encode())
    monkeypatch.setattr(e.urllib.request, 'urlopen', open_url)
    folder = Path(e.native_codex('0.164.0', journal))
    assert (folder/'bin/codex').read_bytes() == b'fixture'
    assert journal.state['native_codex']['architecture'] == 'arm64'
    assert e.native_codex('0.164.0', journal) == str(folder)
    (folder/'bin/codex').write_bytes(b'changed')
    with pytest.raises(ValueError, match='checksum'): e.native_codex('0.164.0', journal)
    metadata['dist']['integrity'] = 'wrong'
    with pytest.raises(ValueError, match='integrity'): e.native_codex('0.165.0', journal)
    metadata['dist']['tarball'] = 'https://unexpected.invalid/package'
    with pytest.raises(ValueError, match='origin'): e.native_codex('0.166.0', journal)
    monkeypatch.setattr(e, 'run', lambda *a, **kw: 'unsupported')
    with pytest.raises(ValueError, match='CPU'): e.native_codex('0.164.0', journal)
