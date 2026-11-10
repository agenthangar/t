"""Discovery adapters use the one service boundary, never cache storage."""
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from conftest import load_script, REPO_ROOT


@pytest.fixture
def adapter():
    return load_script(REPO_ROOT / 'libexec/t_session_discovery.py', 'session_discovery_test')


def view():
    return {'hosts': [{'host': 'local', 'target': 'local', 'checked': 10000, 'stale': False,
                       'refreshing': False, 'error': '', 'retry_at': 13600,
                       'value': {'rows': 'a\t/work\tapi-1\tdetached\tactive\tTask\tcodex\n',
                                 'resume': ['10000\tapi\t1\ta\t/work\tNov 10\tTask\tlocal\tcodex\t-\torg/api\tNONE']}}],
            'loading': False}


def test_config_and_requested_fields_pass_through_single_api(adapter, monkeypatch):
    calls = []
    monkeypatch.setattr(adapter, 'request', lambda op, payload: calls.append((op, payload)))
    cfg = SimpleNamespace(repos={'api': '/work'}, hosts={'mini': 'user@mini'}, worktree={})
    adapter.discover(cfg, fields=['rows'])
    assert calls[-1][0] == 'discover'
    assert calls[-1][1]['fields'] == ['rows']
    assert [h['target'] for h in calls[-1][1]['hosts']] == ['local', 'user@mini']
    adapter.discover(cfg, local=True)
    assert len(calls[-1][1]['hosts']) == 1
    adapter.discover(cfg, host='mini')
    assert calls[-1][1]['hosts'][0]['target'] == 'user@mini'
    adapter.event(['user@mini'], row='verified')
    assert calls[-1] == ('event', {'targets': ['user@mini'], 'event': 'changed', 'row': 'verified'})


def test_rows_candidates_and_freshness_are_pure(adapter):
    snapshot = view()
    assert adapter.rows(snapshot).startswith('local\ta\t')
    assert adapter.rows(snapshot, prefixed=False).startswith('a\t/work')
    assert list(adapter.candidates(snapshot, ['api']))
    assert list(adapter.candidates(snapshot, ['org/api'], '1'))
    assert not list(adapter.candidates(snapshot, ['wrong']))
    assert not list(adapter.candidates(snapshot, ['api'], '2'))
    snapshot['hosts'].append(copy.deepcopy(snapshot['hosts'][0]))
    assert len(list(adapter.candidates(snapshot, ['--all']))) == 1
    assert 'value' not in adapter.freshness(snapshot)['hosts'][0]
    snapshot['hosts'][0]['value']['resume'].append('bad')
    assert len(list(adapter.candidates(snapshot, []))) == 1


def test_announcements_distinguish_warming_empty_offline(adapter, capsys):
    adapter.announce({'unavailable': True})
    assert 'unavailable' in capsys.readouterr().err
    adapter.announce({'loading': True})
    assert 'refreshing' in capsys.readouterr().err
    data = view()
    data['hosts'][0].update(checked=0, error='SSH failure', refreshing=True)
    adapter.announce(data)
    text = capsys.readouterr().err
    assert 'not yet checked' in text and 'unavailable' in text and 'retaining' in text
    data['hosts'][0].update(checked=10000, error='', refreshing=False)
    adapter.announce(data)
    assert 'checked' in capsys.readouterr().err


def test_snapshot_store_merges_newer_metadata_and_keeps_sources(adapter):
    def host(name, updated):
        return {'host': name, 'value': {'catalog': {'conversations': [
            dict(repo='org/api', agent='codex', sid='a', updated=updated, title=name,
                 sources=[{'host': 'local', 'path': '/session'}], associations=[{'host': 'local', 'workspace': '/work'}])]}}}
    data = {'hosts': [host('local', 2), host('mini', 1), host('studio', 3)]}
    store = adapter.SnapshotStore(data)
    rows = store.records()
    assert len(rows) == 1 and rows[0]['title'] == 'studio'
    assert {s['host'] for s in rows[0]['sources']} == {'local', 'mini', 'studio'}
    rows[0]['title'] = 'caller mutation'
    assert store.records()[0]['title'] == 'studio'
    store.close()


def test_mcp_warm_context_never_scans_and_uses_memory_generation(t_mod, monkeypatch):
    monkeypatch.delenv('T_DISCOVERY_COLLECTOR')
    current = {'recs': [], 'live': {}, 'idle': [], 'live_rows': [], 'cache_root': None}
    data = {'hosts': [{'value': {'mcp': current}}], 'loading': False}
    helper = {'discover': lambda *a, **kw: copy.deepcopy(data), 'freshness': lambda v: {'loading': v['loading']}}
    monkeypatch.setattr(t_mod, '_session_discovery', lambda: helper)
    monkeypatch.setattr(t_mod, '_live_rows', lambda *a: pytest.fail('live discovery on cache read'))
    monkeypatch.setattr(t_mod, '_mcp_records', lambda: pytest.fail('transcript discovery on cache read'))
    state = {'recs': ['stale process-local cache']}
    assert t_mod._mcp_context(SimpleNamespace(), state)['recs'] == []
    current['recs'] = [{'sid': 'new'}]
    assert t_mod._mcp_context(SimpleNamespace(), state)['recs'] == [{'sid': 'new'}]
    assert t_mod._session_row(SimpleNamespace(), {'sid': 'new', '_cached_row': {'title': 'Cached'}}, {}, 0, this_sid='new')['this_session']
    data['hosts'] = []
    assert t_mod._mcp_context(SimpleNamespace(), state)['recs'] == []


