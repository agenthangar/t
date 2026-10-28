"""History discovery uses synthetic transcripts and private disposable stores."""
import importlib.util
import json
import os
import re
from pathlib import Path
import shlex
import sqlite3
import stat
import subprocess
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def history(tmp_path, monkeypatch):
    for name, path in [('HOME', tmp_path / 'home'), ('XDG_STATE_HOME', tmp_path / 'state'),
                       ('XDG_CONFIG_HOME', tmp_path / 'config'), ('XDG_CACHE_HOME', tmp_path / 'cache'),
                       ('XDG_DATA_HOME', tmp_path / 'data'), ('CODEX_HOME', tmp_path / 'codex')]:
        path.mkdir()
        monkeypatch.setenv(name, str(path))
    spec = importlib.util.spec_from_file_location('history_data_test', ROOT / 'libexec/t_history.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def store(history):
    value = history.Store()
    yield value
    value.close()


@pytest.fixture
def app(history, store, tmp_path, monkeypatch, t_mod):
    repo = tmp_path / 'repo'
    repo.mkdir()
    worktrees = tmp_path / 'worktrees' / 'repo'
    worktrees.mkdir(parents=True)

    def repo_of_dir(cwd):
        path = Path(cwd)
        if path == repo or repo in path.parents:
            return 'repo', ''
        if path.parent == worktrees and path.name.removeprefix('repo-').isdigit():
            return 'repo', path.name.removeprefix('repo-')
        return None, None

    cfg = SimpleNamespace(repos={'repo': str(repo)}, hosts={}, worktree_root=str(worktrees.parent),
                          repo_of_dir=repo_of_dir)
    value = history.HistoryCommand(cfg, {'_t_source_root': lambda: str(ROOT),
                                         '_parse_rows': lambda *a, **k: [],
                                         '_table_lines': t_mod._table_lines}, store)
    monkeypatch.setattr(value, 'identity', lambda alias: 'org/repo')
    return value


def conversation(sid='same-sid', agent='codex', repo='org/repo', **changes):
    value = dict(repo=repo, agent=agent, sid=sid, title='Implement history', updated=10,
                 associations=[], sources=[], prs=[], mentions=[])
    value.update(changes)
    return value


def transcript_file(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    return path


def codex_rows(cwd, sid='codex-session', branch='dev/repo-19'):
    return [{'type': 'session_meta', 'timestamp': '2026-01-01T00:00:00Z',
             'payload': {'id': sid, 'cwd': str(cwd), 'git': {'branch': branch, 'commit_hash': 'a' * 40}}},
            {'type': 'response_item', 'payload': {'role': 'user', 'content': [{'text': 'Implement history'}]}}]


def test_store_durable_identity_keeps_all_past_slots_and_reused_slots(history, store):
    one = dict(workspace='/missing/repo/1', slot='1', host='local', branch='old', commit='a' * 40)
    two = dict(workspace='/missing/repo/2', slot='2', host='local', branch='new', commit='b' * 40)
    store.merge(conversation(associations=[one], sources=[{'host': 'local', 'path': '/old.jsonl'}], updated=20))
    store.merge(conversation(associations=[two, one], sources=[{'host': 'local', 'path': '/new.jsonl'}], updated=5))
    store.merge(conversation(sid='replacement', associations=[one]))
    store.merge(conversation(agent='claude', associations=[one]))
    store.merge(conversation(repo='other/repo', associations=[one]))
    directory = store.directory
    store.close()
    reopened = history.Store(directory)
    try:
        rows = reopened.records()
        assert len(rows) == 4
        exact = next(r for r in rows if (r['repo'], r['agent'], r['sid']) == ('org/repo', 'codex', 'same-sid'))
        assert exact['associations'] == [one, two]
        assert exact['updated'] == 20
        assert exact['sources'] == [{'host': 'local', 'path': '/old.jsonl'}, {'host': 'local', 'path': '/new.jsonl'}]
        assert exact['version'] == history.VERSION
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE((directory / 'history.sqlite3').stat().st_mode) == 0o600
    finally:
        reopened.close()


@pytest.mark.parametrize('change', [{'sid': ''}, {'sid': '../escape'}, {'sid': 'x\n'}, {'agent': 'other'}, {'repo': ''}])
def test_store_rejects_invalid_conversation_identity(history, store, change):
    value = conversation()
    value.update(change)
    with pytest.raises(history.HistoryError, match='identity'):
        store.merge(value)
    assert store.records() == []


def test_scan_reuses_cache_but_rescans_modified_transcript(history, store, tmp_path, monkeypatch):
    path = transcript_file(tmp_path / 'session.jsonl', codex_rows(tmp_path))
    calls = []
    original = history.transcript
    monkeypatch.setattr(history, 'transcript', lambda *a: (calls.append(a), original(*a))[1])
    assert store.scan(path, 'codex')['sid'] == 'codex-session'
    assert store.scan(path, 'codex')['title'] == 'Implement history'
    with path.open('a') as stream:
        stream.write(json.dumps({'timestamp': 1234567890123}) + '\n')
    assert store.scan(path, 'codex')['updated'] == 1767225600
    assert len(calls) == 2


@pytest.mark.parametrize('value, expected', [('2026-01-01T00:00:00Z', 1767225600), (1767225600000, 1767225600),
                                          (42, 42), ('garbage', 0), (None, 0)])
def test_timestamp_formats(history, value, expected):
    assert history.timestamp(value) == expected


@pytest.mark.parametrize('value', ['https://github.com/Org/Repo.git', 'git@github.com:Org/Repo.git',
                                 'ssh://git@github.com/Org/Repo/', 'https://github.com/Org/Repo'])
def test_repository_origin_normalization(history, value):
    assert history.slug(value) == 'org/repo'
    assert history.slug('https://elsewhere.test/Org/Repo.git') is None


@pytest.mark.parametrize('agent', ['codex', 'claude'])
def test_structured_pr_evidence_is_distinct_from_incidental_links(history, tmp_path, agent):
    incidental = 'https://github.com/org/repo/pull/1'
    created = 'https://github.com/org/repo/pull/2'
    edited = 'https://github.com/other/repo/pull/3'
    if agent == 'codex':
        rows = codex_rows(tmp_path)
        rows += [{'type': 'response_item', 'payload': {'role': 'assistant', 'content': [{'text': incidental}]}},
                 {'type': 'response_item', 'payload': {'type': 'function_call', 'call_id': 'create', 'arguments': json.dumps({'cmd': 'gh pr create --title History'})}},
                 {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'create', 'output': created}},
                 {'type': 'response_item', 'payload': {'type': 'function_call', 'call_id': 'edit', 'arguments': json.dumps({'cmd': 'gh pr edit ' + edited})}},
                 {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'edit', 'output': 'Process exited with code 0'}},
                 {'type': 'response_item', 'payload': {'type': 'function_call', 'call_id': 'noise', 'arguments': json.dumps({'cmd': 'printf ' + incidental})}},
                 {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'noise', 'output': incidental}}]
    else:
        rows = [{'type': 'user', 'sessionId': 'claude-session', 'cwd': str(tmp_path), 'gitBranch': 'feature', 'message': {'role': 'user', 'content': 'Implement history'}},
                {'type': 'assistant', 'message': {'role': 'assistant', 'content': [{'text': incidental}, {'type': 'tool_use', 'id': 'create', 'input': {'command': 'gh pr create'}}, {'type': 'tool_use', 'id': 'edit', 'input': {'command': 'gh pr edit ' + edited}}]}},
                {'type': 'user', 'message': {'role': 'user', 'content': [{'type': 'tool_result', 'tool_use_id': 'create', 'content': created}, {'type': 'tool_result', 'tool_use_id': 'edit', 'content': 'edited'}]}}]
    value = history.transcript(transcript_file(tmp_path / 'tx.jsonl', rows), agent)
    assert {(p['repo'], p['number']) for p in value['prs']} == {('org/repo', 2), ('other/repo', 3)}
    assert incidental in [p['url'] for p in value['mentions']]
    assert value['title'] == 'Implement history'
    assert value['associations'][0]['workspace'] == str(tmp_path)
    assert value['associations'][0]['evidence'] == 'transcript'


