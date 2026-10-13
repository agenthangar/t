"""History displays exact session metadata and keeps its terminal action visible."""
import json
import os
from pathlib import Path
import shlex
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

import pytest

from test_history_data import history, store, app, conversation, codex_rows, transcript_file

ROOT = Path(__file__).resolve().parents[1]


def example(**changes):
    record = conversation(sid='synthetic-selected', title='Fix sign-in redirect after session expires',
                          summary='Fix sign-in redirect after session expires', summary_source='transcript',
                          model='gpt-6.1-sol', model_source='transcript', interface='cli',
                          associations=[dict(workspace='/sandbox/api/5', slot='5', host='local',
                                             branch='dev/api-5', commit='a' * 40, evidence='resume', observed=1)],
                          prs=[dict(repo='org/repo', number=121, url='https://github.com/org/repo/pull/121', state='MERGED'),
                               dict(repo='org/repo', number=122, url='https://github.com/org/repo/pull/122', state='OPEN')])
    record.update(changes)
    return record


def test_transcript_models_are_actual_recorded_models_and_never_config_defaults(history, tmp_path):
    codex = codex_rows(tmp_path, sid='synthetic-codex') + [
        dict(type='turn_context', timestamp=10, payload=dict(model='gpt-6-astra')),
        dict(type='turn_context', timestamp=20, payload=dict(model='gpt-6.1-sol')),
        dict(type='turn_context', timestamp=30, payload=dict(model='<synthetic>')),
        dict(type='response_item', payload=dict(role='user', content=[dict(text='# AGENTS.md instructions for /private')]))]
    value = history.transcript(transcript_file(tmp_path / 'rollout-synthetic-codex.jsonl', codex), 'codex')
    assert value['model'] == 'gpt-6.1-sol' and value['model_source'] == 'transcript'
    assert value['summary'] == 'Implement history'
    assert '# AGENTS' not in value['summary']
    claude = [dict(type='user', sessionId='synthetic-claude', cwd=str(tmp_path), timestamp=10, message=dict(content='Review keyboard focus')),
              dict(type='assistant', timestamp=20, message=dict(model='claude-opus-4-6', content=[])),
              dict(type='custom-title', timestamp=30, customTitle='Keyboard accessibility')]
    value = history.transcript(transcript_file(tmp_path / 'synthetic-claude.jsonl', claude), 'claude')
    assert value['model'] == 'claude-opus-4-6' and value['model_updated'] == 20
    assert value['summary'] == 'Keyboard accessibility' and value['summary_source'] == 'transcript title'
    absent = history.transcript(transcript_file(tmp_path / 'rollout-unknown.jsonl', codex_rows(tmp_path)), 'codex')
    assert absent['model'] == '-' and absent['interface'] == 'unknown'


def test_exact_owner_metadata_matches_id_agent_and_repository_not_reused_slot(app, store):
    workspace = Path(app.cfg.worktree_root) / 'repo/5'
    row = example(associations=[dict(workspace=str(workspace), slot='5', host='local')], model='-', interface='unknown')
    store.merge(row)
    unrelated = dict(sid='replacement', agent='codex', cwd=str(workspace), slot='repo-5', host='local',
                     state='attached', context='active', model='gpt-6-astra', summary='Unrelated newer work')
    assert app.records('repo', '5', [unrelated])[0]['display']['model'] == '-'
    wrong_agent = {**unrelated, 'sid': row['sid'], 'agent': 'claude'}
    assert app.records('repo', '5', [wrong_agent])[0]['owners'] == []
    wrong_repo = {**unrelated, 'sid': row['sid'], 'cwd': '/unregistered/other/5'}
    assert app.records('repo', '5', [wrong_repo])[0]['owners'] == []
    selected = {**unrelated, 'sid': row['sid'], 'summary': 'Finish the exact selected fix', 'model': 'gpt-6.1-sol'}
    live = app.records('repo', '5', [selected])[0]
    assert live['display']['model'] == 'gpt-6.1-sol'
    assert live['display']['summary'] == selected['summary']
    assert live['display']['context'] == 'active' and live['display']['interface'] == 'cli'
    assert live['summary_source'] == 'exact owner'
    assert store.records()[0]['model'] == 'gpt-6.1-sol'


def test_remote_display_matches_exact_retained_host_workspace(app, store):
    remote = '/home/another/worktrees/repo/7'
    row = example(associations=[dict(workspace=remote, slot='7', host='build')])
    store.merge(row)
    owner = dict(sid=row['sid'], agent='codex', cwd=remote, slot='repo-7', host='build',
                 state='app', context='active', model='gpt-6-astra', summary='Exact remote work')
    selected = app.records('repo', '7', [owner])[0]
    assert selected['display']['host'] == 'build' and selected['display']['interface'] == 'desktop'
    assert selected['display']['model'] == 'gpt-6-astra'
    wrong_host = {**owner, 'host': 'unknown-host'}
    assert app.records('repo', '7', [wrong_host])[0]['owners'] == []