def test_only_service_owns_storage_and_cache_policy():
    service = REPO_ROOT / 'libexec/t_session_cache.py'
    for path in [REPO_ROOT / 'bin/t', REPO_ROOT / 'libexec/t_candidates.py', REPO_ROOT / 'libexec/t_session_discovery.py']:
        text = path.read_text()
        assert 'cache.sqlite3' not in text
        if path.name != 't':
            assert 'sqlite3.connect' not in text
        assert 'TTL = 3600' not in text
    import ast
    tree = ast.parse((REPO_ROOT / 'bin/t').read_text())
    for function in tree.body:
        if isinstance(function, ast.FunctionDef) and function.name in ('_index_load', '_index_save'):
            assert not any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == 'open'
                           for n in ast.walk(function))
    assert 'cache.sqlite3' in service.read_text()


@pytest.fixture
def collector(adapter, monkeypatch):
    class History:
        def __init__(self, cfg, api):
            self.cfg = cfg
            self.store = SimpleNamespace(close=lambda: None)
        def local_catalog(self): return {'version': 1, 'repositories': [], 'conversations': []}
        def import_catalog(self, catalog, host): self.imported = (catalog, host)
    original = adapter.runpy.run_path
    monkeypatch.setattr(adapter.runpy, 'run_path', lambda path, *a, **k:
                        {'HistoryCommand': History} if str(path).endswith('/t_history.py') else original(path, *a, **k))
    monkeypatch.setattr(adapter, '_catalog_candidates', lambda catalog, history, host: [])
    api = {'Config': lambda: SimpleNamespace(repos={}, hosts={}), 'ZSH_PRELUDE': '',
           '_mcp_context': lambda *a: {'recs': [{'sid': 's'}], 'live': {}, 'now': 0, 'cache_root': None},
           '_session_row': lambda *a: {'sid': 's', 'title': 'sample'}}
    return api


def test_local_collector_sources_plugin_and_rejects_failure(adapter, collector, monkeypatch):
    calls = []
    result = SimpleNamespace(returncode=0, stdout='rows', stderr='')
    monkeypatch.setattr(adapter.subprocess, 'run', lambda argv, **kw: (calls.append((argv, kw)), result)[1])
    value = adapter.collect({'target': 'local'}, collector)
    assert value['rows'] == 'rows'
    assert value['mcp']['recs'][0]['_cached_row']['title'] == 'sample'
    assert 'source ' in calls[0][0][-1]
    assert calls[0][1]['env']['T_DISCOVERY_COLLECTOR'] == '1'
    assert adapter.collect({'target': 'local', 'export': True}, collector)['mcp']['recs'] == []
    result.returncode = 1
    with pytest.raises(ValueError, match='local session scan failed'):
        adapter.collect({'target': 'local'}, collector)


def test_remote_transport_failure_never_retries_and_old_protocol_falls_back(adapter, collector, monkeypatch):
    calls = []
    result = SimpleNamespace(returncode=255, stdout='', stderr='banner timed out')
    monkeypatch.setattr(adapter.subprocess, 'run', lambda argv, **kw: (calls.append(argv), result)[1])
    with pytest.raises(ValueError, match='banner timed out'):
        adapter.collect({'target': 'mini'}, collector)
    assert len(calls) == 1 and calls[0][-1].startswith('zsh -lic ')
    replies = iter([SimpleNamespace(returncode=2, stdout='', stderr='invalid choice'),
                    SimpleNamespace(returncode=0, stdout=json.dumps({'rows': '', 'catalog': {}, 'mcp': {'private': 'omit'}}), stderr='')])
    calls.clear()
    monkeypatch.setattr(adapter.subprocess, 'run', lambda argv, **kw: (calls.append(argv), next(replies))[1])
    result = adapter.collect({'target': 'mini'}, collector)
    assert len(calls) == 2 and 'mcp' not in result
    monkeypatch.setattr(adapter.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout='{}', stderr=''))
    with pytest.raises(ValueError, match='invalid remote inventory'):
        adapter.collect({'target': 'mini'}, collector)