def test_parser_recovers_from_malformed_lines_and_does_not_guess_git_or_pr(history, tmp_path):
    path = transcript_file(tmp_path / 'rollout.jsonl', [None, [], {'type': 'session_meta', 'payload': {'id': 'sid', 'cwd': '/gone/worktree', 'git': {'branch': 'dev/repo-123', 'commit_hash': 'bad'}}},
        {'type': 'response_item', 'payload': 'bad'}, {'type': 'response_item', 'payload': {'role': 'user', 'content': '<environment>private startup</environment>'}},
        {'type': 'response_item', 'payload': {'role': 'user', 'content': '# AGENTS.md instructions'}},
        {'type': 'response_item', 'payload': {'role': 'user', 'content': 'Fix branch PR #123'}},
        {'type': 'response_item', 'payload': {'type': 'function_call', 'arguments': '{not-json'}},
        {'type': 'response_item', 'payload': {'type': 'function_call', 'arguments': ['wrong']}},
        {'type': 'response_item', 'payload': {'role': 'assistant', 'content': [None]}}])
    with path.open('a') as stream:
        stream.write('{truncated\n')
    value = history.transcript(path, 'codex')
    assert value['sid'] == 'sid'
    assert value['title'] == 'Fix branch PR #123'
    assert value['prs'] == []
    assert value['associations'][0]['branch'] == 'dev/repo-123'
    assert value['associations'][0]['commit'] == ''
    assert history.transcript(tmp_path / 'missing', 'codex')['sid'] == ''


def test_claude_filename_fallback_and_explicit_title(history, tmp_path):
    path = transcript_file(tmp_path / 'claude-id.jsonl', [{'type': 'custom-title', 'customTitle': '  Better\x1b title  '},
        {'type': 'user', 'cwd': str(tmp_path), 'message': {'role': 'user', 'content': 'Old title'}}])
    value = history.transcript(path, 'claude')
    assert value['sid'] == 'claude-id'
    assert value['title'] == 'Better title'


def test_discovery_current_archived_claude_and_index_only_missing_worktrees(history, app):
    root = Path(app.cfg.worktree_root) / 'repo'
    codex = Path(os.environ['CODEX_HOME'])
    current = transcript_file(codex / 'sessions/2026/rollout-current.jsonl', codex_rows(root / '19', 'current'))
    archived = transcript_file(codex / 'archived_sessions/rollout-old.jsonl', codex_rows(root / '1', 'archived'))
    claude_project = re.sub(r'[^A-Za-z0-9]', '-', str(root / '2'))
    transcript_file(Path.home() / '.claude/projects' / claude_project / 'claude.jsonl', [{'sessionId': 'claude', 'cwd': str(root / '2'), 'type': 'user', 'message': {'role': 'user', 'content': 'Claude historical session'}}])
    transcript_file(codex / 'sessions/rollout-other.jsonl', codex_rows('/unregistered/path', 'unrelated'))
    transcript_file(codex / 'sessions/rollout-broken.jsonl', [{'payload': []}])
    # A corrupt newer index must not prevent discovery of the older usable index.
    (codex / 'state_99.sqlite').write_text('not sqlite')
    with sqlite3.connect(codex / 'state_5.sqlite') as db:
        db.execute('create table threads(id text, cwd text, title text, updated_at integer, archived integer, git_branch text, git_sha text, rollout_path text, source text)')
        db.executemany('insert into threads values(?,?,?,?,?,?,?,?,?)', [
            ('index-only', str(root / '3'), 'Index retained', 20, 1, 'dev/repo-3', 'b' * 40, '/missing/tx.jsonl', ''),
            ('subagent', str(root / '3'), 'Child', 30, 0, '', '', '', '{"subagent":{}}')])
    db.close()
    app.discover('repo')
    app.discover('repo')
    rows = app.records('repo')
    assert {r['sid'] for r in rows} == {'current', 'archived', 'claude', 'index-only'}
    assert all(len(r['associations']) == 1 for r in rows)
    assert all(not r['associations'][0]['available'] for r in rows)
    index = next(r for r in rows if r['sid'] == 'index-only')
    assert index['archived'] is True
    assert index['sources'][0]['available'] is False
    assert index['prs'] == []
    current.unlink()
    archived.unlink()
    assert len(app.records('repo')) == 4
    assert not next(r for r in app.records('repo') if r['sid'] == 'current')['sources'][0]['available']
    assert [r['sid'] for r in app.records('repo', '3')] == ['index-only']