def test_stale_remote_or_index_metadata_cannot_replace_newer_exact_owner(app, store, history, monkeypatch):
    workspace = Path(app.cfg.worktree_root) / 'repo/5'
    initial = example(associations=[dict(workspace=str(workspace), slot='5', host='local')],
                      model='gpt-6-astra', model_updated=10, summary='Earlier work', summary_updated=10,
                      interface='unknown', interface_updated=10, updated=10)
    store.merge(initial)
    monkeypatch.setattr(history.time, 'time', lambda: 200)
    owner = dict(sid=initial['sid'], agent='codex', cwd=str(workspace), host='local', slot='repo-5',
                 state='attached', context='active', model='gpt-6.1-sol', summary='Current exact work')
    app.records('repo', '5', [owner])
    stale = {**initial, 'updated': 10000, 'model_updated': 20, 'summary_updated': 20, 'interface_updated': 20,
             'model_source': 'index', 'summary_source': 'index'}
    app.cfg.hosts = {'build': 'synthetic.example'}
    monkeypatch.setattr(history, 'run', lambda *a, **k: json.dumps([stale]))
    app.refresh('repo')
    saved = app.records('repo', '5', [])[0]
    assert saved['model'] == 'gpt-6.1-sol' and saved['model_source'] == 'exact owner'
    assert saved['summary'] == 'Current exact work' and saved['summary_source'] == 'exact owner'
    assert saved['interface'] == 'cli' and saved['display']['status'] == 'Saved'
    assert saved['display']['context'] == 'unknown' and not saved['owners']
    assert saved['model_updated'] == saved['summary_updated'] == 200


def test_rich_browse_does_not_change_authoritative_raw_ownership_scan(app, history, monkeypatch):
    calls = []
    monkeypatch.setattr(history, 'run', lambda argv, **k: (calls.append(argv[-1]), '')[1])
    app.owners()
    app.owners(True)
    assert '_dev_session_rows --details' in calls[0]
    assert '_dev_rows_all' in calls[1] and '--details' not in calls[1]


def test_index_backfill_retains_exact_transcript_model_when_index_has_no_model(app, history, tmp_path):
    workspace = Path(app.cfg.worktree_root) / 'repo/5'
    rollout = transcript_file(Path(os.environ['CODEX_HOME']) / 'sessions/rollout-synthetic-selected.jsonl',
                              codex_rows(workspace, sid='synthetic-selected') + [dict(type='turn_context', timestamp=2000000000, payload=dict(model='gpt-6.1-sol'))])
    with sqlite3.connect(Path(os.environ['CODEX_HOME']) / 'state_5.sqlite') as db:
        db.execute('create table threads(id text,cwd text,name text,title text,source text,archived integer,updated_at integer,rollout_path text)')
        db.execute('insert into threads values (?,?,?,?,?,?,?,?)', ('synthetic-selected', str(workspace), 'Named current work', '', 'cli', 0, 2000000001, str(rollout)))
    app.discover('repo')
    row = app.records('repo', '5')[0]
    assert row['model'] == 'gpt-6.1-sol'
    assert row['summary'] == 'Named current work' and row['summary_source'] == 'index'
    assert row['display']['slot'] == 'repo-5'


@pytest.mark.parametrize('context,mark', [('active', '●✓'), ('idle', '○')])
def test_desktop_status_matches_ls_semantics(history, t_mod, context, mark):
    row = example(owners=[dict(state='app', context=context, slot='api-9', host='local')])
    display = history.history_display(row, 'api')
    display['summary'] = 'Desktop work'
    actual = history.format_row(row, 80)
    ls = t_mod._session_table_lines([display], remote=True)
    assert actual.startswith(mark)
    assert '▣' in actual and display['interface'] == 'desktop'
    assert ('●' if context == 'active' else '○') in '\n'.join(ls)


@pytest.mark.parametrize('width', [68, 32, 20])
def test_picker_rows_budget_unicode_metadata_before_summary(history, width):
    row = example(summary='Fix keyboard 操作 focus and cafe\u0301 labels')
    rendered = history.format_row(row, width)
    assert history.display_width(rendered) <= width
    assert 'Fix' in rendered and '\x1b' not in rendered
    if width >= 60:
        assert 'gpt-6.1-sol' in rendered and '#121M' in rendered and '#122O' in rendered
    if width >= 32:
        assert '6.1-sol' in rendered
    assert row['sid'] not in rendered and '2026' not in rendered


def test_model_shortening_preserves_family_and_full_exact_values_in_details(history):
    row = example(agent='claude', model='claude-opus-4-6')
    assert 'opus-4-6' in history.format_row(row, 68)
    assert 'Model: ✱ claude-opus-4-6 (claude)' in history.details(row, 'api')
    assert 'Shift' not in history.details(row, 'api')
    row = example(model='gpt-6-astra')
    assert 'gpt-6-astra' in history.format_row(row, 68)


