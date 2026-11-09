"""Startup recovery regression tests; no real services, models or user sessions."""
from contextlib import nullcontext
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import threading

import pytest
from conftest import load_script
from test_offline_recovery import UI, models, policy

ROOT = Path(__file__).parents[1]
ROUTING = {'code': -32603, 'message': 'account/read failed: workspace routing discovery failed'}


@pytest.fixture
def offline():
    return load_script(ROOT/'libexec/t_offline.py', 'offline_startup_test')


@pytest.mark.parametrize('method', ['account/read', 'thread/start'])
def test_startup_failure_is_correlated_before_first_thread(offline, tmp_path, method):
    cp = offline.Checkpoint(tmp_path, tmp_path)
    cp.client({'id': 12, 'method': method, 'params': {}})
    cp.server({'id': 99, 'error': ROUTING})
    assert not cp.startup_failure
    cp.server({'id': 12, 'error': ROUTING})
    assert cp.startup_failure
    assert not cp.due()  # No turn/checkpoint exists; handled after owned runtime shutdown.
    with pytest.raises(ValueError):
        cp.prompt()


@pytest.mark.parametrize('error', [
    {'code': -32603, 'message': 'unauthorized'},
    {'code': -32603, 'message': 'quota exceeded'},
    {'code': -32603, 'message': 'workspace routing discovery failed: HTTP 401'},
    {'code': -32600, 'message': 'workspace routing discovery failed'},
    {'code': -32603, 'message': 'unknown application error'},
])
def test_other_startup_errors_do_not_offer_local(offline, tmp_path, error):
    cp = offline.Checkpoint(tmp_path, tmp_path)
    cp.client({'id': 1, 'method': 'account/read'})
    cp.server({'id': 1, 'error': error})
    assert not cp.startup_failure


def test_account_error_after_thread_is_not_startup_failure(offline, tmp_path):
    cp = offline.Checkpoint(tmp_path, tmp_path)
    cp.thread = 'existing'
    cp.client({'id': 1, 'method': 'account/read'})
    cp.server({'id': 1, 'error': ROUTING})
    assert not cp.startup_failure


def test_startup_failure_is_retained_until_thread_starts(offline, tmp_path):
    cp = offline.Checkpoint(tmp_path, tmp_path)
    for ident, method in [(1, 'account/read'), (2, 'thread/start')]:
        cp.client({'id': ident, 'method': method, 'params': {}})
    cp.server({'id': 1, 'error': ROUTING})
    cp.server({'id': 2, 'error': {'code': -32603, 'message': 'initialization aborted'}})
    assert cp.startup_failure
    cp.client({'id': 3, 'method': 'thread/start', 'params': {}})
    cp.server({'id': 3, 'result': {'thread': {'id': 'new'}}})
    assert not cp.startup_failure


def test_typed_bootstrap_transport_failure_is_detected(offline, tmp_path):
    cp = offline.Checkpoint(tmp_path, tmp_path)
    cp.client({'id': 1, 'method': 'account/read'})
    cp.server({'id': 1, 'error': {'code': -32603, 'data': {
        'codexErrorInfo': {'httpConnectionFailed': {'httpStatusCode': None}}}}})
    assert cp.startup_failure


def test_resume_bootstrap_is_not_fresh_start(offline, tmp_path):
    cp = offline.Checkpoint(tmp_path, tmp_path)
    cp.client({'id': 1, 'method': 'thread/resume', 'params': {}})
    cp.server({'id': 1, 'error': ROUTING})
    assert not cp.startup_failure


@pytest.mark.parametrize('argv, expected', [
    (['codex'], None),
    (['codex', '--model', 'cloud', '-c', 'x=1', '--', 'fix the thing'], 'fix the thing'),
    (['codex', '-a', 'on-request', 'a quoted task'], 'a quoted task'),
    (['codex', '--', '--literal task'], '--literal task'),
])
def test_startup_prompt_preserves_text_without_inventing_checkpoint(offline, argv, expected):
    assert offline.startup_prompt(argv) == expected