def test_records_scope_exact_agent_and_sid_and_keep_cached_remote_paths(history, app, store):
    association = {'workspace': '/not/local', 'slot': '7', 'host': 'remote', 'branch': 'old'}
    store.merge(conversation(associations=[association], sources=[{'host': 'remote', 'path': '/secret/transcript'}]))
    store.merge(conversation(agent='claude', associations=[association]))
    store.merge(conversation(repo='other/repo', associations=[association]))
    owner_workspace = Path(app.cfg.worktree_root) / 'repo/1'
    owner_workspace.mkdir()
    owners = [{'agent': 'codex', 'sid': 'same-sid', 'host': 'local', 'slot': 'repo-1',
               'cwd': str(owner_workspace), 'state': 'detached', 'context': '',
               'model': 'fixture-owner-model', 'summary': 'Exact owner working-on text'}]
    rows = app.records('repo', '7', owners)
    assert len(rows) == 2
    assert next(r for r in rows if r['agent'] == 'codex')['owners'] == owners
    assert next(r for r in rows if r['agent'] == 'claude')['owners'] == []
    assert all(r['associations'][0]['available'] == 'cached' for r in rows)
    assert rows[0]['sources'][0]['available'] == 'cached'
    assert app.records('repo', '8') == []


def test_remote_refresh_quotes_zsh_metadata_export_and_preserves_offline_cache(history, app, store, monkeypatch, capsys):
    app.cfg.hosts = {'remote': 'user@example', 'offline': 'offline.example'}
    calls = []
    def command(argv, **kwargs):
        calls.append(argv)
        assert argv[0] == 'ssh'
        assert kwargs['timeout'] == 20
        outer = shlex.split(argv[-1])
        assert outer[:2] == ['zsh', '-lic']
        assert shlex.split(outer[2]) == ['command', 't', '__history-export', 'org/repo']
        if argv[-2] == 'offline.example':
            raise history.HistoryError('offline')
        return json.dumps([conversation(associations=[{'workspace': '/remote/repo/1', 'slot': '1', 'host': 'local'}], sources=[{'host': 'local', 'path': '/remote/tx.jsonl'}], owners=[{'fake': True}]), conversation(repo='different/repo')])
    monkeypatch.setattr(history, 'run', command)
    app.refresh('repo')
    app.refresh('repo')
    rows = store.records()
    assert len(rows) == 1
    assert rows[0]['associations'] == [{'workspace': '/remote/repo/1', 'slot': '1', 'host': 'remote'}]
    assert rows[0]['sources'] == [{'host': 'remote', 'path': '/remote/tx.jsonl'}]
    assert 'owners' not in rows[0]
    assert len(calls) == 4
    assert 'offline unavailable; retained cached history' in capsys.readouterr().err
    # Browse never fetches remote transcript content or launches a session.
    app.records('repo')
    assert len(calls) == 4


@pytest.mark.parametrize('response', ['not-json', '{}', '[null]', '[{"repo":"org/repo","agent":"wrong","sid":"id"}]'])
def test_bad_remote_metadata_retains_existing_history(history, app, store, monkeypatch, capsys, response):
    store.merge(conversation())
    app.cfg.hosts = {'remote': 'example'}
    monkeypatch.setattr(history, 'run', lambda *a, **k: response)
    app.refresh('repo')
    assert len(store.records()) == 1
    assert 'retained cached history' in capsys.readouterr().err


def test_pr_status_batches_all_repositories_and_reuses_offline_cache(history, app, store, monkeypatch):
    refs = [dict(repo='org/repo', number=n, url=f'https://github.com/org/repo/pull/{n}') for n in range(1, 54)]
    refs += [dict(repo='other/repo', number=1, url='https://github.com/other/repo/pull/1')]
    rows = [conversation(prs=refs)]
    calls = []
    def gh(argv, **kwargs):
        calls.append(argv)
        assert argv[:3] == ['gh', 'api', 'graphql']
        import re
        numbers = re.findall(r'p(\d+):pullRequest', argv[-1])
        assert len(numbers) <= 50
        return json.dumps({'data': {'repository': {f'p{n}': {'state': 'CLOSED' if n == '2' else 'OPEN', 'mergedAt': 'date' if n == '2' else None} for n in numbers}}})
    monkeypatch.setattr(history, 'run', gh)
    app.pr_states(rows, refresh=True)
    assert len(calls) == 3
    assert next(p for p in rows[0]['prs'] if p['number'] == 2)['state'] == 'MERGED'
    app.pr_states(rows)
    assert len(calls) == 3
    monkeypatch.setattr(history, 'run', lambda *a, **k: (_ for _ in ()).throw(history.HistoryError('offline')))
    app.pr_states(rows, refresh=True)
    assert all(p['state'] in ('OPEN', 'MERGED') for p in rows[0]['prs'])


