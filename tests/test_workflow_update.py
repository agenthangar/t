"""Scoped instructions, confirmed migration and bounded watcher regression tests."""
import copy
import json
import os
from pathlib import Path
import socket
import subprocess
from types import SimpleNamespace

import pytest
from conftest import load_script

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def modules(tmp_path, monkeypatch):
    for key in ('HOME', 'XDG_CONFIG_HOME', 'XDG_STATE_HOME', 'CODEX_HOME', 'CLAUDE_CONFIG_DIR'):
        path = tmp_path / key
        path.mkdir()
        monkeypatch.setenv(key, str(path))
    monkeypatch.delenv('TMUX', raising=False)
    return [load_script(ROOT / 'libexec' / (name + '.py'), name + '_update') for name in
            ('t_workflow', 't_workflow_install', 't_watch', 't_workflow_sync')]


def test_command_contract():
    api = load_script(ROOT / 'bin/t', 't_update')
    parser = api.build_parser()
    context = parser.parse_args(['config', 'workflow', 'context', '--json'])
    assert context.workflow_action == 'context' and context.json
    watcher = parser.parse_args(['runners', 'runs', 'watch', 'abc', '--timeout', '3', '--json'])
    assert watcher.run_action == 'watch' and watcher.timeout == 3 and watcher.json
    assert api._command_readonly(['workflow', 'context'])
    assert api._command_readonly(['config', 'workflow', 'context', '--json'])


