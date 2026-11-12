"""Explicit refresh observation never changes the ordinary list or worker lifetime."""
import copy
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from test_session_discovery import adapter, view  # noqa: F401
from test_session_cache import cache, engine, spec, drain  # noqa: F401
from test_agent_seam import zsh  # noqa: F401


@pytest.fixture
def clock(adapter, monkeypatch):
    now = [0.0]
    monkeypatch.setattr(adapter, 'time', SimpleNamespace(
        monotonic=lambda: now[0], sleep=lambda seconds: now.__setitem__(0, now[0] + seconds)))
    return now


def pending():
    data = view()
    data['hosts'][0].update(stale=True, refreshing=True)
    return data


@pytest.mark.parametrize('state', ['fresh', 'failed', 'unavailable'])
def test_wait_settled_snapshot_returns_without_query(adapter, monkeypatch, clock, state):
    data = view()
    if state == 'failed':
        data['hosts'][0].update(stale=True, error='offline')
    if state == 'unavailable':
        data = {'unavailable': True}
    monkeypatch.setattr(adapter, 'discover', lambda *a, **k: pytest.fail('unexpected query'))
    assert adapter.wait_for_refresh(None, data) is data
    assert clock == [0]


@pytest.mark.parametrize('cold', [False, True])
@pytest.mark.parametrize('failed', [False, True])
def test_wait_completion_failure_and_scope(adapter, monkeypatch, clock, cold, failed):
    initial = pending()
    if cold:
        initial['hosts'][0]['value'] = None
        initial['loading'] = True
    done = view()
    if failed:
        done['hosts'][0].update(error='offline', stale=True, value=initial['hosts'][0]['value'])
    calls = []
    def discover(cfg, **kwargs):
        calls.append((cfg, kwargs))
        return done
    monkeypatch.setattr(adapter, 'discover', discover)
    assert adapter.wait_for_refresh('config', initial, local=True) == done
    assert calls == [('config', dict(local=True, fields=['rows'], deadline=30))]
    assert clock == [.25]


def test_wait_tracks_all_hosts_without_changing_snapshot(adapter, monkeypatch, clock):
    initial = pending()
    initial['hosts'].append(dict(initial['hosts'][0], host='mini', target='mini'))
    partial = copy.deepcopy(initial)
    partial['hosts'][0]['refreshing'] = False
    done = copy.deepcopy(partial)
    done['hosts'][1].update(refreshing=False, error='offline')
    snapshots = iter([partial, done])
    monkeypatch.setattr(adapter, 'discover', lambda *a, **kw: next(snapshots))
    assert adapter.wait_for_refresh(None, initial) == done
    assert initial['hosts'][0]['refreshing']
    assert clock == [.5]


@pytest.mark.parametrize('during_request', [False, True])
def test_wait_deadline_retains_rows_and_stops_queries(adapter, monkeypatch, clock, during_request):
    initial = pending()
    calls = []
    def discover(*a, **kwargs):
        calls.append(kwargs)
        if during_request:
            clock[0] = kwargs['deadline']
            return {'unavailable': True}
        return initial
    monkeypatch.setattr(adapter, 'discover', discover)
    result = adapter.wait_for_refresh(None, initial, timeout=1)
    assert result == dict(initial, wait_timed_out=True)
    assert clock == [1]
    assert len(calls) == (1 if during_request else 3)


def test_wait_service_loss_retains_available_snapshot(adapter, monkeypatch, clock):
    data = pending()
    monkeypatch.setattr(adapter, 'discover', lambda *a, **kw: {'unavailable': True})
    assert adapter.wait_for_refresh(None, data) == dict(data, wait_unavailable=True)
    assert clock == [.25]


@pytest.mark.parametrize('error', [KeyboardInterrupt, RuntimeError])
def test_wait_cancellation_and_failure_close_progress(adapter, monkeypatch, clock, error):
    events = []
    @contextmanager
    def spinner(message):
        events.append(message)
        try:
            yield
        finally:
            events.append('closed')
    monkeypatch.setattr(adapter, '_spinner', spinner)
    def discover(*a, **kw):
        raise error()
    monkeypatch.setattr(adapter, 'discover', discover)
    with pytest.raises(error):
        adapter.wait_for_refresh(None, pending())
    assert events == ['Refreshing sessions', 'closed']