def test_run_strips_coverage_environment_and_errors_without_exposing_output(history, monkeypatch):
    monkeypatch.setenv('COV_CORE_SOURCE', '/private/coverage')
    observed = []
    def run(argv, **kwargs):
        observed.append(kwargs)
        assert not any(k.startswith('COV_CORE_') for k in kwargs['env'])
        return SimpleNamespace(returncode=0, stdout='metadata')
    monkeypatch.setattr(history.subprocess, 'run', run)
    assert history.run(['metadata']) == 'metadata'
    assert observed[0]['stdin'] == subprocess.DEVNULL
    monkeypatch.setattr(history.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=1, stdout='sensitive'))
    assert history.run(['metadata'], optional=True) == ''
    with pytest.raises(history.HistoryError, match='retained') as exc:
        history.run(['metadata'])
    assert 'sensitive' not in str(exc.value)
    monkeypatch.setattr(history.subprocess, 'run', lambda *a, **k: (_ for _ in ()).throw(subprocess.TimeoutExpired('metadata', 1)))
    assert history.run(['metadata'], optional=True) == ''


def test_private_atomic_json_and_sanitized_details(history, tmp_path):
    path = tmp_path / 'new/private.json'
    history.private_write(path, {'name': 'test'})
    assert json.loads(path.read_text()) == {'name': 'test'}
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(path.parent.iterdir()) == [path]
    row = conversation(associations=[{'workspace': '/gone', 'slot': '1', 'host': 'local', 'evidence': 'index'}],
                       sources=[{'host': 'remote', 'path': '/tx'}], prs=[{'url': 'https://github.com/org/repo/pull/2', 'number': 2}],
                       mentions=[{'url': 'https://github.com/org/repo/pull/2'}, {'url': 'https://github.com/org/repo/pull/3'}])
    rendered = history.details(row)
    assert 'Revision: unverified' in rendered
    assert rendered.count('Incidental PR mention:') == 1
    assert 'Branch: unknown' in rendered
    assert history.clean(' title\x1b\n secret\t words ') == 'title secret words'


@pytest.mark.parametrize('changes', [
    {'associations': ['bad']}, {'sources': {}}, {'associations': [{'workspace': '/repo/1', 'observed': {}}]},
    {'associations': [{'workspace': '/repo/1', 'branch': []}]},
    {'sources': [{'host': 'remote', 'path': []}]},
    {'prs': [{'repo': 'org/repo', 'number': 'not-integer'}]},
    {'mentions': [{'repo': 'org/repo', 'number': 0}]}, {'updated': {}},
    {'updated': float('nan')}, {'updated': float('inf')},
    {'associations': [{'workspace': '/repo/1', 'slot': 'not-a-slot'}]},
])
def test_malformed_nested_remote_metadata_is_rejected_without_poisoning_cache(history, app, store, monkeypatch, capsys, changes):
    original = store.merge(conversation(associations=[{'workspace': '/original', 'slot': '1', 'host': 'local'}]))
    app.cfg.hosts = {'remote': 'remote.example'}
    monkeypatch.setattr(history, 'run', lambda *a, **k: json.dumps([conversation(**changes)]))
    app.refresh('repo')
    assert store.records() == [original]
    assert 'retained cached history' in capsys.readouterr().err
    rows = app.records('repo')
    assert history.page_rows(rows)[1] == 1
    history.format_row(rows[0])
    app.revision(rows[0])


def test_store_pr_dedup_uses_repo_and_number_not_status_or_url_spelling(store):
    store.merge(conversation(prs=[{'repo': 'org/repo', 'number': 1, 'url': 'https://github.com/org/repo/pull/1'}]))
    value = store.merge(conversation(prs=[{'repo': 'org/repo', 'number': 1, 'url': 'https://github.com/Org/Repo/pull/1', 'state': 'MERGED'},
                                         {'repo': 'other/repo', 'number': 1, 'url': 'https://github.com/other/repo/pull/1'}]))
    assert len(value['prs']) == 2
    assert value['prs'][0]['state'] == 'MERGED'
    assert value['prs'][0]['url'] == 'https://github.com/org/repo/pull/1'
    assert value['prs'][1]['repo'] == 'other/repo'