def test_scoped_context_and_worktree(modules, tmp_path):
    w = modules[0]
    root = tmp_path / 'repo'
    subprocess.run(['git', 'init', '-q', str(root)], check=True)
    subprocess.run(['git', '-C', str(root), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-qm', 'init'], check=True)
    tree = tmp_path / 'linked'
    subprocess.run(['git', '-C', str(root), 'worktree', 'add', '-qb', 'linked', str(tree)], check=True)
    doc = w.defaults()
    policy = copy.deepcopy(doc['workflows']['standard'])
    policy['instructions'] = 'only this repo'
    doc['repositories'][str(root.resolve())] = {'inline': policy}
    doc['repositories']['/unrelated'] = {'inline': dict(policy, instructions='never reveal other repo')}
    result = w.resolve_context(doc, tree)
    assert result['repository'] == str(root.resolve())
    assert 'only this repo' in result['text'] and 'never reveal' not in result['text']
    assert '/unrelated' not in w.fallback()
    doc['repositories'][str(root.resolve())]['excluded'] = True
    assert not w.resolve_context(doc, tree)['enabled']
    assert 'only this repo' not in w.resolve_context(doc, tree)['text']
    assert w.resolve_context(doc, tmp_path)['scope'] == 'global'
    with pytest.raises(ValueError):
        w.resolve_context(doc, tmp_path / 'missing')


def test_confirmed_migration_preserves_and_backs_up(modules, tmp_path):
    w, migration, _, _ = modules
    root = tmp_path / 'repo'
    root.mkdir()
    native = w.native_paths()[0]
    native.write_text('personal\n' + w.render(w.defaults()) + '\nend\n')
    local = root / 'AGENTS.md'
    local.write_text('repo safety\n' + w.render(w.defaults()))
    original = {path: path.read_text() for path in (native, local)}
    assert migration.install(vars(w), roots=[root], confirm=lambda: False) == 0
    assert all(path.read_text() == text for path, text in original.items())
    assert not w.config_path().exists()
    assert migration.install(vars(w), roots=[root], confirm=lambda: True) == 0
    assert native.read_text().startswith('personal\n') and native.read_text().endswith('\nend\n')
    assert local.read_text().startswith('repo safety\n')
    assert 't workflow context' in local.read_text()
    manifests = list((Path(os.environ['XDG_STATE_HOME']) / 't/instruction-backups').glob('*/manifest.json'))
    assert len(manifests) == 1
    saved = json.loads(manifests[0].read_text())
    for entry in saved['files']:
        if entry['path'] in {str(p) for p in original}:
            assert (manifests[0].parent / entry['backup']).read_text() == original[Path(entry['path'])]
    migration.install(vars(w), roots=[root], confirm=lambda: pytest.fail('unchanged migration prompted'))


def test_migration_rejects_symlinks_and_retains_custom_settings(modules, tmp_path):
    w, migration, _, _ = modules
    target = tmp_path / 'target'
    target.write_text('keep')
    w.native_paths()[0].symlink_to(target)
    with pytest.raises(ValueError, match='symlink'):
        migration.install(vars(w), roots=[], confirm=lambda: True)
    assert target.read_text() == 'keep'
    old = {'enabled': True, 'planning': {'model': 'gpt-6-astra', 'effort': 'auto'},
           'implementation': {'model': 'gpt-6.1-sol', 'effort': 'auto'}, 'review': {'model': 'gpt-6-astra', 'effort': 'auto'}}
    doc = w.defaults()
    doc['workflows'] = {'standard': old, 'custom': dict(old, stages_enabled=False)}
    changed = migration.migrate_defaults(doc)
    assert changed['workflows']['standard']['model_stages'] == ['review']
    assert changed['workflows']['custom'] == doc['workflows']['custom']


def test_watcher_terminal_pending_stale_and_identity(modules, monkeypatch):
    watch = modules[2]
    assert watch.owner() is None
    assert watch.records('-') == []
    identity = {'session_id': 'test-session', 'host': socket.gethostname()}
    active = watch.Activity('run', identity=identity)
    active.update()
    items = watch.records('test-session')
    assert watch.label(items) == 'active ' + active.data['id']
    assert watch.label(watch.records('test-session', now=items[0]['updated'] + 30)).startswith('stale ')
    assert watch.records('different-session') == []
    result = watch.wait(lambda: {'status': 'succeeded', 'exit_code': 0}, activity=active)
    assert result['wait_status'] == 'complete' and result['watcher_id'] == active.data['id']
    assert watch.label(watch.records('test-session')) == '—'
    times = iter([0, 2])
    monkeypatch.setattr(watch.time, 'monotonic', lambda: next(times))
    assert watch.wait(lambda: {'status': 'running'}, timeout=1)['wait_status'] == 'pending'
    with pytest.raises(ValueError):
        watch.wait(lambda: {}, timeout=0)


def test_speed_storage_and_fallback(modules, monkeypatch, capsys):
    w, _, _, sync = modules
    policy = w.defaults()['workflows']['standard']
    policy['review']['speed'] = 'ultrafast'
    policy['session_defaults']['codex']['speed'] = 'standard'
    w.validate_policy(policy)
    assert w.main_session_settings(policy)['speed'] == 'standard'
    assert sync.speed_args({'speed': 'standard'}, 'codex') == ['-c', 'service_tier=default']
    assert sync.speed_args({}, 'codex') == []
    monkeypatch.setattr(__import__('runpy'), 'run_path', lambda *a: {
        '_config_catalog': lambda *a: [], '_config_live_models': lambda *a: [], '_config_find_model': lambda *a: None})
    assert sync.speed_args({'speed': 'ultrafast'}, 'codex') == []
    assert 'using native speed' in capsys.readouterr().err


def test_watch_github_checks_head_and_failures(modules, monkeypatch):
    watch = modules[2]
    sha = 'a' * 40
    response = {'headRefOid': sha, 'statusCheckRollup': [{'status': 'COMPLETED', 'conclusion': 'SUCCESS'}]}
    monkeypatch.setattr(watch.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout=json.dumps(response)))
    assert watch.github('owner/repo', 1, sha)['status'] == 'succeeded'
    response['statusCheckRollup'][0]['conclusion'] = 'FAILURE'
    assert watch.github('owner/repo', 1, sha)['status'] == 'failed'
    response['headRefOid'] = 'b' * 40
    assert watch.github('owner/repo', 1, sha)['status'] == 'error'
    assert watch.runner('owner/repo#1@' + sha)['status'] == 'error'
    monkeypatch.setattr(watch.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stderr='offline'))
    with pytest.raises(ValueError, match='offline'):
        watch.github('owner/repo', 1, sha)


def test_watch_progress_only_changes_and_interruption(modules, monkeypatch):
    watch = modules[2]
    results = iter([{'status': 'running'}, {'status': 'running'}, {'status': 'succeeded'}])
    updates = []
    monkeypatch.setattr(watch.time, 'sleep', lambda _: None)
    assert watch.wait(lambda: next(results), progress=updates.append)['wait_status'] == 'complete'
    assert len(updates) == 2
    activity = watch.Activity('run', identity={'session_id': 's', 'host': socket.gethostname()})
    activity.start()
    def interrupted():
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        watch.wait(interrupted, activity=activity)
    assert watch.records('s')[0]['state'] == 'stopped'


