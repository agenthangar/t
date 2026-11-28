"""Display cache compatibility stays behind the managed artifact contract."""
from conftest import REPO_ROOT, load_script


def test_display_cache_reads_writes_and_enumeration(monkeypatch):
    helper = load_script(REPO_ROOT / 'libexec/t_cache_file.py', 'display_cache_test')
    values = {}
    calls = []
    def request(operation, payload):
        calls.append(operation)
        if operation == 'artifacts':
            return {'values': {key: value for key, value in values.items() if key.startswith(payload['prefix'])}}
        key = payload['key']
        if 'value' in payload:
            values[key] = payload['value']
        return {'value': values.get(key)}
    monkeypatch.setattr(helper, 'request', request)
    helper.write('/private/cache/pr/o#r#1', 'MERGED')
    helper.write('/private/cache/pr/o#r#1.head', 'abc')
    assert helper.read('/private/cache/pr/o#r#1') == 'MERGED'
    assert helper.read('/private/cache/pr/missing') == ''
    assert set(helper.files('/private/cache/pr/')) == {
        '/private/cache/pr/o#r#1', '/private/cache/pr/o#r#1.head'}
    assert set(calls) == {'artifact', 'artifacts'}
    assert helper.record('/private/cache/pr/o#r#1')['checked'] > 0


def test_stopped_cache_prevents_pr_network_refresh(monkeypatch):
    helper = load_script(REPO_ROOT / 'libexec/t_cache_file.py', 'stopped_display_cache_test')
    monkeypatch.setattr(helper, 'request', lambda *args: {'unavailable': True})
    monkeypatch.setattr(helper.subprocess, 'run', lambda *args, **kwargs: (_ for _ in ()).throw(
        AssertionError('stopped cache started network refresh')))
    monkeypatch.setattr(helper.sys, 'argv', ['helper', 'refresh', 'https://github.com/o/r/pull/1', '/cache/pr/1'])
    assert helper.main() == 1
