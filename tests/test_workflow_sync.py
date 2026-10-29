"""Propagation uses supported settings APIs, never conversation messages."""
import copy
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def sync(tmp_path, monkeypatch):
    for key in ('HOME', 'XDG_CONFIG_HOME', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR'):
        folder = tmp_path / key
        folder.mkdir()
        monkeypatch.setenv(key, str(folder))
    spec = importlib.util.spec_from_file_location('workflow_sync_test', ROOT / 'libexec/t_workflow_sync.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def saved(sync):
    w = sync.workflow()
    doc = w['defaults']()
    w['save'](doc, None, propagate=False)
    return doc


def test_publication_noop_scopes_and_private_journal(sync, monkeypatch):
    w = sync.workflow()
    initial = w['defaults']()
    after = copy.deepcopy(initial)
    after['workflows']['unused'] = copy.deepcopy(w['DEFAULT'])
    assert sync.changed_scopes(initial, after) == []
    after['repositories']['/repository'] = {'workflow': 'unused'}
    after['workflows']['unused']['instructions'] = 'scope text'
    assert sync.changed_scopes(initial, after) == ['/repository']
    after['workflows']['standard']['instructions'] = 'global text'
    assert sync.changed_scopes(initial, after) == [None, '/repository']
    w['save'](after, None)
    state = sync.status()
    assert state['revision'] == 1 and state['native'] == 'published'
    assert 'unverified' in state['runtime']
    assert sync.state_path().stat().st_mode & 0o777 == 0o600
    assert 'scope text' not in sync.state_path().read_text()  # no full policy when no SSH work
    stamp = sync.state_path().stat().st_mtime_ns
    w['save'](after, w['snapshot'](w['config_path']()))
    assert sync.state_path().stat().st_mtime_ns == stamp
    # A superseded publication cannot replace the latest journal.
    assert sync.changed(initial, initial) is None
    assert sync.status()['revision'] == 1
    monkeypatch.setattr(sync, 'write', lambda *_: pytest.fail('read-only status wrote'))
    assert sync.status()['revision'] == 1


def test_explicit_main_settings_independent_from_stages(sync):
    assert sync.settings_delta({'DEV_MODEL[codex]': 'old', 'DEV_FAST[codex]': 'ultrafast'},
                               {'DEV_MODEL[codex]': 'new', 'DEV_EFFORT[codex]': 'high'}) == {'model': 'new', 'effort': 'high'}
    assert sync.settings_delta({'DEV_MODEL[codex]': 'old'}, {}) == {'model': None}
    doc = saved(sync)
    doc['workflows']['standard']['implementation']['model'] = 'not-main'
    w = sync.workflow()
    w['save'](doc, w['snapshot'](w['config_path']()))
    assert sync.status()['settings'] == {}


def fake_proxy(sync, monkeypatch, pages, threads, *, fail=None):
    calls = []
    class Fake:
        def __enter__(self): return self
        def __exit__(self, *_): pass
        def call(self, method, params):
            calls.append((method, params))
            if method == fail: raise ValueError('unsupported')
            if method == 'thread/loaded/list': return pages[params['cursor']]
            if method == 'thread/read': return {'thread': threads[params['threadId']]}
            return {}
    monkeypatch.setattr(sync, 'Proxy', Fake)
    return calls


def test_paginated_root_settings_preserve_tier_permissions_and_stage_agents(sync, monkeypatch):
    doc = saved(sync)
    calls = fake_proxy(sync, monkeypatch, {None: {'data': ['root', 'stage'], 'nextCursor': 'next'},
                                         'next': {'data': ['current', 'custom'], 'nextCursor': None}},
                       {'root': {'source': 'cli', 'model': 'old', 'reasoningEffort': 'high'},
                        'stage': {'source': {'subAgent': {'thread_spawn': {}}}, 'parentThreadId': 'root'},
                        'current': {'source': 'appServer', 'model': 'new', 'reasoningEffort': 'medium'},
                        'custom': {'source': {'custom': 'unknown'}}})
    state = sync.changed(doc, doc, settings={'model': 'new', 'effort': 'medium'})
    updates = [params for method, params in calls if method == 'thread/settings/update']
    assert updates == [{'threadId': 'root', 'model': 'new', 'effort': 'medium'}]
    assert state['threads'] == {'root': 'accepted for subsequent turns', 'current': 'already current'}
    assert 'subsequent turns' in state['runtime']
    assert all(method in ('thread/loaded/list', 'thread/read', 'thread/settings/update') for method, _ in calls)
    assert not any('serviceTier' in params or 'permissions' in params or 'collaborationMode' in params for _, params in calls)


def test_retry_failure_superseding_and_reset(sync, monkeypatch):
    doc = saved(sync)
    fake_proxy(sync, monkeypatch, {None: {'data': ['one']}}, {}, fail='thread/read')
    state = sync.changed(doc, doc, settings={'model': 'new'})
    assert state['runtime'].startswith('pending:') and state['attempts'] == 1
    calls = fake_proxy(sync, monkeypatch, {None: {'data': ['one']}}, {'one': {'source': 'cli', 'model': 'old'}})
    state = sync.retry()
    assert state['attempts'] == 2 and 'accepted' in state['runtime']
    state = sync.changed(doc, doc, settings={'model': None})
    assert state['revision'] == 2 and state['runtime'].startswith('pending: clearing')
    assert len([m for m, _ in calls if m == 'thread/settings/update']) == 1
    monkeypatch.setattr(sync, 'current', lambda _: False)
    assert sync.retry()['revision'] == 2


@pytest.mark.parametrize('page,limit', [({'data': [3]}, 200), ({'data': ['a', 'b']}, 1),
                                        ({'data': [], 'nextCursor': 3}, 200)])
def test_malformed_or_bounded_pages_stay_pending(sync, monkeypatch, page, limit):
    doc = saved(sync)
    monkeypatch.setattr(sync, 'MAX_THREADS', limit)
    fake_proxy(sync, monkeypatch, {None: page}, {'a': {'source': 'cli'}})
    assert sync.changed(doc, doc, settings={'model': 'new'})['runtime'].startswith('pending:')


def test_repeated_cursor_and_stale_check(sync, monkeypatch):
    doc = saved(sync)
    fake_proxy(sync, monkeypatch, {None: {'data': [], 'nextCursor': 'same'},
                                 'same': {'data': [], 'nextCursor': 'same'}}, {})
    assert sync.changed(doc, doc, settings={'model': 'new'})['runtime'].startswith('pending:')
    calls = fake_proxy(sync, monkeypatch, {None: {'data': ['one']}}, {'one': {'source': 'cli'}})
    count = iter([True, False, False, False])
    monkeypatch.setattr(sync, 'current', lambda _: next(count, False))
    sync.retry()
    assert not any(method == 'thread/settings/update' for method, _ in calls)


def test_remote_identity_mapping_nonrecursive_and_pending(sync, monkeypatch, tmp_path):
    doc = saved(sync)
    after = copy.deepcopy(doc)
    after['repositories']['/local/repo'] = {'workflow': 'standard', 'excluded': True}
    monkeypatch.setattr(sync, 'identity', lambda path: 'owner/repo' if path in ('/local/repo', str(tmp_path)) else None)
    payload, missing = sync.portable(after, ['/local/repo', '/unknown'])
    assert missing == 1 and '/local/repo' not in json.dumps(payload)
    payload['settings'] = {}
    result = sync.receive(payload, {'api': str(tmp_path)})
    actual = sync.workflow()['load']()[0]
    assert actual['repositories'][str(tmp_path)]['excluded']
    assert 'native instructions published' in result['summary']
    assert sync.status()['context'] == {'hosts': {}, 'repos': {}}  # receiver cannot fan out
    assert sync.receive(payload, {})['summary'].startswith('pending: unmapped')
    with pytest.raises(ValueError, match='unsupported'):
        sync.receive({'version': 4}, {})
    payload['settings'] = {'serviceTier': 'ultrafast'}
    with pytest.raises(ValueError, match='invalid main'):
        sync.receive(payload, {})


def test_unused_named_remote_preserves_unrelated_and_resets_scope(sync, monkeypatch, tmp_path):
    doc = saved(sync)
    doc['workflows']['private'] = {**copy.deepcopy(sync.workflow()['DEFAULT']), 'instructions': 'preserve'}
    doc['repositories'][str(tmp_path)] = {'workflow': 'private'}
    w = sync.workflow(); w['save'](doc, w['snapshot'](w['config_path']()), propagate=False)
    monkeypatch.setattr(sync, 'identity', lambda _: 'owner/repo')
    payload, _ = sync.portable(w['defaults'](), [str(tmp_path)])
    assert payload['repositories']['owner/repo'] is None
    sync.receive(payload, {'a': str(tmp_path), 'same': str(tmp_path)})
    actual = w['load']()[0]
    assert str(tmp_path) not in actual['repositories']
    assert actual['workflows']['private']['instructions'] == 'preserve'


def test_remote_calls_bounded_no_paths_and_failure(sync, monkeypatch):
    doc = saved(sync)
    monkeypatch.setattr(sync, 'identity', lambda _: 'owner/repo')
    doc['workflows']['standard']['instructions'] = 'new'
    w = sync.workflow(); w['save'](doc, w['snapshot'](w['config_path']()), propagate=False)
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        if argv[-2] == 'bad': raise OSError('offline')
        return SimpleNamespace(returncode=0, stdout=json.dumps({'summary': 'native published; runtime unverified'}))
    monkeypatch.setattr(sync.subprocess, 'run', run)
    monkeypatch.setattr(sync, 'MAX_HOSTS', 2)
    context = {'hosts': {'first': 'ok', 'second': 'bad', 'duplicate': 'ok', 'later': 'z'}}
    state = sync.changed(w['defaults'](), doc, context=context)
    assert len(calls) == 2
    assert 'zsh -lic' in calls[0][0][-1] and '--receive' in calls[0][0][-1]
    assert calls[0][1]['timeout'] == 5
    assert state['hosts']['bad'].startswith('pending:') and state['hosts']['z'].startswith('pending:')
    assert state['hosts']['ok'].startswith('native published')
    assert state['context']['hosts'] == context['hosts']


def test_git_identity_and_settings_source_staleness(sync, monkeypatch, tmp_path):
    monkeypatch.setattr(sync.subprocess, 'run', lambda *_a, **_k: SimpleNamespace(returncode=0, stdout='git@github.com:Owner/Repo.git\n'))
    assert sync.identity('/repo') == 'owner/repo'
    monkeypatch.setattr(sync.subprocess, 'run', lambda *_a, **_k: SimpleNamespace(returncode=1, stdout=''))
    assert sync.identity('/repo') is None
    doc = saved(sync)
    source = tmp_path / 'config'; source.write_text('old')
    state = {'workflow_hash': sync.digest(doc), 'context': {'settings_source': {'path': str(source), 'hash': sync.digest('old')}}}
    assert sync.current(state)
    source.write_text('new')
    assert not sync.current(state)


def test_sync_cli_and_readonly_status(sync, t_mod, monkeypatch, capsys):
    doc = saved(sync)
    helper = sync.workflow()
    helper['propagation'] = lambda: vars(sync)
    monkeypatch.setattr(t_mod, '_workflow', lambda: helper)
    cfg = SimpleNamespace(repos={}, hosts={})
    parser = t_mod.build_parser()
    args = parser.parse_args(['config', 'workflow', 'sync'])
    assert t_mod.cmd_config(cfg, args) == 0
    assert 'unverified' in capsys.readouterr().out
    monkeypatch.setattr(sync, 'retry', lambda: pytest.fail('list retried'))
    monkeypatch.setattr(sync, 'Proxy', lambda: pytest.fail('list connected'))
    args = parser.parse_args(['config', 'workflow', 'list', '--json'])
    assert t_mod.cmd_config(cfg, args) == 0
    assert json.loads(capsys.readouterr().out)['propagation']['revision'] == 1
    # Raw instructions contain only editable prose, without propagation status.
    args = parser.parse_args(['config', 'workflow', 'instructions', 'show'])
    assert t_mod.cmd_config(cfg, args) == 0
    assert capsys.readouterr().out == helper['instructions'](doc)


def websocket_process(sync, monkeypatch, mode):
    import sys
    original = sync.subprocess.Popen
    processes, requested = [], []
    def launch(argv, **kwargs):
        requested.append(argv)
        assert not any(key.startswith('COV_CORE_') for key in kwargs['env'])
        process = original([sys.executable, '-u', str(ROOT / 'tests/_websocket_peer.py'), mode], **kwargs)
        processes.append(process)
        return process
    monkeypatch.setattr(sync.subprocess, 'Popen', launch)
    return processes, requested


def test_proxy_supported_wire_protocol(sync, monkeypatch):
    processes, requested = websocket_process(sync, monkeypatch, 'success')
    monkeypatch.setenv('COV_CORE_TEST', 'strip')
    with sync.Proxy() as proxy:
        assert proxy.call('thread/loaded/list', {'limit': 50}) == {'data': [], 'nextCursor': None}
    assert requested == [['codex', 'app-server', 'proxy']]
    assert processes[0].poll() is not None


@pytest.mark.parametrize('mode,match', [
    ('timeout', 'timed out'), ('disconnect', 'disconnected'),
    ('too-large', 'too large'), ('bad-json', 'Expecting'), ('refused', 'refused'),
    ('http-invalid', 'upgrade'), ('http-large', 'headers too large'), ('bad-utf8', 'decode'),
])
def test_proxy_failures_close_process(sync, monkeypatch, mode, match):
    processes, _ = websocket_process(sync, monkeypatch, mode)
    monkeypatch.setattr(sync, 'TIMEOUT', 0.3)
    with pytest.raises(ValueError, match=match):
        with sync.Proxy():
            pass
    assert processes[0].poll() is not None
    assert processes[0].stdin.closed and processes[0].stdout.closed


def test_proxy_kills_unresponsive_child(sync, monkeypatch):
    process = SimpleNamespace(stdin=SimpleNamespace(close=lambda: None), stdout=SimpleNamespace(close=lambda: None))
    calls = []
    process.terminate = lambda: calls.append('terminate')
    process.kill = lambda: calls.append('kill')
    def wait(**kwargs):
        if kwargs: raise sync.subprocess.TimeoutExpired('proxy', 1)
        calls.append('wait')
    process.wait = wait
    proxy = sync.Proxy(); proxy.process = process
    proxy.__exit__()
    assert calls == ['terminate', 'kill', 'wait']


def test_postpublication_failure_retains_saved_files(sync, monkeypatch, capsys):
    w = sync.workflow()
    doc = w['defaults'](); doc['workflows']['standard']['instructions'] = 'published'
    functions = w['save'].__globals__
    def retry():
        # Publication includes durable intent; runtime work holds neither lock.
        assert w['load']()[0] == doc
        assert sync.status()['revision'] == 1
        with open(w['config_path']().with_suffix('.lock'), 'r+') as stream:
            import fcntl
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with sync.locked(blocking=False) as acquired:
            assert acquired
        raise OSError('runtime unavailable')
    monkeypatch.setattr(sync, 'retry', retry)
    monkeypatch.setitem(functions, 'propagation', lambda: vars(sync))
    w['save'](doc, None)
    assert 'preferences saved; propagation pending' in capsys.readouterr().err
    assert w['load']()[0] == doc


def test_failed_intent_publication_rolls_back_preferences(sync, monkeypatch):
    w = sync.workflow()
    doc = w['defaults'](); doc['workflows']['standard']['instructions'] = 'not published'
    original = os.replace
    def replace(source, target):
        if Path(target) == sync.state_path():
            raise OSError('journal unavailable')
        return original(source, target)
    monkeypatch.setattr(os, 'replace', replace)
    with pytest.raises(OSError, match='journal unavailable'):
        w['save'](doc, None)
    assert not w['config_path']().exists()
    assert not sync.state_path().exists()
    assert not (Path(os.environ['CODEX_HOME']) / 'AGENTS.md').exists()


def test_missing_journal_retry_does_not_connect(sync, monkeypatch):
    monkeypatch.setattr(sync, 'Proxy', lambda: pytest.fail('connected without pending settings'))
    assert sync.retry()['revision'] == 0


def test_remote_unsupported_bad_reply_and_stale(sync, monkeypatch):
    doc = saved(sync)
    state = {'context': {'hosts': {'a': 'host'}}, 'portable': {}, 'settings': {},
             'workflow_hash': sync.digest(doc), 'scopes': [], 'hosts': {}, 'remote_scopes': {'host': [None]}}
    monkeypatch.setattr(sync, 'portable', lambda *_: ({}, 1))
    sync.remote(state)
    assert 'no portable' in state['hosts']['host']
    monkeypatch.setattr(sync, 'portable', lambda *_: ({'version': 1}, 1))
    monkeypatch.setattr(sync.subprocess, 'run', lambda *_a, **_k: SimpleNamespace(returncode=1, stdout=''))
    sync.remote(state)
    assert 'unreachable' in state['hosts']['host']
    monkeypatch.setattr(sync.subprocess, 'run', lambda *_a, **_k: SimpleNamespace(returncode=0, stdout='{}'))
    sync.remote(state)
    assert 'unreachable' in state['hosts']['host']
    monkeypatch.setattr(sync.subprocess, 'run', lambda *_a, **_k: SimpleNamespace(returncode=0, stdout='{"summary":"ok"}'))
    sync.remote(state)
    assert 'local identities unverified' in state['hosts']['host']
    monkeypatch.setattr(sync, 'current', lambda _: False)
    monkeypatch.setattr(sync.subprocess, 'run', lambda *_a, **_k: pytest.fail('stale SSH'))
    sync.remote(state)


def test_receiving_never_fans_out_or_overwrites_shared_names(sync, monkeypatch, tmp_path):
    doc = saved(sync)
    doc['workflows']['standard']['instructions'] = 'remote global remains'
    w = sync.workflow(); w['save'](doc, w['snapshot'](w['config_path']()), propagate=False)
    # Simulate previous local mutations on a host that also has registered peers.
    with sync.locked():
        sync.write({'revision': 1, 'runtime': 'old', 'context': {'hosts': {'peer': 'other'}},
                    'workflow_hash': sync.digest(doc)})
    incoming = w['defaults'](); incoming['workflows']['standard']['instructions'] = 'scope only'
    incoming['repositories']['/source'] = {'workflow': 'standard'}
    monkeypatch.setattr(sync, 'identity', lambda _: 'owner/repo')
    payload, _ = sync.portable(incoming, ['/source'])
    monkeypatch.setattr(sync.subprocess, 'run', lambda *_a, **_k: pytest.fail('receiver fanned out'))
    sync.receive(payload, {'repo': str(tmp_path)})
    actual = w['load']()[0]
    assert w['effective'](actual)['instructions'] == 'remote global remains'
    assert w['effective'](actual, str(tmp_path))['instructions'] == 'scope only'
    assert actual['repositories'][str(tmp_path)]['inline']['instructions'] == 'scope only'
    assert not sync.status()['context']['hosts']


def test_acknowledged_settings_never_replay_on_workflow_edit(sync, monkeypatch):
    doc = saved(sync)
    calls = fake_proxy(sync, monkeypatch, {None: {'data': ['root']}}, {'root': {'source': 'cli', 'model': 'old'}})
    state = sync.changed(doc, doc, settings={'model': 'new'})
    assert state['settings'] == {}
    newer = copy.deepcopy(doc); newer['workflows']['standard']['instructions'] = 'new text'
    w = sync.workflow(); w['save'](newer, w['snapshot'](w['config_path']()), propagate=False)
    sync.changed(doc, newer)
    sync.retry(boundary=True)
    assert len([m for m, _ in calls if m == 'thread/settings/update']) == 1
    assert sync.status()['threads']['root'] == 'accepted for subsequent turns'


def test_partial_settings_preserve_ack_and_source_guard(sync, monkeypatch, tmp_path):
    doc = saved(sync)
    source = tmp_path / 'config'; source.write_text('old')
    calls = fake_proxy(sync, monkeypatch, {None: {'data': ['root', 'bad']}},
                       {'root': {'source': 'cli'}, 'bad': {'source': 'cli'}})
    original_proxy = sync.Proxy
    class FailSecond(original_proxy):
        def call(self, method, params):
            if method == 'thread/read' and params['threadId'] == 'bad': raise ValueError('lost')
            return super().call(method, params)
    monkeypatch.setattr(sync, 'Proxy', FailSecond)
    state = sync.changed(doc, doc, settings={'model': 'new'},
                         context={'settings_source': {'path': str(source), 'hash': sync.digest('old')}})
    assert state['settings'] == {'model': 'new'} and 'root' in state['threads']
    newer = copy.deepcopy(doc); newer['workflows']['standard']['instructions'] = 'new text'
    w = sync.workflow(); w['save'](newer, w['snapshot'](w['config_path']()), propagate=False)
    source.write_text('user changed defaults')
    sync.changed(doc, newer, context={'hosts': {}})
    assert sync.status()['context']['settings_source']['hash'] == sync.digest('old')
    assert len([m for m, _ in calls if m == 'thread/settings/update']) == 1


def test_completed_host_global_scope_is_not_replayed_for_scoped_edit(sync, monkeypatch):
    doc = saved(sync)
    initial = copy.deepcopy(doc)
    doc['workflows']['standard']['instructions'] = 'global'
    w = sync.workflow(); w['save'](doc, w['snapshot'](w['config_path']()), propagate=False)
    payloads = []
    def run(_argv, **kwargs):
        payloads.append(json.loads(kwargs['input']))
        return SimpleNamespace(returncode=0, stdout='{"summary":"published","pending_scopes":0}')
    monkeypatch.setattr(sync.subprocess, 'run', run)
    monkeypatch.setattr(sync, 'identity', lambda _: 'owner/repo')
    context = {'hosts': {'remote': 'host'}}
    sync.changed(initial, doc, context=context)
    assert payloads[-1]['global'] is True
    assert sync.status()['remote_scopes'] == {}
    scoped = copy.deepcopy(doc)
    scoped['repositories']['/repo'] = {'inline': {**copy.deepcopy(w['DEFAULT']), 'instructions': 'only repo'}}
    w['save'](scoped, w['snapshot'](w['config_path']()), propagate=False)
    sync.changed(doc, scoped, context=context)
    assert payloads[-1]['global'] is False
    assert list(payloads[-1]['repositories']) == ['owner/repo']


def test_boundary_trylock_does_not_wait(sync):
    doc = saved(sync)
    with sync.locked():
        assert sync.retry(boundary=True)['revision'] == 0


def test_remote_identity_scan_advances_across_bounded_retries(sync, monkeypatch):
    saved(sync)
    monkeypatch.setattr(sync, 'identity', lambda path: 'owner/target' if path == '/repo11' else 'owner/other')
    w = sync.workflow(); incoming = w['defaults']()
    incoming['repositories']['/source'] = {'workflow': 'standard'}
    monkeypatch.setattr(sync, 'identity', lambda path: 'owner/target' if path in ('/source', '/repo11') else 'owner/other')
    payload, _ = sync.portable(incoming, ['/source'])
    payload['request_id'] = 'a' * 32 + ':1'
    repos = {str(i): '/repo' + str(i) for i in range(12)}
    assert sync.receive(payload, repos)['pending_scopes'] == 1
    assert sync.status()['identity_scan']['cursor'] == 8
    assert sync.receive(payload, repos)['pending_scopes'] == 0
    assert '/repo11' in w['load']()[0]['repositories']
    # Duplicate delivered requests cannot replay settings after a manual override.
    monkeypatch.setattr(sync, 'changed', lambda *_a, **_k: pytest.fail('duplicate delivery replayed'))
    assert sync.receive(payload, repos)['summary'].startswith('already received')


def test_portable_scope_cap_and_timeout_remain_pending(sync, monkeypatch):
    doc = saved(sync)
    monkeypatch.setattr(sync, 'MAX_SCOPES', 1)
    monkeypatch.setattr(sync, 'identity', lambda _: 'owner/repo')
    assert sync.portable(doc, ['/one', '/two'])[1] == 1
    def timeout(_): raise sync.subprocess.TimeoutExpired('git', 0.1)
    monkeypatch.setattr(sync, 'identity', timeout)
    assert sync.portable(doc, ['/one'])[1] == 1


def test_sender_scope_batches_and_hosts_advance_without_starvation(sync, monkeypatch):
    doc = saved(sync)
    for i in range(30):
        doc['repositories']['/repo' + str(i)] = {'inline': {**copy.deepcopy(sync.workflow()['DEFAULT']), 'instructions': str(i)}}
    w = sync.workflow(); w['save'](doc, w['snapshot'](w['config_path']()), propagate=False)
    monkeypatch.setattr(sync, 'identity', lambda path: 'owner/' + Path(path).name)
    monkeypatch.setattr(sync, 'MAX_HOSTS', 2)
    deliveries = []
    def run(argv, **kwargs):
        target = argv[-2]
        deliveries.append((target, json.loads(kwargs['input'])))
        if target == 'a-bad': raise OSError('unreachable')
        return SimpleNamespace(returncode=0, stdout='{"summary":"published","pending_scopes":0}')
    monkeypatch.setattr(sync.subprocess, 'run', run)
    context = {'hosts': {'a': 'a-bad', 'b': 'b', 'c': 'c', 'd': 'd'}}
    sync.changed(w['defaults'](), doc, context=context)
    for _ in range(8): sync.retry()
    assert {target for target, _ in deliveries} == {'a-bad', 'b', 'c', 'd'}
    assert set(sync.status()['remote_scopes']) == {'a-bad'}
    for host in ('b', 'c', 'd'):
        entries = set().union(*(set(payload['repositories']) for target, payload in deliveries if target == host))
        assert len(entries) == 30


def test_receive_global_default_uses_isolated_name(sync, monkeypatch):
    doc = saved(sync)
    doc['repositories']['/unrelated'] = {'workflow': 'standard'}
    w = sync.workflow(); w['save'](doc, w['snapshot'](w['config_path']()), propagate=False)
    incoming = w['defaults'](); incoming['workflows']['standard']['instructions'] = 'incoming global'
    payload, _ = sync.portable(incoming, [None])
    sync.receive(payload, {})
    actual = w['load']()[0]
    assert actual['default'].startswith('synced-')
    assert w['effective'](actual)['instructions'] == 'incoming global'
    assert w['effective'](actual, '/unrelated')['instructions'] == w['issue_policy']()


def test_multiple_scope_batches_apply_end_to_end(sync, monkeypatch, tmp_path):
    doc = saved(sync)
    for i in range(sync.MAX_SCOPES + 5):
        doc['repositories']['/source/repo' + str(i)] = {
            'inline': {**copy.deepcopy(sync.workflow()['DEFAULT']), 'instructions': 'policy ' + str(i)}}
    w = sync.workflow(); w['save'](doc, w['snapshot'](w['config_path']()), propagate=False)
    remote_dirs = {}
    for key in ('HOME', 'XDG_CONFIG_HOME', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR'):
        remote_dirs[key] = str(tmp_path / ('remote-' + key))
        Path(remote_dirs[key]).mkdir()
    repos = {str(i): '/destination/repo' + str(i) for i in range(sync.MAX_SCOPES + 5)}
    monkeypatch.setattr(sync, 'identity', lambda path: 'owner/' + Path(path).name)
    deliveries = []
    def run(_argv, **kwargs):
        payload = json.loads(kwargs['input']); deliveries.append(payload)
        with monkeypatch.context() as remote_environment:
            for key, value in remote_dirs.items(): remote_environment.setenv(key, value)
            result = sync.receive(payload, repos)
        return SimpleNamespace(returncode=0, stdout=json.dumps(result))
    monkeypatch.setattr(sync.subprocess, 'run', run)
    sync.changed(w['defaults'](), doc, context={'hosts': {'remote': 'host'}})
    for _ in range(20):
        if not sync.status()['remote_scopes']: break
        sync.retry()
    assert not sync.status()['remote_scopes']
    assert len({sync.digest(item['repositories']) for item in deliveries}) == 2
    with monkeypatch.context() as remote_environment:
        for key, value in remote_dirs.items(): remote_environment.setenv(key, value)
        remote_doc = w['load']()[0]
        assert len(remote_doc['repositories']) == sync.MAX_SCOPES + 5
        for i in range(sync.MAX_SCOPES + 5):
            assert w['effective'](remote_doc, '/destination/repo' + str(i))['instructions'] == 'policy ' + str(i)


def test_bootstrap_rejects_stale_derived_settings(sync):
    text = 'DEV_MODEL[codex]=new\nDEV_EFFORT[codex]=medium\n'
    assert sync.verified_settings(text, {'model': 'stale', 'effort': 'medium'}) == {'effort': 'medium'}
    assert sync.verified_settings('DEV_MODEL[codex]="$MODEL"\n', {'model': 'cached'}) == {}
    assert sync.verified_settings("DEV_MODEL[codex]='unterminated", {'model': 'cached'}) == {}
    assert sync.verified_settings('DEV_MODEL[codex]=cached extra\n', {'model': 'cached'}) == {}


def test_bootstrap_cli_never_attaches_fresh_guard_to_stale_model(sync, t_mod, monkeypatch, tmp_path, capsys):
    saved(sync)
    source = tmp_path / 'local.zsh'; source.write_text('DEV_MODEL[codex]=new-source-model\n')
    cfg = SimpleNamespace(repos={}, hosts={}, models={'codex': 'stale-cache-model'}, efforts={}, local_rc=str(source))
    helper = sync.workflow(); helper['propagation'] = lambda: vars(sync)
    monkeypatch.setattr(t_mod, '_workflow', lambda: helper)
    monkeypatch.setattr(sync, 'Proxy', lambda: pytest.fail('unverified model applied'))
    args = t_mod.build_parser().parse_args(['config', 'workflow', 'sync'])
    assert t_mod.cmd_config(cfg, args) == 0
    state = sync.status()
    assert state['settings'] == {}
    assert 'settings_source' not in state['context']
    assert state['context']['unverified_main_defaults'] == ['model']
    assert 'pending: bootstrap' in capsys.readouterr().out


@pytest.mark.parametrize('suffix', [' DEV_MODEL[codex]=new', 'export DEV_MODEL[codex]=new',
                                     'if true; then\nDEV_MODEL[codex]=new\nfi', 'unset DEV_MODEL'])
def test_bootstrap_rejects_unrecognized_later_source_writes(sync, suffix):
    assert sync.verified_settings('DEV_MODEL[codex]=old\n' + suffix, {'model': 'old'}) == {}


def test_native_defaults_preserve_unrelated_tier_permissions_plan_and_profiles(sync):
    import tomllib
    path = Path(os.environ['CODEX_HOME']) / 'config.toml'
    original = 'model = "old" # selected\nmodel_reasoning_effort = "high"\nservice_tier = "ultrafast"\nplan_mode_reasoning_effort = "high"\napproval_policy = "never"\n[profiles.local]\nmodel = "profile-model"\n'
    path.write_text(original)
    extra, pending = sync.native_defaults({'model': 'new', 'effort': 'medium'})
    assert pending is None
    before = tomllib.loads(original); after = tomllib.loads(extra[0][2])
    expected = copy.deepcopy(before); expected.update(model='new', model_reasoning_effort='medium')
    assert after == expected and '# selected' in extra[0][2]
    sync.workflow()['publish'](extra)
    cleared, pending = sync.native_defaults({'model': None})
    assert pending is None and 'model' not in tomllib.loads(cleared[0][2])
    assert tomllib.loads(cleared[0][2])['profiles']['local']['model'] == 'profile-model'


def test_native_defaults_unsupported_ownership_and_toml_stay_pending(sync, monkeypatch):
    path = Path(os.environ['CODEX_HOME']) / 'config.toml'
    path.write_text('"model" = "quoted key"\n')
    assert sync.native_defaults({'model': 'new'})[1]
    path.write_text('invalid = [')
    assert sync.native_defaults({'model': 'new'})[1]
    path.write_text('model="old"\n')
    monkeypatch.setattr(sync.os, 'getuid', lambda: path.stat().st_uid + 1)
    assert sync.native_defaults({'model': 'new'})[1]
    assert path.read_text() == 'model="old"\n'


def test_remote_explicit_defaults_atomic_with_workflow_and_future_launch_source(sync, t_mod, monkeypatch, tmp_path):
    import tomllib
    doc = saved(sync)
    path = tmp_path / 'authoritative.zsh'
    path.write_text('DEV_FAST[codex]=ultrafast\nOTHER=value\n')
    cfg = t_mod.Config(); cfg.local_rc = str(path)
    helper = sync.workflow(); helper['propagation'] = lambda: vars(sync)
    monkeypatch.setattr(t_mod, '_workflow', lambda: helper)
    monkeypatch.setattr(sync, 'Proxy', lambda: (_ for _ in ()).throw(OSError('offline')))
    native = Path(os.environ['CODEX_HOME']) / 'config.toml'
    native.write_text('service_tier="ultrafast"\nplan_mode_reasoning_effort="high"\n')
    payload, _ = sync.portable(doc, [None]); payload['settings'] = {'model': 'new-main', 'effort': 'medium'}
    result = sync.receive(payload, {}, settings_writer=lambda settings: t_mod._workflow_launch_settings(cfg, settings))
    assert not result['defaults_pending']
    text = path.read_text()
    assert 'DEV_MODEL[codex]=new-main' in text and 'DEV_EFFORT[codex]=medium' in text
    assert 'DEV_FAST[codex]=ultrafast' in text and 'OTHER=value' in text
    parsed = tomllib.loads(native.read_text())
    assert parsed == {'service_tier': 'ultrafast', 'plan_mode_reasoning_effort': 'high',
                      'model': 'new-main', 'model_reasoning_effort': 'medium'}
    assert sync.status()['settings'] == {'model': 'new-main', 'effort': 'medium'}
    assert sync.status()['context']['hosts'] == {}


def test_remote_unavailable_defaults_writer_remains_pending(sync):
    doc = saved(sync)
    payload, _ = sync.portable(doc, [None]); payload['settings'] = {'model': 'new-main'}
    result = sync.receive(payload, {})
    assert result['defaults_pending'] and result['summary'].startswith('pending:')
    assert not sync.status().get('settings')
    assert not (Path(os.environ['CODEX_HOME']) / 'config.toml').exists()


def test_remote_default_publication_rolls_back_all_authoritative_files(sync, t_mod, monkeypatch, tmp_path):
    doc = saved(sync)
    source = tmp_path / 'local.zsh'; source.write_text('DEV_MODEL[codex]=old\\n'.replace('\\n', '\n'))
    cfg = t_mod.Config(); cfg.local_rc = str(source)
    helper = sync.workflow(); helper['propagation'] = lambda: vars(sync)
    monkeypatch.setattr(t_mod, '_workflow', lambda: helper)
    native = Path(os.environ['CODEX_HOME']) / 'config.toml'; native.write_text('model="old"\nservice_tier="ultrafast"\n')
    source_before, native_before = source.read_text(), native.read_text()
    incoming = copy.deepcopy(doc); incoming['workflows']['standard']['instructions'] = 'new instructions'
    payload, _ = sync.portable(incoming, [None]); payload['settings'] = {'model': 'new-main'}
    replace = os.replace
    def fail_journal(pending, target):
        if Path(target) == sync.state_path() and 'DEV_MODEL[codex]=new-main' in source.read_text():
            raise OSError('intent commit unavailable')
        return replace(pending, target)
    monkeypatch.setattr(os, 'replace', fail_journal)
    with pytest.raises(OSError, match='intent commit unavailable'):
        sync.receive(payload, {}, settings_writer=lambda settings: t_mod._workflow_launch_settings(cfg, settings))
    assert source.read_text() == source_before and native.read_text() == native_before
    assert helper['load']()[0] == doc
    assert sync.status()['revision'] == 0


def test_native_defaults_without_tomllib_use_private_fallback(sync, monkeypatch):
    import builtins
    original = builtins.__import__
    def import_module(name, *args, **kwargs):
        if name == 'tomllib': raise ImportError('not available')
        return original(name, *args, **kwargs)
    monkeypatch.setattr(builtins, '__import__', import_module)
    extra, pending = sync.native_defaults({'model': 'new'})
    assert extra and pending is None


def test_remote_partial_scope_retry_does_not_rewrite_accepted_launch_defaults(sync, t_mod, monkeypatch, tmp_path):
    doc = saved(sync)
    source = tmp_path / 'local.zsh'; source.write_text('DEV_MODEL[codex]=old\n')
    cfg = t_mod.Config(); cfg.local_rc = str(source)
    helper = sync.workflow(); helper['propagation'] = lambda: vars(sync)
    monkeypatch.setattr(t_mod, '_workflow', lambda: helper)
    monkeypatch.setattr(sync, 'Proxy', lambda: (_ for _ in ()).throw(OSError('offline')))
    monkeypatch.setattr(sync, 'identity', lambda path: 'owner/repo')
    incoming = copy.deepcopy(doc); incoming['repositories']['/source'] = {'workflow': 'standard'}
    payload, _ = sync.portable(incoming, ['/source'])
    payload.update(settings={'model': 'received-main'}, request_id='b' * 32 + ':1')
    writer = lambda settings: t_mod._workflow_launch_settings(cfg, settings)
    first = sync.receive(payload, {}, settings_writer=writer)
    assert first['pending_scopes'] == 1 and not first['defaults_pending']
    native = Path(os.environ['CODEX_HOME']) / 'config.toml'
    source.write_text('DEV_MODEL[codex]=deliberate-choice\n')
    native.write_text('model="deliberate-choice"\nservice_tier="ultrafast"\n')
    second = sync.receive(payload, {'repo': str(tmp_path)},
                          settings_writer=lambda _: pytest.fail('accepted defaults rewritten'))
    assert second['pending_scopes'] == 0
    assert source.read_text() == 'DEV_MODEL[codex]=deliberate-choice\n'
    assert native.read_text() == 'model="deliberate-choice"\nservice_tier="ultrafast"\n'
    assert str(tmp_path) in helper['load']()[0]['repositories']


@pytest.mark.parametrize('prose', ['', 'My deliberate issue policy replacement.'])
def test_received_workflow_does_not_restore_removed_issue_policy(sync, monkeypatch, tmp_path, prose):
    doc = saved(sync)
    doc['workflows']['standard']['instructions'] = prose
    doc['workflows']['named'] = copy.deepcopy(doc['workflows']['standard'])
    doc['repositories']['/source/repo'] = {'workflow': 'named'}
    monkeypatch.setattr(sync, 'identity', lambda _: 'owner/repo')
    payload, missing = sync.portable(doc, [None, '/source/repo'])
    assert not missing
    sync.receive(payload, {'repo': str(tmp_path)})
    w = sync.workflow()
    actual = w['load']()[0]
    assert w['instructions'](actual) == prose
    assert w['instructions'](actual, str(tmp_path)) == prose
    assert w['issue_policy']() not in (Path(os.environ['CODEX_HOME']) / 'AGENTS.md').read_text()


@pytest.mark.parametrize('version', [1, 2])
def test_issue_policy_migration_queues_effective_scopes_and_refreshes_hash(sync, tmp_path, version):
    w = sync.workflow()
    policy = copy.deepcopy(w['DEFAULT'])
    repo = str(tmp_path / 'repo')
    old = ({'version': 1, 'global': policy, 'repositories': {repo: copy.deepcopy(policy)}}
           if version == 1 else {'version': 2, 'default': 'standard',
                 'workflows': {'standard': policy}, 'repositories': {repo: {'workflow': 'standard'}}})
    w['config_path']().parent.mkdir(parents=True, exist_ok=True)
    w['config_path']().write_text(json.dumps(old))
    w['sync']()
    state = sync.status()
    assert state['revision'] == 1
    assert state['scopes'] == [None, repo]
    assert state['workflow_hash'] == sync.digest(w['load']()[0])
    assert state['context'] == {} and not state['settings']
    w['sync']()
    assert sync.status()['revision'] == 1
def test_proxy_helper_setup_failure_reaps_child(sync, monkeypatch):
    processes, _ = websocket_process(sync, monkeypatch, 'timeout')
    original = sync.runpy.run_path
    def load(path, *args, **kwargs):
        if str(path).endswith('t_codex_socket.py'): raise ValueError('helper unavailable')
        return original(path, *args, **kwargs)
    monkeypatch.setattr(sync.runpy, 'run_path', load)
    with pytest.raises(ValueError, match='helper unavailable'):
        with sync.Proxy(): pass
    assert processes[0].poll() is not None and processes[0].stdin.closed and processes[0].stdout.closed


def test_guarded_resume_retains_latest_explicit_intent_after_live_ack(sync, monkeypatch, tmp_path):
    sid = '11111111-1111-1111-1111-111111111111'
    doc = saved(sync)
    fake_proxy(sync, monkeypatch, {None:{'data':['root']}}, {'root':{'source':'cli'}})
    sync.changed(doc, doc, settings={'model':'new-main','effort':'medium'})
    assert sync.status()['settings'] == {}
    before = sync.state_path().read_bytes()
    assert sync.resume_settings(sid) == ['--model','new-main','-c','model_reasoning_effort="medium"']
    assert sync.state_path().read_bytes() == before
    sync.changed(doc, doc, settings={'model':'newest'})
    assert sync.resume_settings(sid)[:2] == ['--model','newest']
    source = tmp_path / 'config'; source.write_text('source')
    state = sync.status(); state['context']['settings_source']={'path':str(source),'hash':sync.digest('source')};sync.write(state)
    source.write_text('deliberately changed')
    assert sync.resume_settings(sid) == []
    assert sync.resume_settings('bad; touch secret') == []


def test_resume_scope_exclusion_worktree_and_child_are_preserved(sync, monkeypatch, tmp_path):
    import sqlite3
    sid = '22222222-2222-2222-2222-222222222222'
    doc = saved(sync)
    repo = tmp_path / 'repo';(repo / '.git').mkdir(parents=True)
    doc['repositories'][str(repo)]={'excluded':True}
    w = sync.workflow();w['save'](doc,w['snapshot'](w['config_path']()),propagate=False)
    monkeypatch.setattr(sync,'Proxy',lambda: (_ for _ in ()).throw(OSError('offline')))
    sync.changed(doc,doc,settings={'model':'new-main'})
    monkeypatch.setattr(sync,'git',lambda *_:str(repo / '.git'))
    assert sync.resume_settings(sid,str(tmp_path / 'worktree')) == []
    doc['repositories'].clear();w['save'](doc,w['snapshot'](w['config_path']()),propagate=False)
    sync.changed(doc,doc)
    db = Path(os.environ['CODEX_HOME']) / 'state_5.sqlite'
    with sqlite3.connect(db) as conn:
        conn.execute('create table threads(id text, source text)');conn.execute('insert into threads values(?,?)',(sid,'{"subagent":{}}'))
    assert sync.resume_settings(sid) == []


def config_transaction(sync, t_mod, monkeypatch, tmp_path):
    doc = saved(sync)
    source = tmp_path / 'local.zsh'
    source.write_text('DEV_MODEL[codex]=preferred\nDEV_EFFORT[codex]=medium\nDEV_FAST[codex]=ultrafast\nDEV_HANDOFF[codex]=native\n')
    monkeypatch.setenv('T_LOCAL_RC',str(source));monkeypatch.setenv('T_SETUP_SHIM','1')
    cache=tmp_path/'cache.sh';cache.write_text('')
    monkeypatch.setattr(t_mod,'CONFIG',str(cache))
    cfg=t_mod.Config();cfg.models={'codex':'preferred'};cfg.efforts={'codex':'medium'}
    cfg.fast={'codex':'ultrafast'};cfg.handoff={'codex':'native'}
    calls=fake_proxy(sync,monkeypatch,{None:{'data':['root']}},{'root':{'source':'cli','model':'old','reasoningEffort':'medium'}})
    sync.changed(doc,doc,settings={'model':'preferred','effort':'medium'},
                 context={'settings_source':{'path':str(source),'hash':sync.digest(source.read_text())}})
    helper=sync.workflow();helper['propagation']=lambda:vars(sync)
    monkeypatch.setitem(helper['save'].__globals__,'propagation',lambda:vars(sync))
    monkeypatch.setattr(t_mod,'_workflow',lambda:helper)
    monkeypatch.setattr(t_mod.sys.stdin,'isatty',lambda:True)
    monkeypatch.setattr(t_mod.sys.stdout,'isatty',lambda:True)
    monkeypatch.setattr(t_mod,'zsh_capture',lambda *_a,**_k:'')
    monkeypatch.setattr(t_mod,'_host_verify_offer',lambda *_:None)
    return cfg,source,calls


def test_known_unrelated_config_transaction_rebinds_checkpoint_without_model_replay(sync,t_mod,monkeypatch,tmp_path):
    cfg,source,calls=config_transaction(sync,t_mod,monkeypatch,tmp_path)
    initial_ack=copy.deepcopy(sync.status()['threads'])
    edited=copy.deepcopy(cfg);edited.handoff['codex']='t'
    monkeypatch.setattr(t_mod,'_config_wizard',lambda _:edited)
    args=t_mod.build_parser().parse_args(['config'])
    assert t_mod.cmd_config(cfg,args)==0
    state=sync.status()
    assert state['context']['settings_source']['hash']==sync.digest(source.read_text())
    assert state['threads']==initial_ack and state['settings']=={}
    assert len([m for m,_ in calls if m=='thread/settings/update'])==1
    assert sync.resume_settings('66666666-6666-6666-6666-666666666666')[:2]==['--model','preferred']
    assert 'DEV_FAST[codex]=ultrafast' in source.read_text()
    # A later real model edit supersedes intent and legitimately resets ACKs.
    newest=copy.deepcopy(edited);newest.models['codex']='new-main'
    monkeypatch.setattr(t_mod,'_config_wizard',lambda _:newest)
    assert t_mod.cmd_config(edited,args)==0
    assert sync.status()['main_settings']=={'model':'new-main','effort':'medium'}
    assert [p for m,p in calls if m=='thread/settings/update'][-1]=={'threadId':'root','model':'new-main'}


def test_external_source_edit_is_not_rebound_by_unrelated_config_transaction(sync,t_mod,monkeypatch,tmp_path):
    cfg,source,calls=config_transaction(sync,t_mod,monkeypatch,tmp_path)
    original_guard=copy.deepcopy(sync.status()['context']['settings_source'])
    source.write_text(source.read_text().replace('=preferred','=external-choice'))
    edited=copy.deepcopy(cfg);edited.handoff['codex']='t'
    monkeypatch.setattr(t_mod,'_config_wizard',lambda _:edited)
    assert t_mod.cmd_config(cfg,t_mod.build_parser().parse_args(['config']))==0
    assert sync.status()['context']['settings_source']==original_guard
    assert sync.resume_settings('66666666-6666-6666-6666-666666666666')==[]
    assert len([m for m,_ in calls if m=='thread/settings/update'])==1


def test_source_rebind_rolls_back_with_failed_config_publication(sync,t_mod,monkeypatch,tmp_path):
    cfg,source,_=config_transaction(sync,t_mod,monkeypatch,tmp_path)
    before=source.read_text();guard=copy.deepcopy(sync.status()['context']['settings_source'])
    edited=copy.deepcopy(cfg);edited.handoff['codex']='t'
    monkeypatch.setattr(t_mod,'_config_wizard',lambda _:edited)
    replace=os.replace
    def fail(pending,target):
        if Path(target)==sync.state_path() and source.read_text()!=before:
            raise OSError('intent commit unavailable')
        return replace(pending,target)
    monkeypatch.setattr(os,'replace',fail)
    assert t_mod.cmd_config(cfg,t_mod.build_parser().parse_args(['config']))==1
    assert source.read_text()==before and sync.status()['context']['settings_source']==guard
    assert sync.resume_settings('66666666-6666-6666-6666-666666666666')[:2]==['--model','preferred']