def test_watcher_table_and_details(modules, monkeypatch):
    api = load_script(ROOT / 'bin/t', 't_update_table')
    rows = [{'sid': 'sid', 'slot': 'api-3', 'state': 'attached', 'context': 'active', 'agent': 'codex',
             'host': 'local', 'summary': 'Fix login 界面', 'model': 'sol', 'last_turn_started_at': '2026-11-09T22:10:00Z',
             'watchers': [{'id': 'watch-123456789abc', 'active': True, 'state': 'watching local tests', 'run_id': 'run-one'},
                          {'id': 'watch-abcdef123456', 'active': True, 'state': 'waiting', 'run_id': 'run-two'}]}]
    monkeypatch.setattr(api.sys.stdout, 'isatty', lambda: True)
    for width in (200, 120, 80, 44, 32):
        monkeypatch.setattr(api, '_columns', lambda: width)
        lines = api._session_table_lines(rows)
        assert 'WATCHER' in lines[0] and all(api._display_width(line) <= width for line in lines)
        assert 'api-3' in lines[1]
    monkeypatch.setattr(api.sys.stdout, 'isatty', lambda: False)
    lines = api._session_table_lines(rows, remote=True)
    assert lines[0].index('WORKING ON') < lines[0].index('WATCHER') < lines[0].index('LAST TURN')
    assert 'active watch-123456789abc +1' in lines[1]
    picker = api._session_picker_rows(rows)[0]
    assert 'run-one' in picker and 'run-two' in picker and 'watch-abcdef123456' in picker


def test_local_wait_connection_bound(modules, monkeypatch):
    worker = load_script(ROOT / 'libexec/t_local_worker.py', 'worker_wait_update')
    instance = worker.Worker()
    instance.tasks['one'] = ({'task_id': 'one', 'status': 'completed', 'evidence': ['checked']}, None)
    result = instance.call('wait_task', {'task_id': 'one'})
    assert result['wait_status'] == 'complete' and result['evidence'] == ['checked']
    for args in ({'task_id': 'another'}, {'task_id': 'one', 'timeout_seconds': 61}, {'task_id': 'one', 'extra': True}):
        with pytest.raises(ValueError):
            instance.call('wait_task', args)


def test_text_submenu_undo_and_speed_edits_preserve_settings(modules, monkeypatch):
    from test_workflow import Menu
    w = modules[0]
    api = load_script(ROOT / 'bin/t', 't_update_menus')
    doc = w.defaults()
    doc['workflows']['standard']['instructions'] = 'keep this exact text'
    cfg = SimpleNamespace(repos={}, _workflow_document=doc)
    original = copy.deepcopy(doc)
    api._workflow_policy_menu(Menu(['instructions', 'clear', 'instructions', 'undo', 'back']), cfg, vars(w), name='standard')
    assert cfg._workflow_document == original
    api._workflow_policy_menu(Menu(['instructions', 'clear', 'back']), cfg, vars(w), name='standard')
    assert cfg._workflow_document['workflows']['standard']['instructions'] == ''
    policy = original['workflows']['standard']
    policy['session_defaults']['codex']['speed'] = 'ultrafast'
    def edit(ui, staged, *args, **kwargs):
        staged.efforts['codex'] = 'high'
    monkeypatch.setattr(api, '_config_model_menu', edit)
    changed = api._workflow_session_setting(None, cfg, policy, 'codex', 'effort')
    assert changed['session_defaults']['codex']['speed'] == 'ultrafast'
    assert changed['session_defaults']['codex']['effort'] == 'high'
    monkeypatch.setattr(api, '_config_model_menu', lambda *args, **kwargs: None)
    assert api._workflow_speed(None, cfg, policy, 'codex') == policy


def test_migration_rechecks_after_confirmation(modules, tmp_path):
    w, migration, _, _ = modules
    path = w.native_paths()[0]
    path.write_text(w.render(w.defaults()))
    def confirm():
        path.write_text('changed during review')
        return True
    with pytest.raises(ValueError, match='changed after review'):
        migration.install(vars(w), roots=[], confirm=confirm)
    assert path.read_text() == 'changed during review'
    assert not w.config_path().exists()


def test_native_context_caps_without_losing_custom_text(modules, tmp_path, monkeypatch):
    w = modules[0]
    hook = load_script(ROOT / 'libexec/t_workflow_context.py', 'hook_update')
    monkeypatch.setattr(hook.runpy, 'run_path', lambda *args: vars(w))
    assert hook.context('codex', tmp_path) == ''
    doc = w.defaults()
    doc['workflows']['standard']['instructions'] = 'long instruction\n' * 1000
    w.publish([(w.config_path(), None, w.encode(doc))])
    text = hook.context('codex', tmp_path)
    assert text == w.fallback('codex')
    assert 'long instruction' in w.resolve_context(w.load()[0], tmp_path)['text']


