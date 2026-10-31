"""Tool scopes remain isolated during launch resolution and host propagation."""
import json

import pytest

from test_workflow_sync import sync  # noqa: F401 -- isolated native/config homes


def tool_model(w, document, tool, model):
    scoped = w['tool_document'](document, tool)
    policy = scoped['workflows'][scoped['default']]
    policy['stages_enabled'] = False
    policy['session_defaults'] = {tool: {'model': model}}
    return w['with_tool_document'](document, tool, scoped)


def test_tool_launch_settings_choose_actual_tool_before_repository(sync, monkeypatch):
    w = sync.workflow()
    document = tool_model(w, w['defaults'](), 'codex', 'codex-main')
    document = tool_model(w, document, 'claude', 'claude-main')
    document = tool_model(w, document, 'cursor', 'cursor-model[effort=high,fast=false]')
    w['save'](document, None, propagate=False)
    assert sync.launch_settings('codex', '/work') == ['--model', 'codex-main']
    assert sync.launch_settings('claude', '/work') == ['--model', 'claude-main']
    assert sync.launch_settings('cursor', '/work') == ['--model', 'cursor-model[effort=high,fast=false]']
    # A different tool's repository override must not trigger Git discovery here.
    scoped = w['tool_document'](document, 'claude')
    scoped['repositories']['/claude-repo'] = {'workflow': 'standard', 'excluded': True}
    document = w['with_tool_document'](document, 'claude', scoped)
    w['save'](document, w['snapshot'](w['config_path']()), propagate=False)
    monkeypatch.setattr(sync, 'git', lambda *args: pytest.fail('unrelated repository scope queried'))
    assert sync.launch_settings('codex', '/work') == ['--model', 'codex-main']
    assert sync.setting_args({'model': 'cursor-model', 'effort': 'high'}, 'cursor') == ['--model', 'cursor-model']


def test_changed_scopes_preserves_tool_identity(sync):
    w = sync.workflow()
    before = w['defaults']()
    after = tool_model(w, before, 'claude', 'claude-main')
    assert sync.changed_scopes(before, after) == ['tool:claude']
    scoped = w['tool_document'](after, 'cursor')
    scoped['repositories']['/source'] = {'workflow': 'standard', 'excluded': True}
    after = w['with_tool_document'](after, 'cursor', scoped)
    assert sync.changed_scopes(before, after) == ['tool:claude', 'tool:cursor:/source']
    assert before == w['defaults']()


def test_portable_tools_round_trip_without_local_paths_or_cross_tool_changes(sync, monkeypatch, tmp_path):
    w = sync.workflow()
    local = tool_model(w, w['defaults'](), 'codex', 'keep-codex')
    w['save'](local, None, propagate=False)
    incoming = tool_model(w, w['defaults'](), 'claude', 'remote-claude')
    incoming = tool_model(w, incoming, 'cursor', 'remote-cursor')
    for tool in ('claude', 'cursor'):
        scoped = w['tool_document'](incoming, tool)
        scoped['repositories']['/source/private/path'] = {'workflow': 'standard', 'excluded': tool == 'cursor'}
        if tool == 'claude':
            scoped['repositories']['/source/private/path'].pop('excluded')
        incoming = w['with_tool_document'](incoming, tool, scoped)
    monkeypatch.setattr(sync, 'identity', lambda _: 'owner/project')
    scopes = ['tool:claude', 'tool:claude:/source/private/path', 'tool:cursor:/source/private/path']
    payload, missing = sync.portable(incoming, scopes)
    assert payload['version'] == 2 and set(payload['tools']) == {'claude', 'cursor'}
    assert not missing and '/source/private/path' not in json.dumps(payload)
    result = sync.receive(payload, {'project': str(tmp_path)})
    assert result['pending_scopes'] == 0
    actual = w['load']()[0]
    assert w['tool_document'](actual, 'codex') == w['tool_document'](local, 'codex')
    claude = w['tool_document'](actual, 'claude')
    cursor = w['tool_document'](actual, 'cursor')
    assert w['main_session_settings'](w['effective'](claude, str(tmp_path)), 'claude')['model'] == 'remote-claude'
    assert not w['effective'](cursor, str(tmp_path))['enabled']
    assert not cursor['repositories'].get('/source/private/path')


def test_legacy_payload_preserves_other_tools(sync, monkeypatch):
    w = sync.workflow()
    before = tool_model(w, w['defaults'](), 'claude', 'keep-claude')
    before = tool_model(w, before, 'cursor', 'keep-cursor')
    w['save'](before, None, propagate=False)
    incoming = tool_model(w, w['defaults'](), 'codex', 'new-codex')
    payload, missing = sync.portable(incoming, [None])
    assert payload['version'] == 1 and not missing
    sync.receive(payload, {})
    actual = w['load']()[0]
    assert actual['tools'] == before['tools']
    assert w['main_session_settings'](w['effective'](actual), 'codex')['model'] == 'new-codex'


def test_tool_scope_limit_and_invalid_tool_payload(sync, monkeypatch):
    w = sync.workflow()
    monkeypatch.setattr(sync, 'MAX_SCOPES', 1)
    monkeypatch.setattr(sync, 'identity', lambda _: 'owner/project')
    payload, missing = sync.portable(w['defaults'](), ['tool:claude', 'tool:cursor'])
    assert set(payload['tools']) == {'claude'} and missing == 1
    with pytest.raises(ValueError, match='tool'):
        sync.receive({'version': 2, 'tools': {'unknown': {}}}, {})
    with pytest.raises(ValueError, match='tool scope'):
        sync.scope_parts('tool:claude:relative')


def test_remote_removes_only_sent_tool_scopes(sync, monkeypatch):
    w = sync.workflow()
    doc = w['defaults']()
    state = {'context': {'hosts': {'host': 'example'}}, 'remote_scopes': {'example': ['tool:claude', 'tool:cursor:/unmapped']},
             'remote_settings': {}, 'hosts': {}, 'origin': 'a' * 32, 'revision': 1}
    monkeypatch.setattr(sync, 'current', lambda _: True)
    monkeypatch.setattr(sync, 'identity', lambda _: None)
    monkeypatch.setattr(sync, 'workflow', lambda: {**w, 'load': lambda: (doc, None)})
    calls = []
    def run(argv, **kwargs):
        from types import SimpleNamespace
        calls.append(json.loads(kwargs['input']))
        return SimpleNamespace(returncode=0, stdout=json.dumps({'summary': 'received', 'pending_scopes': 0}))
    monkeypatch.setattr(sync.subprocess, 'run', run)
    sync.remote(state)
    assert calls[0]['version'] == 2
    assert state['remote_scopes']['example'] == ['tool:cursor:/unmapped']