@pytest.mark.parametrize('argv', [['codex', '-i', 'a.png', 'task'], ['codex', '--', 'x'*32001]])
def test_incomplete_startup_prompt_cannot_switch(offline, argv):
    with pytest.raises(ValueError):
        offline.startup_prompt(argv)


def test_startup_picker_selects_model_directly_and_exposes_retry(offline):
    local = {'catalogue': lambda **kw: {'models': models()}}
    ui = UI(['1'])
    assert offline.choose_local(ui, local, policy(), startup=True)['model'] == 'b'
    assert len(ui.calls) == 1
    assert ('retry', 'Retry cloud') in ui.calls[0][0][1]
    assert ('pause', 'Cancel startup') in ui.calls[0][0][1]
    assert offline.choose_local(UI(['retry']), local, policy(), startup=True) == 'retry'
    assert offline.choose_local(UI([None]), local, policy(), startup=True) is None


@pytest.mark.parametrize('prompt', [None, 'Keep the supplied task exactly.'])
@pytest.mark.parametrize('choices', [['1'], ['retry', '1'], [None], ['1', '0']])
def test_startup_recovery_retries_or_launches_local_without_extra_screen(offline, tmp_path, monkeypatch, prompt, choices):
    m = offline
    ui = UI(choices)
    cloud, launches = [], []
    monkeypatch.setattr(m.os, 'isatty', lambda fd: True)
    monkeypatch.setattr(m, 'recovery_ui', lambda *a: nullcontext(ui))
    monkeypatch.setattr(m, 'RecoveryProbe', lambda: nullcontext(SimpleNamespace(ready=threading.Event())))
    def supervise(argv, chosen, cwd, **kw):
        cloud.append(argv)
        cp = kw['checkpoint']
        cp.client({'id': 1, 'method': 'account/read'})
        cp.server({'id': 1, 'error': ROUTING})
        return {'action': None, 'exit_code': 1, 'checkpoint': cp}
    def lead(argv, **kw):
        launches.append((argv, kw))
        assert kw['chosen']['mode'] == 'local-only'
        assert ('--' in argv) == (prompt is not None)
        if prompt:
            assert argv[-1] == prompt
        if choices == ['1', '0'] and len(launches) == 1:
            raise ValueError('local unavailable')
        return 0
    monkeypatch.setattr(m, 'supervise', supervise)
    monkeypatch.setattr(m, 'inference', lambda: {
        'helper': lambda n: {'state_root': lambda: tmp_path},
        'catalogue': lambda **kw: {'models': models()}, 'lead': lead})
    argv = ['codex', '-a', 'on-request'] + (['--', prompt] if prompt else [])
    assert m.recover(argv, policy(), tmp_path) == (1 if choices == [None] else 0)
    assert len(ui.calls) == len(choices)
    assert len(cloud) == (2 if 'retry' in choices else 1)
    assert len(launches) == (0 if choices == [None] else 2 if choices == ['1', '0'] else 1)