def test_watcher_verified_owner_and_reused_pane(modules, monkeypatch, tmp_path):
    watch = modules[2]
    sid = '11111111-1111-4111-8111-111111111111'
    monkeypatch.setenv('TMUX', '/tmp/isolated-watch,1,0')
    monkeypatch.setenv('TMUX_PANE', '%4')
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path))
    registry = tmp_path / 'claude-sessions'
    registry.mkdir()
    (registry / str(os.getpid())).write_text(sid + '\t/repo')
    installed = []
    local = {'pane_info': lambda *a: ['%4', '100', '$1'], 'ancestry': lambda *a: True,
             'tmux': lambda *a: SimpleNamespace(stdout='CLAUDE_RESUME_ID=' + sid),
             'install': lambda *a: installed.append(a), 'root': lambda: tmp_path}
    monkeypatch.setattr(watch, 'helper', lambda _: local)
    assert watch.owner()['session_id'] == sid and len(installed) == 1
    (registry / str(os.getpid())).write_text('old-session\t/repo')
    monkeypatch.setattr(watch.subprocess, 'run', lambda *a, **k: SimpleNamespace(stdout='1'))
    assert watch.owner() is None
    local['tmux'] = lambda *a: SimpleNamespace(stdout='-CLAUDE_RESUME_ID')
    assert watch.owner() is None
    local['ancestry'] = lambda *a: False
    assert watch.owner() is None


def test_watch_cache_corruption_and_write_failure_do_not_break_wait(modules, monkeypatch, capsys):
    watch = modules[2]
    active = watch.Activity('run', identity={'session_id': 's', 'host': socket.gethostname()})
    active.update()
    folder = next(watch.root().iterdir())
    for name, text in [('array', '[]'), ('json', '{broken'), ('foreign', json.dumps(dict(active.data, host='elsewhere'))),
                       ('badid', json.dumps(dict(active.data, id='wrong'))),
                       ('badtime', json.dumps(dict(active.data, created='yesterday')))]:
        (folder / (name + '.json')).write_text(text)
    assert len(watch.records('s')) == 1
    def no_write(*a, **k):
        raise PermissionError('read only cache')
    monkeypatch.setattr(watch.os, 'replace', no_write)
    result = watch.wait(lambda: {'status': 'succeeded', 'exit_code': 0}, activity=active)
    assert result['wait_status'] == 'complete'
    assert capsys.readouterr().err.count('activity display unavailable') == 1


def test_watch_timeout_rate_limit_and_local_receipt(modules, monkeypatch):
    watch = modules[2]
    sha = 'a' * 40
    responses = iter([SimpleNamespace(returncode=1, stderr='rate limit exceeded'),
                      SimpleNamespace(returncode=0, stdout=json.dumps({'headRefOid': sha,
                          'statusCheckRollup': [{'status': 'COMPLETED', 'conclusion': 'SUCCESS'}]}))])
    monkeypatch.setattr(watch.subprocess, 'run', lambda *a, **k: next(responses))
    pauses, changes = [], []
    monkeypatch.setattr(watch.time, 'sleep', pauses.append)
    assert watch.github('o/r', 1, sha, progress=changes.append)['status'] == 'succeeded'
    assert pauses == [30] and changes[0]['unavailable'] == 'GitHub rate limit'
    def timeout(*a, **k):
        raise subprocess.TimeoutExpired('gh', 1)
    monkeypatch.setattr(watch.subprocess, 'run', timeout)
    ticks = iter([0, 0, 0, 2])
    monkeypatch.setattr(watch.time, 'monotonic', lambda: next(ticks))
    assert watch.github('o/r', 1, sha, timeout=1)['wait_status'] == 'pending'
    monkeypatch.setattr(watch.time, 'monotonic', lambda: 0)
    monkeypatch.setattr(watch, 'helper', lambda _: {'status': lambda _: {'id': 'run', 'status': 'succeeded', 'targets': {'local': {'type': 'local'}}}})
    assert watch.runner('run')['wait_status'] == 'complete'