def test_lifecycle_uses_one_incremental_scan_for_identity_and_metadata(app, history, tmp_path, monkeypatch):
    workspace = Path(app.cfg.worktree_root) / 'repo/5'
    rollout = transcript_file(tmp_path / 'rollout-synthetic-selected.jsonl',
                              codex_rows(workspace, sid='synthetic-selected') + [dict(type='turn_context', timestamp=20, payload=dict(model='gpt-6.1-sol'))])
    original, scans = history.Store.scan, []
    def scan(store, path, agent):
        scans.append(path)
        return original(store, path, agent)
    monkeypatch.setattr(history.Store, 'scan', scan)
    def commands(argv, **kwargs):
        if argv[-3:] == ['remote', 'get-url', 'origin']:
            return 'https://github.com/org/repo.git\n'
        if argv[-1] == 'HEAD':
            return 'a' * 40
        return 'dev/repo-5'
    monkeypatch.setattr(history, 'run', commands)
    history.record_association('synthetic-selected', 'codex', str(workspace), str(rollout), 'resume', 'cli')
    assert scans == [rollout]
    record = next(r for r in app.store.records() if r['sid'] == 'synthetic-selected')
    assert record['model'] == 'gpt-6.1-sol' and record['interface'] == 'cli'
    history.record_association('wrong-id', 'codex', str(workspace), str(rollout), 'resume', 'cli')
    assert len(scans) == 2
    assert not any(r['sid'] == 'wrong-id' for r in app.store.records())


@pytest.mark.parametrize('columns,lines', [(80, 24), (44, 20), (32, 12)])
def test_real_details_picker_retains_focus_and_scrollable_metadata_at_each_size(history, tmp_path, columns, lines):
    if not shutil.which('tmux') or not shutil.which('fzf'):
        pytest.skip('real terminal geometry requires tmux and fzf')
    env = {k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_') and k not in ('TMUX', 'TMUX_PANE')}
    env.update(TERM='xterm-256color', FZF_DEFAULT_OPTS='', FZF_DEFAULT_OPTS_FILE='/dev/null')
    rows = [example(sid='selected', updated=20), example(sid='second', updated=10, agent='claude', model='claude-opus-4-6', archived=True,
                              summary='Review settings keyboard focus')]
    snapshot = tmp_path / 'records.json'; snapshot.write_text(json.dumps(rows))
    result = tmp_path / 'result.json'
    harness = tmp_path / 'picker.py'
    harness.write_text('''import importlib.util,json,pathlib,sys
from types import SimpleNamespace
root,snapshot,result=map(pathlib.Path,sys.argv[1:])
spec=importlib.util.spec_from_file_location('synthetic_details',root/'libexec/t_history.py');m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
rows=json.loads(snapshot.read_text())
for row in rows:row['display']=m.history_display(row,'api')
app=m.HistoryCommand(SimpleNamespace(repos={'api':'/sandbox/api'},hosts={}),{'_t_source_root':lambda:str(root)})
try:
 row,key,query=app.pick(rows,'api 5');result.write_text(json.dumps(dict(key=key,sid=row['sid'] if row else None)))
finally:app.store.close()
''')
    with tempfile.TemporaryDirectory(prefix='t-details-geometry-') as directory:
        socket = str(Path(directory) / 'tmux.sock')
        def tmux(*args):
            return subprocess.run(['tmux', '-S', socket, '-f', '/dev/null', *args], env=env, text=True,
                                  capture_output=True, timeout=10, check=True).stdout
        def screen():
            return tmux('capture-pane', '-p', '-t', 'picker')
        def wait_for(predicate):
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                text = screen()
                if predicate(text):
                    return text
                time.sleep(.05)
            pytest.fail('detail terminal did not render required fields:\n' + screen())
        try:
            tmux('new-session', '-d', '-s', 'picker', '-x', str(columns), '-y', str(lines),
                 shlex.join([sys.executable, str(harness), str(ROOT), str(snapshot), str(result)]))
            tmux('set-option', '-t', 'picker', 'remain-on-exit', 'on')
            first = wait_for(lambda text: 'Fix sign' in text and 'Enter resume' in text and 'Ctrl-/' in text)
            assert any('▸' in line and 'Fix sign' in line for line in first.splitlines())
            if columns >= 70:
                first = wait_for(lambda text: 'Model:' in text and '#121M' in text)
                assert 'gpt-6.1-sol' in first and '#122O' in first
            else:
                tmux('send-keys', '-t', 'picker', 'C-_')
                opened = wait_for(lambda text: 'Details' in text and ('Model:' in text or 'Working on:' in text))
                assert '▸' in opened and 'Fix sign' in opened and 'Enter resume' in opened
                assert 'Shift' in opened
                if lines <= 14:
                    assert 'gpt-6.1-sol' in opened
                scrolled = opened
                for _ in range(24):
                    if 'ID: selected' in scrolled:
                        break
                    tmux('send-keys', '-t', 'picker', 'S-Down')
                    previous = scrolled
                    scrolled = wait_for(lambda text: text != previous)
                assert 'ID: selected' in scrolled
                assert any('▸' in line and 'Fix sign' in line for line in scrolled.splitlines())
                assert 'Enter resume' in scrolled
            tmux('send-keys', '-t', 'picker', 'Escape')
            deadline = time.monotonic() + 5
            while not result.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            assert json.loads(result.read_text()) == {'key': 'cancel', 'sid': None}
        finally:
            subprocess.run(['tmux', '-S', socket, 'kill-server'], env=env, capture_output=True)