def test_cached_scope_never_widens_unknown_repo(adapter):
    data = view()
    value = data['hosts'][0]['value']
    value['scopes'] = {'api': ['/work']}
    assert adapter.rows(data, repo='api')
    assert not adapter.rows(data, repo='elsewhere')
    value['scopes'] = {}
    value['catalog'] = {'repositories': [{'repo': 'org/api', 'alias': 'api', 'path': '/work'}]}
    assert adapter.rows(data, repo='api')
    value['rows'] = value['rows'].replace('detached', 'app')
    assert not adapter.rows(data, repo='api', cli_only=True)


def test_failed_cold_host_reports_cooldown(adapter, capsys):
    data = view()
    data['hosts'][0].update(value=None, checked=0, error='offline')
    adapter.announce(data)
    text = capsys.readouterr().err
    assert 'unavailable' in text and 'refreshing' not in text


def test_catalog_projection_filters_and_preserves_repository_identity(adapter, monkeypatch):
    history = SimpleNamespace(cfg=SimpleNamespace(repos={'api': '/work'}),
                              repository_alias=lambda repo: 'api',
                              worktree_enabled=lambda alias: True,
                              pr_states=lambda rows: None)
    record = dict(repo='org/api', agent='codex', sid='saved', title='Saved task', updated=10000,
                  sources=[{'host': 'local', 'path': '/saved'}],
                  associations=[{'workspace': '/work', 'slot': '2', 'observed': 1}],
                  prs=[{'repo': 'org/api', 'number': 1, 'state': 'OPEN', 'url': 'https://example.invalid/1'}], mentions=[])
    data = {'conversations': [record]}
    row, = adapter._catalog_candidates(data, history, 'mini')
    fields = row.split('\t')
    assert fields[1:4] == ['api', '2', 'saved']
    assert fields[7] == 'mini' and fields[10:] == ['org/api', 'OPEN']
    history.worktree_enabled = lambda alias: False
    assert not list(adapter._catalog_candidates(data, history, 'mini'))
    history.worktree_enabled = lambda alias: True
    record['title'] = ''
    assert not list(adapter._catalog_candidates(data, history, 'mini'))


def test_cli_adapters_use_cache_and_targeted_events(adapter, t_mod, monkeypatch, capsys):
    monkeypatch.delenv('T_DISCOVERY_COLLECTOR')
    data = view()
    data['hosts'][0]['value']['scopes'] = {'api': ['/work']}
    cfg = SimpleNamespace(hosts={'mini': 'user@mini'}, repos={'api': '/work'})
    events = []
    helper = {'discover': lambda *a, **kw: data, 'announce': lambda v: None,
              'rows': adapter.rows, 'candidates': adapter.candidates,
              'event': lambda *a, **kw: events.append((a, kw))}
    monkeypatch.setattr(t_mod, '_session_discovery', lambda: helper)
    monkeypatch.setattr(t_mod, 'Config', lambda: cfg)
    assert t_mod._main(['__cache-rows', '--repo', 'api', '--basic']) == 0
    assert capsys.readouterr().out.startswith('local\ta\t/work')
    assert t_mod._main(['__cache-rows', '--repo', 'unknown']) == 0
    assert capsys.readouterr().out == ''
    assert t_mod._main(['__resume-candidates', 'api', '--slot', '1']) == 0
    assert 'Task' in capsys.readouterr().out
    assert t_mod._main(['__resume-candidates', 'api', '--slot', 'bad']) == 2
    capsys.readouterr()
    assert t_mod._main(['__cache-event', 'mini']) == 0
    assert events[-1][0] == (['user@mini'],)
    assert t_mod._main(['__cache-record', 'launch', 'new', 'codex', '/work', 'api-2']) == 0
    assert events[-1][1]['row'].startswith('new\t/work\tapi-2')


def test_local_snapshot_includes_unindexed_live_sessions_and_repo_scopes(adapter, collector, monkeypatch):
    cfg = SimpleNamespace(repos={'api': '/work/api'}, hosts={}, worktree_root='/trees')
    collector['Config'] = lambda: cfg
    collector['_mcp_context'] = lambda *a: dict(recs=[], live={'new': {'cwd': '/trees/api/1'}}, now=100, cache_root=None)
    collector['_session_row'] = lambda cfg, rec, *a: dict(sid=rec['sid'], cwd=rec['cwd'], slot='api-1')
    collector['_dev_url'] = lambda *a: 'http://localhost:5201'
    history_type = adapter.runpy.run_path(str(adapter.ROOT / 'libexec/t_history.py'))['HistoryCommand']
    monkeypatch.setattr(history_type, 'local_catalog', lambda self: {'repositories': [
        {'repo': 'org/api', 'alias': 'api', 'path': '/work/api'}], 'conversations': []})
    monkeypatch.setattr(adapter.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=0, stdout='', stderr=''))
    value = adapter.collect({'target': 'local'}, collector)
    assert value['scopes']['api'] == value['scopes']['org/api'] == ['/work/api', '/trees/api']
    row = value['mcp']['recs'][0]['_cached_row']
    assert row['title'] == '(no transcript yet)' and row['url'].endswith(':5201')