@pytest.mark.parametrize('ask', [True, False])
def test_bootstrap_error_survives_immediate_frontend_exit(offline, tmp_path, monkeypatch, ask):
    """Exercise real owned subprocess/bridge shutdown, not a mocked monitor."""
    script = tmp_path/'codex'
    script.write_text('#!'+sys.executable+'\n'+'''
import json, os, runpy, socket, sys, time
if sys.argv[1] == 'app-server':
 for raw in sys.stdin:
  msg = json.loads(raw)
  print(json.dumps({'id': msg['id'], 'error': {'code': -32603, 'message': 'account/read failed: workspace routing discovery failed'}}), flush=True)
else:
 sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
 sock.connect(sys.argv[2].removeprefix('unix://'))
 ws = runpy.run_path(os.environ['SOCKET_HELPER'])['WebSocket'](sock.makefile('rb'), sock.makefile('wb'), time.monotonic()+10)
 ws.handshake()
 ws.send(json.dumps({'id': 1, 'method': 'account/read', 'params': {}}).encode())
 assert 'error' in json.loads(ws.receive())
 sys.exit(1)
''')
    script.chmod(0o700)
    monkeypatch.setenv('SOCKET_HELPER', str(ROOT/'libexec/t_codex_socket.py'))
    monkeypatch.setattr(offline, 'inference', lambda: {})
    monkeypatch.setattr(offline.os, 'isatty', lambda fd: False)
    processes = []
    original = offline.subprocess.Popen
    def popen(*a, **kw):
        p = original(*a, **kw); processes.append(p); return p
    monkeypatch.setattr(offline.subprocess, 'Popen', popen)
    cp = offline.Checkpoint(tmp_path, tmp_path)
    result = offline.supervise([str(script)], dict(policy(), on_cloud_failure='pause' if ask else 'local'), tmp_path,
                               checkpoint=cp, monitor=(lambda *a: None) if ask else None)
    assert (result['exit_code'] in (None, 1) if ask else result == 1) and cp.startup_failure
    assert all(p.poll() is not None for p in processes)


@pytest.mark.parametrize('width,height', [(80, 24), (44, 20), (32, 12)])
def test_startup_picker_keeps_retry_and_cancel_accessible(offline, t_mod, width, height):
    from test_tui import make_ui
    ui = make_ui(t_mod, ['j', 'j', 'j', 'enter'], width, height)
    assert offline.choose_local(ui, {'catalogue': lambda **kw: {'models': models()}},
                                policy(), startup=True) == 'retry'
    assert all(len(frame) <= height for frame in ui.frames)


def test_saved_pause_policy_keeps_native_launch(offline, monkeypatch):
    local = load_script(ROOT/'libexec/t_local_inference.py', 'startup_policy_test')
    calls = []
    monkeypatch.setattr(local.subprocess, 'call', lambda argv: calls.append(argv) or 17)
    monkeypatch.setattr(local, 'helper', lambda name: pytest.fail('Pause must not start recovery'))
    assert local.lead(['codex'], chosen=dict(policy(), on_cloud_failure='pause')) == 17
    assert calls == [['codex']]


def test_failed_cloud_return_keeps_original_task_and_local_progress(offline, tmp_path, monkeypatch):
    m = offline
    monkeypatch.setattr(m.os, 'isatty', lambda fd: True)
    monkeypatch.setattr(m, 'recovery_ui', lambda *a: nullcontext(UI(['0'])))
    monkeypatch.setattr(m, 'choose_cloud', lambda *a: {'model': 'cloud'})
    ready = threading.Event(); ready.set()
    monkeypatch.setattr(m, 'RecoveryProbe', lambda: nullcontext(SimpleNamespace(ready=ready)))
    stages = []
    def supervise(argv, chosen, cwd, **kw):
        cp = kw['checkpoint']; stages.append(argv)
        if len(stages) == 3:
            cp.client({'id': 1, 'method': 'account/read'})
            cp.server({'id': 1, 'error': ROUTING})
            return {'action': None, 'exit_code': 1, 'checkpoint': cp}
        cp.client({'id': 1, 'method': 'thread/start', 'params': {}})
        cp.server({'id': 1, 'result': {'thread': {'id': 'main'}, 'model': 'cloud'}})
        cp.client({'method': 'turn/start', 'params': {'threadId': 'main', 'input': [
            {'type': 'text', 'text': cp.handoff or 'Original task'}]}})
        cp.active = False
        if len(stages) == 1:
            cp.failed = True
        elif len(stages) == 2:
            cp.evidence.append({'kind': 'agentMessage', 'text': 'Local progress'})
        else:
            assert cp.users == ['Original task']
            assert cp.evidence[-1]['text'] == 'Local progress'
            return {'action': None, 'exit_code': 0, 'checkpoint': cp}
        return {'action': kw['monitor'](cp, None, None), 'exit_code': None, 'checkpoint': cp}
    def lead(argv, **kw):
        return kw['runner'](['codex', '--model', 'local'], {})
    monkeypatch.setattr(m, 'supervise', supervise)
    monkeypatch.setattr(m, 'inference', lambda: {'helper': lambda n: {'state_root': lambda: tmp_path},
                                              'catalogue': lambda **kw: {'models': models()}, 'lead': lead})
    assert m.recover(['codex', '--', 'Original task'], policy(), tmp_path) == 0
    assert len(stages) == 4