def test_native_speed_capabilities(modules, monkeypatch):
    sync = modules[3]
    monkeypatch.setattr(__import__('runpy'), 'run_path', lambda *a: {
        '_config_catalog': lambda *a: [], '_config_live_models': lambda *a: [],
        '_config_find_model': lambda *a: {'fast': True, 'ultrafast': True}})
    assert 'service_tier=fast' in sync.speed_args({'speed': 'fast'}, 'codex')
    assert 'features.ultrafast_mode=true' in sync.speed_args({'speed': 'ultrafast'}, 'codex')
    assert json.loads(sync.speed_args({'speed': 'fast'}, 'claude')[1]) == {'fastMode': True}
    assert sync.speed_args({'speed': 'ultrafast'}, 'cursor') == []


def test_installer_discovers_linked_worktrees_and_requires_tty(modules, monkeypatch, tmp_path):
    w, install, _, _ = modules
    doc = w.defaults()
    doc['repositories']['/canonical'] = {'workflow': 'standard'}
    monkeypatch.setattr(install.runpy, 'run_path', lambda _: {'Config': lambda: SimpleNamespace(repos={'r': '/registered'})})
    monkeypatch.setattr(install.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout='worktree /linked\0HEAD abc\0\0'))
    assert install.repositories(doc) == {Path('/canonical'), Path('/registered'), Path('/linked')}
    monkeypatch.setattr(install.sys.stdin, 'isatty', lambda: False)
    assert install.install(vars(w), roots=[]) == 0
    assert not w.config_path().exists()


def test_installer_eof_defers_without_writing(modules, monkeypatch):
    w, install, _, _ = modules
    monkeypatch.setattr(install.sys.stdin, 'isatty', lambda: True)
    def eof(*args):
        raise EOFError
    monkeypatch.setattr('builtins.input', eof)
    assert install.install(vars(w), roots=[]) == 0
    assert not w.config_path().exists()


def test_statusline_combines_verified_watch_and_local_model(modules, monkeypatch, tmp_path):
    import time
    status = load_script(ROOT / 'libexec/t_local_status.py', 'watch_status')
    watch = modules[2]
    sid, transport, pane = 'session', '/tmp/isolated', '%1'
    monkeypatch.setattr(status, 'pane_info', lambda *a: [pane, '42', '$1', 'Title | test', '/repo', 'slot'])
    monkeypatch.setattr(status, 'tmux', lambda *a: SimpleNamespace(stdout='CLAUDE_RESUME_ID=' + sid))
    info = {'session_id': sid, 'host': socket.gethostname(), 'socket': transport, 'pane': pane, 'pane_pid': 42}
    activity = watch.Activity('run', identity=info)
    activity.update('watching local tests')
    record = status.record_path(tmp_path, transport, pane)
    record.write_text(json.dumps({'pane_pid': 42, 'updated': time.time(), 'state': 'working', 'model': 'provider/model'}))
    text = status.render('right', tmp_path, transport, pane)
    assert 'Watching local tests' in text and 'local model' in text
    activity.finish({'status': 'succeeded'})
    assert 'Result ready' in status.render('right', tmp_path, transport, pane)
    monkeypatch.setattr(status, 'tmux', lambda *a: SimpleNamespace(stdout='CLAUDE_RESUME_ID=other'))
    assert 'Result ready' not in status.render('right', tmp_path, transport, pane)


def test_speed_save_review_labels_speed(modules):
    w = modules[0]
    api = load_script(ROOT / 'bin/t', 'speed_review')
    before = w.defaults()
    after = copy.deepcopy(before)
    after['workflows']['standard']['session_defaults']['codex']['speed'] = 'fast'
    lines = api._workflow_changed_review(vars(w), after, before)
    assert any('Lead speed:' in line and 'fast' in line for line in lines)


def test_speed_validates_effective_launch_model_and_keeps_worker_separate(modules, monkeypatch):
    w, _, _, sync = modules
    seen = []
    monkeypatch.setenv('T_WORKFLOW_DEFAULT_MODEL', 'account-model')
    monkeypatch.setattr(__import__('runpy'), 'run_path', lambda *a: {
        '_config_catalog': lambda *a: [], '_config_live_models': lambda *a: [],
        '_config_find_model': lambda catalog, model: seen.append(model) or {'fast': True}})
    assert sync.speed_args({'speed': 'fast'}, 'codex')
    assert seen == ['account-model']
    policy = w.defaults()['workflows']['standard']
    policy['model_stages'] = ['implementation']
    policy['implementation'].update(effort='auto', speed='ultrafast')
    policy['session_defaults']['codex']['speed'] = 'standard'
    assert w.main_session_settings(policy)['speed'] == 'standard'
    policy['implementation']['effort'] = 'high'
    assert w.main_session_settings(policy)['speed'] == 'ultrafast'