def test_wait_cold_timeout_never_reports_empty(adapter, monkeypatch, capsys):
    data = pending()
    data['hosts'][0]['value'] = None
    adapter.announce(dict(data, wait_timed_out=True))
    result = capsys.readouterr()
    assert not result.out and 'timed out' in result.err and 'refreshing' in result.err
    assert 'No dev sessions' not in result.err
    adapter.announce(dict(view(), wait_unavailable=True))
    assert 'last available snapshot' in capsys.readouterr().err


def test_wait_reuses_single_service_collector(adapter, engine, monkeypatch, clock):
    memory, _, jobs, _ = engine
    cfg = SimpleNamespace(hosts={}, repos={})
    def request(operation, payload, **kwargs):
        if kwargs and jobs:
            drain(jobs)
        return memory.handle(operation, payload)
    monkeypatch.setattr(adapter, 'request', request)
    initial = adapter.discover(cfg, fields=['rows'])
    again = adapter.discover(cfg, fields=['rows'])
    assert len(jobs) == 1
    assert adapter.wait_for_refresh(cfg, initial)['hosts'][0]['value']['rows']
    assert adapter.wait_for_refresh(cfg, again)['hosts'][0]['value']['rows']
    assert not jobs


def test_wait_sees_refresh_queued_behind_another_context(engine):
    memory, _, jobs, _ = engine
    memory.handle('discover', {'hosts': [spec(context={'repo': 'one'})]})
    second = {'hosts': [spec(context={'repo': 'two'})]}
    queued = memory.handle('discover', second)['hosts'][0]
    assert queued['refreshing'] and queued['value'] is None
    assert len(jobs) == 1
    drain(jobs)
    assert memory.handle('discover', second)['hosts'][0]['refreshing']
    assert len(jobs) == 1
    drain(jobs)
    assert not memory.handle('discover', second)['hosts'][0]['refreshing']


@pytest.mark.parametrize('wait', [False, True])
@pytest.mark.parametrize('terminal', [False, True])
def test_list_wait_is_explicit_and_readonly(t_mod, adapter, monkeypatch, capsys, wait, terminal):
    monkeypatch.delenv('T_DISCOVERY_COLLECTOR')
    monkeypatch.setattr(t_mod.sys.stdout, 'isatty', lambda: terminal)
    monkeypatch.setattr(t_mod.sys.stderr, 'isatty', lambda: terminal)
    monkeypatch.setattr(t_mod, '_columns', lambda: 200)
    monkeypatch.setattr('builtins.input', lambda *a: pytest.fail('list prompted'))
    calls = []
    data = view()
    def observe(cfg, initial, **kwargs):
        calls.append(kwargs)
        return initial
    helper = dict(discover=lambda *a, **kw: data, wait_for_refresh=observe,
                  announce=adapter.announce, rows=adapter.rows)
    monkeypatch.setattr(t_mod, '_session_discovery', lambda: helper)
    cfg = SimpleNamespace(hosts={}, repos={}, worktree_root='/tmp/fixture-worktrees')
    args = t_mod.build_parser().parse_args(['ls', '--local'] + (['--wait'] if wait else []))
    assert t_mod.cmd_ls(cfg, args) == 0
    output = capsys.readouterr()
    assert 'api-1' in output.out and 'STATUS' in output.out
    assert calls == ([{'local': True}] if wait else [])
    if not terminal:
        assert '\x1b' not in output.out + output.err
    for prefix in (['ls'], ['list'], ['session', 'list'], ['session', 'ls']):
        assert t_mod._command_readonly(prefix + ['--wait'], terminal=terminal)


def test_list_wait_completion(zsh):
    result = zsh('_values() { print -l -- "$@"; }; words=(t session list --); CURRENT=4; _t')
    assert result.returncode == 0
    assert '--wait' in result.stdout.splitlines()


@pytest.mark.parametrize('missing', [False, True])
def test_partial_empty_list_does_not_claim_verified_empty(t_mod, adapter, monkeypatch, capsys, missing):
    monkeypatch.delenv('T_DISCOVERY_COLLECTOR')
    data = view()
    data['hosts'][0]['value']['rows'] = ''
    data['hosts'].append(dict(host='mini', checked=0, refreshing=not missing,
                             error='offline' if missing else '', value=None))
    helper = dict(discover=lambda *a, **kw: data, wait_for_refresh=lambda *a, **kw: data,
                  announce=adapter.announce, rows=adapter.rows)
    monkeypatch.setattr(t_mod, '_session_discovery', lambda: helper)
    cfg = SimpleNamespace(hosts={}, repos={}, worktree_root='/tmp/fixture-worktrees')
    assert t_mod.cmd_ls(cfg, t_mod.build_parser().parse_args(['ls', '--wait'])) == 0
    result = capsys.readouterr()
    assert 'discovery is incomplete' in result.out
    assert 'No dev sessions running' not in result.out