@pytest.mark.parametrize('prompt', [None, 'The original task'])
def test_startup_runner_preserves_prompt_and_tracks_first_local_turn(offline, tmp_path, monkeypatch, prompt):
    m = offline
    monkeypatch.setattr(m.os, 'isatty', lambda fd: True)
    monkeypatch.setattr(m, 'recovery_ui', lambda *a: nullcontext(UI(['0'])))
    monkeypatch.setattr(m, 'RecoveryProbe', lambda: nullcontext(SimpleNamespace(ready=threading.Event())))
    def supervise(argv, chosen, cwd, **kw):
        cp = kw['checkpoint']
        if 'env' not in kw:
            cp.client({'id': 1, 'method': 'account/read'})
            cp.server({'id': 1, 'error': ROUTING})
            return {'action': None, 'exit_code': 1, 'checkpoint': cp}
        assert ('--' in argv) == bool(prompt)
        cp.client({'id': 1, 'method': 'thread/start', 'params': {}})
        cp.server({'id': 1, 'result': {'thread': {'id': 'local'}}})
        cp.client({'method': 'turn/start', 'params': {'threadId': 'local', 'input': [
            {'type': 'text', 'text': prompt or 'Typed after local startup'}]}})
        assert cp.users == [prompt or 'Typed after local startup']
        return {'action': None, 'exit_code': 0, 'checkpoint': cp}
    monkeypatch.setattr(m, 'supervise', supervise)
    monkeypatch.setattr(m, 'inference', lambda: {'helper': lambda n: {'state_root': lambda: tmp_path},
        'catalogue': lambda **kw: {'models': models()},
        'lead': lambda argv, **kw: kw['runner'](['codex', '--model', 'local'], {})})
    assert m.recover(['codex'] + (['--', prompt] if prompt else []), policy(), tmp_path) == 0


def test_startup_without_models_still_offers_cloud_retry(offline):
    ui = UI(['retry'])
    assert offline.choose_local(ui, {'catalogue': lambda **kw: {'models': []}}, policy(), startup=True) == 'retry'
    assert ui.calls[0][0][1] == [('retry', 'Retry cloud'), ('pause', 'Cancel startup')]


def test_startup_preview_retries_without_starting_any_services(monkeypatch, capsys):
    preview = load_script(ROOT/'scripts/preview_connection_recovery.py', 'startup_preview_test')
    calls, choices = [], iter(['retry', {'model': 'sample-local'}])
    def choose(*a, **kw):
        calls.append(kw); return next(choices)
    recovery = {'ui_helpers': lambda: {}, 'recovery_ui': lambda: nullcontext(None),
                'choose_local': choose, 'choose_cloud': lambda *a: None}
    monkeypatch.setattr(preview.runpy, 'run_path', lambda *a: recovery)
    monkeypatch.setattr(preview.sys, 'argv', ['preview', '--startup'])
    monkeypatch.setattr(preview.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(preview.sys.stdout, 'isatty', lambda: True)
    monkeypatch.setattr('builtins.input', lambda *a: '')
    assert preview.main() == 0
    assert len(calls) == 2 and all(call['startup'] for call in calls)
    assert 'No agent was launched' in capsys.readouterr().out