def test_incremental_scan_handles_partial_final_line_truncation_and_inode_replacement(history, store, tmp_path):
    path = transcript_file(tmp_path / 'rollout-incremental.jsonl', codex_rows(tmp_path))
    original = store.scan(path, 'codex')
    pending = json.dumps({'type': 'response_item', 'payload': {'type': 'function_call', 'call_id': 'tool', 'arguments': json.dumps({'cmd': 'gh pr create'})}}).encode()
    with path.open('ab') as stream:
        stream.write(pending[:20])
    partial = store.scan(path, 'codex')
    assert partial['sid'] == original['sid']
    with path.open('ab') as stream:
        stream.write(pending[20:] + b'\n')
        stream.write((json.dumps({'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'tool', 'output': 'https://github.com/org/repo/pull/7'}}) + '\n').encode())
    completed = store.scan(path, 'codex')
    assert [p['number'] for p in completed['prs']] == [7]
    assert len(completed['associations']) == 1
    # Truncation and replacement invalidate the append offset and old identity.
    transcript_file(path, codex_rows(tmp_path, sid='replacement'))
    assert store.scan(path, 'codex')['sid'] == 'replacement'
    new = transcript_file(tmp_path / 'new.jsonl', codex_rows(tmp_path, sid='new-inode'))
    new.replace(path)
    assert store.scan(path, 'codex')['sid'] == 'new-inode'


def test_conflicting_transcript_identity_is_not_discovered(history, app):
    codex = Path(os.environ['CODEX_HOME'])
    cwd = Path(app.cfg.worktree_root) / 'repo/1'
    transcript_file(codex / 'sessions/rollout-conflict.jsonl', codex_rows(cwd, 'one') + codex_rows(cwd, 'two'))
    app.discover('repo')
    assert app.records('repo') == []


def test_custom_exec_tool_and_numeric_pr_operations_resolve_explicit_repo(history, app, tmp_path):
    path = transcript_file(tmp_path / 'rollout-numeric.jsonl', codex_rows(Path(app.cfg.worktree_root) / 'repo/1') + [
        {'type': 'response_item', 'payload': {'type': 'custom_tool_call', 'call_id': 'create', 'input': 'text(await tools.exec_command({cmd: "gh pr create --title History"}));'}},
        {'type': 'response_item', 'payload': {'type': 'custom_tool_call_output', 'call_id': 'create', 'output': 'https://github.com/org/repo/pull/2'}},
        {'type': 'response_item', 'payload': {'type': 'function_call', 'call_id': 'checkout', 'arguments': json.dumps({'cmd': 'gh pr checkout 3'})}},
        {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'checkout', 'output': 'Process exited with code 0'}},
        {'type': 'response_item', 'payload': {'type': 'function_call', 'call_id': 'edit', 'arguments': json.dumps({'cmd': 'gh pr edit 4 --repo other/repo'})}},
        {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'edit', 'output': 'Process exited with code 0'}},
    ])
    app.ingest(history.transcript(path, 'codex'), 'repo', path)
    assert {(p['repo'], p['number']) for p in app.records('repo')[0]['prs']} == {('org/repo', 2), ('org/repo', 3), ('other/repo', 4)}


def test_printing_gh_command_text_is_not_structured_pr_association(history, tmp_path):
    path = transcript_file(tmp_path / 'rollout-incidental.jsonl', codex_rows(tmp_path) + [
        {'type': 'response_item', 'payload': {'type': 'function_call', 'call_id': 'print', 'arguments': json.dumps({'cmd': "printf '%s' 'gh pr create https://github.com/org/repo/pull/99'"})}},
        {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'print', 'output': 'https://github.com/org/repo/pull/99'}},
    ])
    assert history.transcript(path, 'codex')['prs'] == []


def test_empty_index_title_does_not_erase_durable_title(store):
    store.merge(conversation(title='Retained conversation title'))
    assert store.merge(conversation(title=''))['title'] == 'Retained conversation title'


@pytest.mark.parametrize('command,expected', [
    ('env SAFE=1 command gh pr checkout 8 -R Org/Repo', [('org/repo', 8)]),
    ('gh pr edit 9 --repo=Other/Repo', [('other/repo', 9)]),
    # Overall shell success cannot prove each conditional or mixed gh call succeeded.
    ('echo harmless && gh pr checkout 10; gh pr edit 11', []),
    ("printf 'gh pr checkout 8'", []), ('gh pr edit "unterminated', []),
    (None, []), ('gh pr view 5', []),
])
def test_actual_gh_tool_commands_scope_explicit_numeric_targets(history, command, expected):
    evidence = history.tool_pr_command(command)
    assert [(number['repo'], number['number']) for item in evidence for number in item['numbers']] == expected


@pytest.mark.parametrize('agent', ['codex', 'claude'])
def test_failed_structured_tool_output_does_not_establish_pr_evidence(history, tmp_path, agent):
    url = 'https://github.com/org/repo/pull/7'
    if agent == 'codex':
        data = codex_rows(tmp_path) + [
            {'type': 'response_item', 'payload': {'type': 'function_call', 'call_id': 'failed', 'arguments': json.dumps({'cmd': 'gh pr checkout ' + url})}},
            {'type': 'response_item', 'payload': {'type': 'function_call_output', 'call_id': 'failed', 'output': 'Process exited with code 1\n' + url}}]
    else:
        data = [{'type': 'assistant', 'sessionId': 'failed', 'cwd': str(tmp_path), 'message': {'content': [{'type': 'tool_use', 'id': 'failed', 'input': {'command': 'gh pr checkout ' + url}}]}},
                {'type': 'user', 'message': {'content': [{'type': 'tool_result', 'tool_use_id': 'failed', 'is_error': True, 'content': url}]}}]
    assert history.transcript(transcript_file(tmp_path / 'failed.jsonl', data), agent)['prs'] == []


def test_claude_conflicting_ids_and_invalid_message_shape(history, tmp_path):
    invalid = transcript_file(tmp_path / 'invalid.jsonl', [{'sessionId': 'one', 'type': 'user', 'cwd': str(tmp_path), 'message': None}])
    assert history.transcript(invalid, 'claude')['sid'] == 'one'
    with invalid.open('a') as stream:
        stream.write(json.dumps({'sessionId': 'two', 'message': {}}) + '\n')
    with pytest.raises(history.HistoryError, match='conflicting'):
        history.transcript(invalid, 'claude')


def test_ingest_rejects_untrusted_identity_and_repository_evidence(history, app, tmp_path):
    path = transcript_file(tmp_path / 'invalid.jsonl', codex_rows(Path(app.cfg.worktree_root) / 'repo/1', sid='../bad'))
    app.ingest(history.transcript(path, 'codex'), 'repo', path)
    assert app.records('repo') == []
    value = history.transcript(path, 'codex')
    value.update(sid='valid', recorded_repo='other/repo')
    app.ingest(value, 'repo', path)
    assert app.records('repo') == []


def test_branch_name_only_never_guesses_pr_but_verified_exact_head_can_associate(history, app, monkeypatch):
    rows = [conversation(associations=[{'workspace': '/gone/1', 'branch': 'reused', 'commit': 'a' * 40}]),
            conversation(sid='no-commit', associations=[{'workspace': '/gone/2', 'branch': 'reused', 'commit': ''}]),
            conversation(sid='wrong-commit', associations=[{'workspace': '/gone/3', 'branch': 'reused', 'commit': 'b' * 40}])]
    calls = []
    def gh(argv, **kwargs):
        calls.append(argv)
        assert argv[:3] == ['gh', 'api', 'graphql']
        return json.dumps({'data': {'repository': {'b0': {'nodes': [dict(number=4, url='https://github.com/org/repo/pull/4', headRefName='reused', headRefOid='a' * 40, state='MERGED')]}}}})
    monkeypatch.setattr(history, 'run', gh)
    app.branch_prs(rows, refresh=True)
    assert [p['number'] for p in rows[0]['prs']] == [4]
    assert rows[1]['prs'] == rows[2]['prs'] == []
    assert rows[0]['prs'][0]['evidence'] == 'verified branch and commit'
    app.branch_prs(rows)
    assert len(calls) == 1
    monkeypatch.setattr(history, 'run', lambda *a, **k: (_ for _ in ()).throw(history.HistoryError('offline')))
    app.branch_prs(rows, refresh=True)
    assert [p['number'] for p in rows[0]['prs']] == [4]
    app.branch_prs([conversation(repo='local:/repo', associations=[{'branch': 'main', 'commit': 'a' * 40}])])


def test_missing_graphql_pr_does_not_invent_status(history, app, monkeypatch):
    row = conversation(prs=[dict(repo='org/repo', number=1, url='https://github.com/org/repo/pull/1')])
    monkeypatch.setattr(history, 'run', lambda *a, **k: json.dumps({'data': {'repository': {'p1': None}}}))
    app.pr_states([row], refresh=True)
    assert 'state' not in row['prs'][0]


def test_record_association_captures_git_evidence_and_store_is_private(history, tmp_path, monkeypatch):
    workspace = tmp_path / 'repo/19'
    workspace.mkdir(parents=True)
    calls = []
    def git(argv, **kwargs):
        calls.append(argv)
        assert argv[:3] == ['git', '-C', str(workspace)]
        if argv[3:] == ['remote', 'get-url', 'origin']:
            return 'git@github.com:org/repo.git\n'
        if argv[3:] == ['rev-parse', 'HEAD']:
            return 'c' * 40 + '\n'
        return 'dev/repo-19\n'
    monkeypatch.setattr(history, 'run', git)
    history.record_association('recorded', 'codex', str(workspace), '/missing/tx', 'move')
    store = history.Store()
    try:
        row, = store.records()
        assert row['repo'] == 'org/repo'
        assert [(s['host'], s['path']) for s in row['sources']] == [('local', '/missing/tx')]
        assert row['sources'][0]['updated'] > 0
        assert row['associations'][0]['slot'] == '19'
        assert row['associations'][0]['commit'] == 'c' * 40
        assert row['associations'][0]['evidence'] == 'move'
    finally:
        store.close()
    assert len(calls) == 3
    history.record_association('../invalid', 'codex', str(workspace))
    history.record_association('good', 'invalid-agent', str(workspace))
    assert len(calls) == 3
    monkeypatch.setattr(history, 'run', lambda argv, **k: 'https://not-github.example/org/repo' if argv[3:] == ['remote', 'get-url', 'origin'] else '')
    history.record_association('not-github', 'codex', str(workspace))


def test_identity_local_repo_fallback_and_owners_shell_contract(history, app, monkeypatch):
    calls = []
    app.identity = history.HistoryCommand.identity.__get__(app)
    monkeypatch.setattr(history, 'run', lambda *a, **k: '')
    assert app.identity('repo') == 'local:' + os.path.realpath(app.cfg.repos['repo'])
    monkeypatch.setattr(history, 'run', lambda *a, **k: 'https://github.com/Org/Repo.git\n')
    assert app.identity('repo') == 'org/repo'
    def shell(argv, **kwargs):
        calls.append(argv)
        assert argv[:2] == ['zsh', '-lic']
        assert 'source ' + shlex.quote(str(ROOT / 't.plugin.zsh')) in argv[2]
        return 'metadata'
    monkeypatch.setattr(history, 'run', shell)
    app.api['_parse_rows'] = lambda text, host_prefixed: [{'sid': text, 'host': 'local'}]
    assert app.owners() == [{'sid': 'metadata', 'host': 'local'}]
    assert '_dev_session_rows' in calls[-1][2]
    app.owners(True)
    assert '_dev_rows_all' in calls[-1][2]


def test_first_discovery_preserves_all_workspace_moves_inside_one_transcript(history, app, tmp_path):
    root = Path(app.cfg.worktree_root) / 'repo'
    path = transcript_file(tmp_path / 'claude-moved.jsonl', [
        {'type': 'user', 'sessionId': 'moved', 'cwd': str(root / '1'), 'gitBranch': 'old-branch', 'timestamp': 10, 'message': {'role': 'user', 'content': 'Continue work'}},
        {'type': 'assistant', 'sessionId': 'moved', 'cwd': str(root / '2'), 'gitBranch': 'new-branch', 'timestamp': 20, 'message': {'role': 'assistant', 'content': 'Continued after move'}},
    ])
    app.ingest(history.transcript(path, 'claude'), 'repo', path)
    row, = app.records('repo')
    assert {(a['slot'], a['branch']) for a in row['associations']} == {('1', 'old-branch'), ('2', 'new-branch')}
    assert [r['sid'] for r in app.records('repo', '1')] == ['moved']
    assert [r['sid'] for r in app.records('repo', '2')] == ['moved']


def test_claude_invalid_content_block_does_not_crash_metadata_discovery(history, tmp_path):
    path = transcript_file(tmp_path / 'scalar-block.jsonl', [
        {'sessionId': 'valid', 'cwd': str(tmp_path), 'message': {'content': [None, 42, {'text': 'Retained title'}], 'role': 'user'}},
    ])
    value = history.transcript(path, 'claude')
    assert value['sid'] == 'valid'
    assert value['title'] == 'Retained title'


def test_private_write_failure_keeps_original_and_removes_pending_file(history, tmp_path):
    path = tmp_path / 'atomic' / 'retained.json'
    history.private_write(path, {'retained': True})
    with pytest.raises(TypeError):
        history.private_write(path, {'unsupported': object()})
    assert json.loads(path.read_text()) == {'retained': True}
    assert list(path.parent.iterdir()) == [path]


@pytest.mark.parametrize('field', ['associations', 'sources', 'prs', 'mentions'])
def test_store_rejects_metadata_container_of_wrong_shape(history, store, field):
    value = conversation()
    value[field] = {}
    with pytest.raises(history.HistoryError, match='metadata'):
        store.merge(value)
    assert store.records() == []


def test_store_repairs_existing_directory_and_database_permissions(history, tmp_path):
    directory = tmp_path / 'existing-private-history'
    directory.mkdir(mode=0o755)
    database = directory / 'history.sqlite3'
    database.touch(mode=0o644)
    value = history.Store(directory)
    try:
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert stat.S_IMODE(database.stat().st_mode) == 0o600
    finally:
        value.close()


def test_remote_export_omits_other_hosts_cached_associations_and_transcript_paths(app, store):
    local = {'host': 'local', 'workspace': '/local/repo/1', 'slot': '1'}
    remote = {'host': 'remote', 'workspace': '/remote/repo/2', 'slot': '2'}
    store.merge(conversation(associations=[local, remote], sources=[{'host': 'local', 'path': '/local/tx'}, {'host': 'remote', 'path': '/remote/tx'}]))
    store.merge(conversation(sid='remote-only', associations=[remote], sources=[{'host': 'remote', 'path': '/remote/only'}]))
    exported = app.export('repo')
    assert len(exported) == 1
    assert exported[0]['sid'] == 'same-sid'
    assert exported[0]['associations'][0]['workspace'] == '/local/repo/1'
    assert [(s['host'], s['path']) for s in exported[0]['sources']] == [('local', '/local/tx')]
    assert len(store.records()) == 2
    assert len(store.records()[0]['associations']) == 2


def test_record_association_local_repository_and_wrong_transcript_identity(history, tmp_path, monkeypatch):
    repo = tmp_path / 'repo'
    workspace = repo / '19'
    workspace.mkdir(parents=True)
    def git(argv, **kwargs):
        if argv[3:] == ['remote', 'get-url', 'origin']:
            return ''
        if '--git-common-dir' in argv:
            return str(repo / '.git')
        if argv[3:] == ['rev-parse', 'HEAD']:
            return 'e' * 40
        return 'dev/repo-19'
    monkeypatch.setattr(history, 'run', git)
    history.record_association('local', 'claude', str(workspace))
    store = history.Store()
    try:
        assert store.records()[0]['repo'] == 'local:' + str(repo.resolve())
        assert store.records()[0]['associations'][0]['commit'] == 'e' * 40
        wrong = transcript_file(tmp_path / 'wrong.jsonl', codex_rows(workspace, sid='other'))
        history.record_association('selected', 'codex', str(workspace), str(wrong))
        assert [r['sid'] for r in store.records()] == ['local']
    finally:
        store.close()


def test_store_partial_source_metadata_preserves_freshness_and_size(store):
    source = dict(host='local', path='/gone/rollout-exact.jsonl', updated=100, size=200, available=True)
    store.merge(conversation(sources=[source]))
    merged = store.merge(conversation(sources=[dict(host='local', path=source['path'], available=False)]))
    retained, = merged['sources']
    assert retained['updated'] == 100
    assert retained['size'] == 200
    assert retained['available'] is False


def test_codex_index_discovery_preserves_freshness_of_newest_exact_source(history, app):
    codex = Path(os.environ['CODEX_HOME'])
    cwd = Path(app.cfg.worktree_root) / 'repo/1'
    old_rows = codex_rows(cwd, sid='exact')
    old_rows[0]['timestamp'] = '2025-01-01T00:00:00Z'
    older = transcript_file(codex / 'sessions/rollout-exact-older.jsonl', old_rows)
    newest = transcript_file(codex / 'sessions/rollout-exact-newest.jsonl', codex_rows(cwd, sid='exact') + [
        {'type': 'response_item', 'timestamp': '2026-02-01T00:00:00Z', 'payload': {'role': 'assistant', 'content': [{'text': 'Newest retained conversation state'}]}}])
    database = sqlite3.connect(codex / 'state_5.sqlite')
    try:
        database.execute('create table threads(id text, cwd text, title text, updated_at integer, archived integer, git_branch text, git_sha text, rollout_path text, source text)')
        database.execute('insert into threads values(?,?,?,?,?,?,?,?,?)', ('exact', str(cwd), 'Index title', 1769904000, 0, 'dev/repo-1', 'a' * 40, str(newest), ''))
        database.commit()
    finally:
        database.close()
    app.discover('repo')
    row, = app.records('repo')
    sources = {s['path']: s for s in row['sources']}
    assert set(sources) == {str(older), str(newest)}
    assert sources[str(newest)]['updated'] == 1769904000
    assert sources[str(newest)]['size'] == newest.stat().st_size
    assert sources[str(older)]['updated'] == 1735689600
    assert max(row['sources'], key=lambda s: (s.get('updated', 0), s.get('size', 0)))['path'] == str(newest)
    # Index metadata is merged on every refresh and must not degrade the cache.
    app.discover('repo')
    latest = next(s for s in app.records('repo')[0]['sources'] if s['path'] == str(newest))
    assert latest['updated'] == 1769904000
    assert latest['size'] == newest.stat().st_size


def test_ingest_filters_foreign_workspaces_and_never_guesses_local_repo_pr_numbers(history, app):
    workspace = str(Path(app.cfg.worktree_root) / 'repo/1')
    value = conversation(associations=[{'workspace': '/another/repo/9', 'branch': 'foreign'}, {'workspace': workspace, 'branch': 'exact'}])
    app.ingest(value, 'repo')
    row, = app.records('repo')
    assert [(a['slot'], a['branch']) for a in row['associations']] == [('1', 'exact')]
    app.ingest(conversation(sid='foreign-only', associations=[{'workspace': '/another/repo/9'}]), 'repo')
    assert [r['sid'] for r in app.records('repo')] == ['same-sid']
    app.identity = lambda alias: 'local:' + app.cfg.repos[alias]
    app.ingest(conversation(sid='local', associations=[{'workspace': workspace}], pr_numbers=[{'number': 44, 'repo': None}]), 'repo')
    local, = app.records('repo')
    assert local['prs'] == []
    assert local['repo'].startswith('local:')


def test_discovery_skips_transcript_scans_for_unrelated_repositories(history, app, monkeypatch):
    cwd = Path(app.cfg.worktree_root) / 'repo/1'
    codex = Path(os.environ['CODEX_HOME'])
    relevant = transcript_file(codex / 'sessions/rollout-relevant.jsonl', codex_rows(cwd, 'relevant'))
    transcript_file(codex / 'sessions/rollout-unrelated.jsonl', codex_rows('/unregistered/repo/1', 'unrelated'))
    transcript_file(Path.home() / '.claude/projects/unrelated-project/claude.jsonl', [{'sessionId': 'other', 'cwd': '/unregistered/repo/1'}])
    observed = []
    original = app.store.scan
    def scan(path, agent):
        observed.append(path)
        return original(path, agent)
    monkeypatch.setattr(app.store, 'scan', scan)
    app.discover('repo')
    assert observed == [relevant]
    assert [r['sid'] for r in app.records('repo')] == ['relevant']


def test_cold_cache_pr_display_never_contacts_provider_without_explicit_refresh(history, app, monkeypatch):
    row = conversation(prs=[dict(repo='org/repo', number=1, url='https://github.com/org/repo/pull/1')],
                       associations=[{'workspace': '/gone/1', 'branch': 'feature', 'commit': 'a' * 40}])
    monkeypatch.setattr(history, 'run', lambda *a, **k: pytest.fail('initial history display must use cached PR metadata'))
    app.branch_prs([row])
    app.pr_states([row])
    assert row['prs'] == [dict(repo='org/repo', number=1, url='https://github.com/org/repo/pull/1')]


@pytest.mark.parametrize('workspace,expected_slot', [
    ('/old/code/.worktrees/renamed/renamed-42', '42'),
    ('/other/home/worktrees/previous/19', '19'),
    ('/old/code/renamed', ''),
])
def test_discover_renamed_codex_workspace_by_exact_repository(app, history, workspace, expected_slot):
    rows = codex_rows(workspace, sid='renamed')
    rows[0]['payload']['git']['repository_url'] = 'git@github.com:ORG/REPO.git'
    transcript_file(Path(os.environ['CODEX_HOME']) / 'sessions/rollout-renamed.jsonl', rows)
    app.discover('repo')
    found = app.records('repo')
    assert len(found) == 1
    assert found[0]['associations'][0]['workspace'] == workspace
    assert found[0]['associations'][0]['slot'] == expected_slot


@pytest.mark.parametrize('remote', ['', 'https://github.com/other/repo', 'https://github.com/org/repository'])
def test_discover_rejects_unrelated_renamed_workspace(app, remote):
    rows = codex_rows('/old/code/.worktrees/renamed/19')
    rows[0]['payload']['git']['repository_url'] = remote
    transcript_file(Path(os.environ['CODEX_HOME']) / 'sessions/rollout-unrelated.jsonl', rows)
    app.discover('repo')
    assert app.records('repo') == []


def test_discover_rejects_conflicting_repository_even_under_current_path(app):
    rows = codex_rows(Path(app.cfg.worktree_root) / 'repo/19')
    rows[0]['payload']['git']['repository_url'] = 'https://github.com/other/repo'
    transcript_file(Path(os.environ['CODEX_HOME']) / 'sessions/rollout-conflicting.jsonl', rows)
    app.discover('repo')
    assert app.records('repo') == []


def test_transcript_rejects_conflicting_repository_metadata(history, tmp_path):
    rows = codex_rows('/old/repo/19')
    rows[0]['payload']['git']['repository_url'] = 'https://github.com/org/repo'
    other = json.loads(json.dumps(rows[0]))
    other['payload']['git']['repository_url'] = 'https://github.com/other/repo'
    path = transcript_file(tmp_path / 'rollout-conflicting.jsonl', rows + [other])
    with pytest.raises(history.HistoryError, match='repository identities'):
        history.transcript(path, 'codex')


def test_resume_candidates_refresh_all_hosts_and_deduplicate_sources(app, store, monkeypatch):
    row = conversation(associations=[dict(workspace='/different/home/trees/old/old-42', slot='42', host='remote')],
                       sources=[dict(host='remote', path='/different/home/.codex/sessions/rollout-same-sid.jsonl')])
    events = []
    monkeypatch.setattr(app, 'discover', lambda alias, **kwargs: events.append(('local', alias)))
    def refresh(alias):
        events.append(('remote', alias))
        store.merge(row)
        store.merge({**row, 'sources': [dict(host='local', path='/local/rollout-same-sid.jsonl')]})
    monkeypatch.setattr(app, 'refresh', refresh)
    rows = list(app.resume_candidates(['repo']))
    assert events == [('local', 'repo'), ('remote', 'repo')]
    assert len(rows) == 1
    fields = rows[0].split('\t')
    assert fields[1:5] == ['repo', '42', 'same-sid', '/different/home/trees/old/old-42']
    assert fields[7:] == ['remote', 'codex', '-']
    with pytest.raises(ValueError, match='unknown repository'):
        list(app.resume_candidates(['missing']))


@pytest.mark.parametrize('child_source', [{'source': {'subagent': {'thread_spawn': {}}}}, {'thread_source': 'subagent'}])
def test_resume_discovery_excludes_child_rollouts_and_keeps_turn_evidence(app, child_source):
    rows = codex_rows('/old/trees/repo/repo-4', sid='main')
    rows[0]['payload']['git']['repository_url'] = 'https://github.com/org/repo'
    rows.append({'type': 'event_msg', 'timestamp': '2026-10-28T10:11:12Z',
                 'payload': {'type': 'task_started'}})
    path = Path(os.environ['CODEX_HOME']) / 'sessions/rollout-main.jsonl'
    transcript_file(path, rows)
    child = json.loads(json.dumps(rows))
    child[0]['payload'].update(id='child', **child_source)
    transcript_file(path.with_name('rollout-child.jsonl'), child)
    app.discover('repo', metadata_only=True)
    found = app.records('repo')
    assert [row['sid'] for row in found] == ['main']
    assert found[0]['last_turn'] == '2026-10-28T10:11:12Z'
    # A full history scan must use its separate cache and retain the same turn.
    app.discover('repo')
    assert app.records('repo')[0]['last_turn'] == found[0]['last_turn']