def test_deadline_request_never_restarts_service(cache, monkeypatch):
    calls = []
    def wire(*args):
        calls.append(args)
        raise TimeoutError()
    monkeypatch.setattr(cache, '_wire', wire)
    monkeypatch.setattr(cache.subprocess, 'Popen', lambda *a, **k: pytest.fail('wait restarted service'))
    assert cache.request('discover', {'hosts': []}, deadline=42)['unavailable']
    assert calls == [('discover', {'hosts': []}, 42)]


@pytest.mark.parametrize('chunks', [[b'{"ok":', b'true}\n'], [b'{"ok":true}', b'']])
def test_wire_deadline_bounds_each_read(cache, monkeypatch, chunks):
    deadlines = []
    clock = [0.0]
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, timeout): deadlines.append(timeout)
        def connect(self, address): clock[0] += .1
        def sendall(self, data): clock[0] += .1
        def recv(self, size):
            clock[0] += .1
            return chunks.pop(0)
    monkeypatch.setattr(cache.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(cache.socket, 'socket', lambda *a: Socket())
    assert cache._wire('discover', {}, deadline=1) == {'ok': True}
    assert deadlines == pytest.approx([1, .9, .8, .7])
    with pytest.raises(TimeoutError):
        cache._wire('discover', {}, deadline=0)


def test_wire_rejects_oversized_response(cache, monkeypatch):
    class Socket:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def settimeout(self, timeout): pass
        def connect(self, address): pass
        def sendall(self, data): pass
        def recv(self, size): return b'x' * size
    monkeypatch.setattr(cache.socket, 'socket', lambda *a: Socket())
    monkeypatch.setattr(cache, 'LIMIT', 10)
    with pytest.raises(ValueError, match='too large'):
        cache._wire('discover', {})


def test_open_waits_for_new_attached_row_before_snapshot(adapter, monkeypatch, clock):
    initial = pending()
    initial['hosts'][0]['value']['rows'] = ''
    done = view()
    done['hosts'][0]['value']['rows'] = 'new\t/work\tapi-10\tattached\tactive\tNew task\tcodex\n'
    calls = []
    def discover(cfg, **kwargs):
        calls.append(kwargs)
        return initial if len(calls) == 1 else done
    monkeypatch.setattr(adapter, 'discover', discover)
    result, notice = adapter.open_snapshot(None, lambda *a: pytest.fail('completed refresh prompted'), local=True)
    assert 'api-10\tattached' in adapter.rows(result)
    assert not notice and clock == [.25]
    assert all(call['local'] for call in calls)


def test_open_wait_keeps_remote_scope_and_metadata(adapter, monkeypatch, clock):
    initial, done = pending(), view()
    calls = []
    monkeypatch.setattr(adapter, 'discover', lambda cfg, **kw: calls.append(kw) or done)
    assert adapter.wait_for_refresh(None, initial, host='mini', fields=['rows', 'scopes']) is done
    assert calls == [dict(local=False, host='mini', fields=['rows', 'scopes'], deadline=30)]


@pytest.mark.parametrize('available', [False, True])
@pytest.mark.parametrize('action', ['cached', 'cancel', None])
def test_open_timeout_requires_explicit_cached_choice(adapter, monkeypatch, available, action):
    data = pending()
    if not available:
        data['hosts'][0]['value'] = None
    monkeypatch.setattr(adapter, 'discover', lambda *a, **k: data)
    monkeypatch.setattr(adapter, 'wait_for_refresh', lambda *a, **k: dict(data, wait_timed_out=True))
    choices = []
    choose = lambda message, rows: choices.append((message, rows)) or action
    if action == 'cached' and available:
        result, notice = adapter.open_snapshot(None, choose)
        assert 'api-1' in adapter.rows(result) and 'incomplete' in notice
    else:
        with pytest.raises(KeyboardInterrupt):
            adapter.open_snapshot(None, choose)
    assert choices == [('Session discovery is still refreshing', available)]


def test_open_retry_does_not_reset_remote_failure_cooldown(adapter, monkeypatch):
    initial = pending()
    initial['hosts'].append(dict(host='mini', target='u@mini', error='offline', value=None, retry_at=100))
    done = view()
    done['hosts'].append(initial['hosts'][1])
    snapshots = iter([initial, done])
    monkeypatch.setattr(adapter, 'discover', lambda *a, **k: next(snapshots))
    monkeypatch.setattr(adapter, 'wait_for_refresh', lambda cfg, value, **k: dict(value, wait_timed_out=True) if value is initial else value)
    monkeypatch.setattr(adapter, 'request', lambda *a, **k: pytest.fail('remote cooldown reset'))
    choices = []
    result, notice = adapter.open_snapshot(None, lambda *args: choices.append(args) or 'retry')
    assert len(choices) == 1 and 'api-1' in adapter.rows(result) and notice


def test_open_local_retry_and_verified_empty(adapter, monkeypatch):
    data = view()
    data['hosts'][0].update(error='failed', value=None)
    done = view()
    done['hosts'][0]['value']['rows'] = ''
    snapshots = iter([data, done])
    monkeypatch.setattr(adapter, 'discover', lambda *a, **k: next(snapshots))
    calls = []
    monkeypatch.setattr(adapter, 'request', lambda *args: calls.append(args) or {})
    assert adapter.open_snapshot(None, lambda *a: 'retry') == (done, '')
    assert calls == [('reset-host', {'target': 'local'})]


def test_open_local_retry_service_unavailable(adapter, monkeypatch):
    data = view()
    data['hosts'][0].update(error='failed', value=None)
    monkeypatch.setattr(adapter, 'discover', lambda *a, **k: data)
    monkeypatch.setattr(adapter, 'request', lambda *a: {'unavailable': True})
    with pytest.raises(OSError, match='discovery unavailable'):
        adapter.open_snapshot(None, lambda *a: 'retry')


def test_private_open_rows_waits_and_keeps_notice_out_of_rows(t_mod, adapter, monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(t_mod, 'Config', lambda: None)
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(t_mod.sys.stderr, 'isatty', lambda: True)
    calls = []
    def snapshot(cfg, choose, **kwargs):
        calls.append(kwargs)
        return view(), 'Older results'
    monkeypatch.setattr(t_mod, '_session_discovery', lambda: dict(open_snapshot=snapshot, announce=lambda v: None, rows=adapter.rows))
    notice = tmp_path / 'notice'
    assert t_mod.main(['__cache-rows', '--open', '--host', 'mini', '--repo', 'api', '--notice-file', str(notice)]) == 0
    assert notice.read_text() == 'Older results'
    assert calls == [dict(local=False, host='mini', fields=['rows', 'scopes', 'catalog'])]
    assert 'Older results' not in capsys.readouterr().out


def test_private_open_rows_never_prompts_off_terminal(t_mod, monkeypatch, capsys):
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: False)
    assert t_mod.main(['__cache-rows', '--open']) == 1
    assert 'requires a terminal' in capsys.readouterr().err


def test_private_open_rows_reports_service_failure(t_mod, monkeypatch, capsys):
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(t_mod.sys.stderr, 'isatty', lambda: True)
    def failed(*args, **kwargs):
        raise OSError('session discovery unavailable; retry shortly')
    monkeypatch.setattr(t_mod, '_session_discovery', lambda: {'open_snapshot': failed})
    assert t_mod.main(['__cache-rows', '--open']) == 1
    output = capsys.readouterr()
    assert not output.out and 't open: session discovery unavailable' in output.err


@pytest.mark.parametrize('available', [False, True])
def test_open_recovery_uses_shared_rail_and_restores_terminal(t_mod, monkeypatch, available):
    calls = []
    class UI:
        def raw(self): calls.append('raw')
        def restore(self): calls.append('restore')
        def pick(self, message, choices, **kwargs):
            calls.append(choices)
            return 'cancel'
    monkeypatch.setattr(t_mod, '_RailUI', UI)
    assert t_mod._open_discovery_choice('Still refreshing', available) == 'cancel'
    assert calls[0] == 'raw' and calls[-1] == 'restore'
    assert ('cached', 'Browse older results') in calls[1] if available else ('cached', 'Browse older results') not in calls[1]
