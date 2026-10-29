"""Issue ownership uses fake provider responses and disposable repositories only."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def mod():
    spec = importlib.util.spec_from_file_location('issue_test', ROOT / 'libexec/t_issue.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def issue(tmp_path):
    return dict(repo='org/repo', number=12, alias='repo', directory=str(tmp_path),
                url='https://github.com/org/repo/issues/12', title='Repair broken login flow',
                body='Fix the login', comments=[[{'body': 'Details'}]], state='open')


@pytest.fixture
def cfg(issue):
    return SimpleNamespace(repos={'repo': issue['directory']}, hosts={},
                           repo_of_dir=lambda cwd: ('repo', '1'),
                           agent_for=lambda alias, override: override or 'claude')


@pytest.fixture
def app(mod, cfg, t_mod):
    return mod.IssueCommand(cfg, {'_t_source_root': lambda: str(ROOT), '_parse_rows': t_mod._parse_rows})


@pytest.fixture
def args():
    return SimpleNamespace(action='start', target=['12'], repo=None, scan_host=False, json=False,
                           allow_incomplete_check=False, codex=False, claude=False)


def clean_report(**changes):
    return dict(version=1, issue='https://github.com/org/repo/issues/12', matches=[],
                incomplete=[], checked=['local'], **changes)


@pytest.mark.parametrize('value', ['org/repo', 'https://github.com/Org/Repo.git',
                                 'git@github.com:Org/Repo.git', 'ssh://git@github.com/org/repo',
                                 'https://github.com/org/repo/'])
def test_slug(mod, value):
    assert mod.slug(value) == 'org/repo'
    assert mod.slug('https://evil.test/org/repo') is None


@pytest.mark.parametrize('words', [['12'], ['repo', '12'], ['org/repo', '12'],
                                   ['https://github.com/org/repo/issues/12']])
def test_target(mod, cfg, monkeypatch, words):
    monkeypatch.setattr(mod, 'command', lambda a: 'git@github.com:org/repo.git\n')
    assert mod.target(words, cfg)['repo'] == 'org/repo'


@pytest.mark.parametrize('words', [[], ['repo', '12', 'extra'], ['0'], ['-3'], ['repo', 'wat'],
                                   ['https://github.com/org/repo/pull/12'], ['missing/repo', '12']])
def test_bad_target(mod, cfg, monkeypatch, words):
    monkeypatch.setattr(mod, 'command', lambda a: 'git@github.com:org/repo.git')
    with pytest.raises(mod.IssueError):
        mod.target(words, cfg)


def test_target_missing_inference_origin_and_broken_checkout(mod, cfg, monkeypatch):
    cfg.repo_of_dir = lambda cwd: (None, None)
    with pytest.raises(mod.IssueError, match='infer'):
        mod.target(['12'], cfg)
    monkeypatch.setattr(mod, 'command', lambda a: '/some/local/path')
    with pytest.raises(mod.IssueError, match='origin'):
        mod.target(['repo', '12'], cfg)
    monkeypatch.setattr(mod, 'command', lambda a: (_ for _ in ()).throw(mod.IssueError('broken')))
    with pytest.raises(mod.IssueError, match='registered'):
        mod.target(['org/repo', '12'], cfg)


@pytest.mark.parametrize('text,scoped,match', [
    ('Fix https://github.com/ORG/REPO/issues/12.', False, True),
    ('Fix org/repo#12', False, True), ('Fix #12', True, True), ('Fix #12', False, False),
    ('Fix other/repo#12', True, False), ('Fix #123', True, False),
    ('https://github.com/other/repo/issues/12', True, False), ('Fix #12abc', True, False),
])
def test_references(mod, issue, text, scoped, match):
    assert mod.references(text, issue, scoped=scoped) is match


def test_evidence_and_worktrees(mod, issue):
    assert mod.evidence('Fix #12', issue, scoped=True) == 'explicit issue reference'
    assert 'title' in mod.evidence(issue['title'].upper(), issue, scoped=True)
    assert 'number' in mod.evidence('cursor/issue-12-fix', issue, scoped=True)
    assert mod.evidence('cursor/issue-120-fix', issue, scoped=True) is None
    assert mod.worktrees('worktree /a b\0HEAD abc\0branch refs/heads/main\0\0worktree /c\0bare\0') == [
        {'worktree': '/a b', 'HEAD': 'abc', 'branch': 'refs/heads/main'}, {'worktree': '/c', 'bare': ''}]
    assert mod.user_text({'content': 'a'}) == 'a'
    assert mod.user_text({'content': [{'text': 'a'}, 7, {'text': 'b'}]}) == 'a\nb'
    assert mod.user_text({'content': None}) == ''


def test_command_and_json_errors(mod, monkeypatch):
    monkeypatch.setenv('COV_CORE_SOURCE', 'private')
    def run(argv, **kw):
        assert 'COV_CORE_SOURCE' not in kw['env']
        assert kw['stdin'] == subprocess.DEVNULL
        assert kw['start_new_session'] is True
        return SimpleNamespace(returncode=0, stdout='{}')
    monkeypatch.setattr(mod.subprocess, 'run', run)
    assert mod.decoded(mod.command(['gh'])) == {}
    for value in ('oops', None):
        with pytest.raises(mod.IssueError):
            mod.decoded(value)
    monkeypatch.setattr(mod.subprocess, 'run', lambda *a, **kw: SimpleNamespace(returncode=1))
    with pytest.raises(mod.IssueError, match='exit 1'):
        mod.command(['gh'])
    for error in (OSError('secret'), subprocess.TimeoutExpired('gh', 1)):
        def fail(*a, **kw):
            raise error
        monkeypatch.setattr(mod.subprocess, 'run', fail)
        with pytest.raises(mod.IssueError, match='unavailable'):
            mod.command(['gh'])


def test_cursor_auth_http_and_pages(mod, monkeypatch):
    monkeypatch.delenv('CURSOR_API_KEY', raising=False)
    with pytest.raises(mod.IssueError, match='configured'):
        mod.cursor_get('agents')
    monkeypatch.setenv('CURSOR_API_KEY', 'fake-key')
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self): return b'{"items": []}'
    def get(req, **kw):
        assert req.full_url == 'https://api.cursor.com/v1/agents'
        assert req.get_header('Authorization') == 'Bearer fake-key'
        return Response()
    monkeypatch.setattr(mod, 'urlopen', get)
    assert mod.cursor_get('agents') == {'items': []}
    monkeypatch.setattr(mod, 'urlopen', lambda *a, **kw: (_ for _ in ()).throw(OSError('secret')))
    with pytest.raises(mod.IssueError, match='request failed'):
        mod.cursor_get('agents')
    assert list(mod.pages(lambda c: {'items': [2]} if c else {'items': [1], 'next': 'a'}, 'items', 'next')) == [1, 2]
    for fetch in (lambda c: [], lambda c: {'items': [], 'next': 'a'}, lambda c: {'items': [], 'next': 12}):
        with pytest.raises(mod.IssueError):
            list(mod.pages(fetch, 'items', 'next'))


def test_issue_fetch(mod, app, issue, monkeypatch):
    monkeypatch.setattr(mod, 'target', lambda *a: dict(issue))
    results = iter([json.dumps({'title': issue['title'], 'state': 'open', 'body': None}), '[[]]'])
    monkeypatch.setattr(mod, 'command', lambda *a: next(results))
    assert app.issue(['12'])['body'] == ''
    monkeypatch.setattr(mod, 'command', lambda *a: '{"pull_request": {}}')
    with pytest.raises(mod.IssueError, match='pull request'):
        app.issue(['12'])


def test_github_provider(mod, app, issue, monkeypatch):
    issue['comments'] = [[{'body': 'https://cursor.com/agents/bc-1 https://cursor.com/agents/bc-1'}]]
    def pull(n, body):
        return dict(number=n, title='work', body=body, html_url=f'https://github.com/org/repo/pull/{n}',
                    head={'ref': 'fix', 'sha': 'a'*40, 'repo': {'clone_url': 'https://github.com/org/repo.git'}})
    responses = iter([[ [{'source': {'issue': dict(pull_request={}, state='open', html_url='bad')}}],
                         [{'source': {'issue': dict(pull_request={'url': 'u'}, state='open',
                                                   html_url='https://github.com/org/repo/pull/2')}}]],
                      [[pull(1, 'Fix #12'), pull(2, ''), pull(3, 'other/repo#12')]],
                      [[{'name': 'issue-12-wip', 'commit': {'sha': 'b'*40}},
                        {'name': 'main', 'commit': {'sha': 'c'*40}}]]])
    monkeypatch.setattr(mod, 'command', lambda *a: json.dumps(next(responses)))
    rows = app.github(issue)
    assert [r['id'] for r in rows] == ['https://github.com/org/repo/pull/1',
                                      'https://github.com/org/repo/pull/2', 'branch:issue-12-wip', 'https://cursor.com/agents/bc-1']


def test_codex_cloud_scoping_and_pagination(mod, app, issue, monkeypatch):
    monkeypatch.setattr(mod.shutil, 'which', lambda name: '/bin/codex')
    calls = []
    def run(argv):
        calls.append(argv)
        if '--cursor' in argv:
            return json.dumps({'tasks': [{'id': '2', 'url': 'u2', 'title': issue['title'], 'status': 'ready'},
                                        {'id': '3', 'url': 'u3', 'title': 'Fix #12', 'status': 'running'},
                                        {'id': '4', 'url': 'u4', 'title': 'unrelated'}]})
        return json.dumps({'tasks': [{'id': '1', 'url': 'u1', 'title': 'Fix org/repo#12', 'status': 'running'}], 'cursor': 'more'})
    monkeypatch.setattr(mod, 'command', run)
    rows = app.codex(issue)
    assert len(rows) == 3 and rows[0]['state'] == 'running'
    assert 'unverified' in rows[1]['evidence'] and 'unverified' in rows[2]['evidence']
    assert calls[-1][-2:] == ['--cursor', 'more']


def test_cursor_provider(mod, app, issue, monkeypatch):
    def get(path):
        if path.startswith('agents?'):
            return {'items': [{'id': n} for n in ['a', 'b', 'c']]}
        if '/runs/' in path:
            return {'status': 'RUNNING', 'prompt': {'text': 'Fix #12'}}
        return {'url': 'https://cursor.com/agents/' + path[-1], 'name': 'other',
                'repos': [{'url': 'org/repo' if path[-1] != 'b' else 'other/repo'}],
                **({'latestRunId': 'r'} if path[-1] == 'a' else {})}
    monkeypatch.setattr(mod, 'cursor_get', get)
    rows = app.cursor(issue)
    assert len(rows) == 1 and rows[0]['state'] == 'running'
    def active_get(path):
        data = get(path)
        if path == 'agents/a': data['status'] = 'ACTIVE'
        elif '/runs/' in path: data['status'] = 'FINISHED'
        return data
    monkeypatch.setattr(mod, 'cursor_get', active_get)
    assert app.cursor(issue)[0]['state'] == 'active'


def test_scan_failures_remote_dedupe(mod, app, issue, monkeypatch):
    app.cfg.hosts = {'cloud': 'host'}
    local = mod.candidate('local', '/w', 'recorded', host='local', worktree='/w', branch='fix')
    pr = mod.candidate('github', 'pr', 'ref', url='pr', pr='pr', branch='fix')
    task = mod.candidate('codex-cloud', 'id', 'ref', url='task')
    monkeypatch.setattr(app, 'local', lambda i: [local])
    monkeypatch.setattr(app, 'github', lambda i: [pr, mod.candidate('github', 'task', 'link', url='task')])
    monkeypatch.setattr(app, 'codex', lambda i: [task])
    monkeypatch.setattr(app, 'cursor', lambda i: (_ for _ in ()).throw(mod.IssueError('no credentials')))
    monkeypatch.setattr(app, 'remote', lambda *a: [])
    report = app.scan(issue)
    assert len(report['matches']) == 2 and local['pr'] == 'pr'
    assert report['incomplete'] == [{'source': 'cursor-cloud', 'reason': 'no credentials'}]
    assert app.scan(issue, True)['checked'] == ['local']


def test_remote_contract(mod, app, issue, monkeypatch):
    def run(argv, **kw):
        assert argv[:2] == ['ssh', '-o'] and 'zsh -lic' in argv[-1]
        assert '--scan-host' in argv[-1] and 'org/repo' in argv[-1]
        return json.dumps({'version': 1, 'matches': [{'id': '/w'}], 'incomplete': []})
    monkeypatch.setattr(mod, 'command', run)
    assert app.remote(issue, 'cloud', 'host') == [{'id': 'cloud:/w', 'host': 'cloud', 'provider': 'remote'}]
    monkeypatch.setattr(mod, 'command', lambda *a, **kw: '{"version": 0}')
    with pytest.raises(mod.IssueError, match='update t'):
        app.remote(issue, 'cloud', 'host')


@pytest.fixture
def local_scan(mod, app, issue, tmp_path, monkeypatch):
    wt = tmp_path / 'tree'; wt.mkdir()
    gd = tmp_path / 'metadata'; gd.mkdir()
    tx = tmp_path / 'transcript.jsonl'
    tx.write_text('not json\n[]\n' + json.dumps({'type': 'user', 'sessionId': 'sid',
        'message': {'content': f't issue association: org/repo#12; token=token\nFix #12'}}) + '\n')
    marker = gd / 't-issue.json'
    marker.write_text(json.dumps({'repo': 'org/repo', 'number': 12, 'branch': 'dev/repo-1', 'slot': '1', 'token': 'token'}))
    state = dict(live=True, stamp=True, transcript=True, branch='dev/repo-1', marker=True, second=False)
    def run(argv, **kw):
        if argv[0] == 'zsh':
            if '_dev_session_rows' in argv[-1]:
                row = f'sid\t{wt}\trepo-1\tattached\t-\tFix #12\tclaude\n'
                return row * (2 if state['second'] else 1) if state['live'] else ''
            return str(tx) + '\n/missing' if state['transcript'] else ''
        if argv[0] == 'tmux':
            if not state['stamp']: raise mod.IssueError('absent')
            return 'T_ISSUE_TOKEN=token'
        if 'worktree' in argv:
            return f'worktree {wt}\0branch refs/heads/{state["branch"]}\0\0worktree /bare\0bare\0\0'
        if 'rev-parse' in argv: return str(gd)
        if 'log' in argv: return ''
        pytest.fail(str(argv))
    monkeypatch.setattr(mod, 'command', run)
    return state, marker, tx


def test_local_managed_unmanaged_saved_reused(mod, app, issue, local_scan):
    state, marker, tx = local_scan
    row = app.local(issue)[0]
    assert row['verified'] and row['session'] == 'dev-repo-1'
    state.update(live=False)
    assert app.local(issue)[0]['resume']
    state.update(live=True, second=True)
    assert not app.local(issue)[0]['verified']
    state.update(second=False, stamp=False, transcript=False)
    assert not app.local(issue)[0]['verified']
    marker.unlink()
    state.update(transcript=True)
    # Transferred transcript keeps the association without a t-created worktree marker.
    assert app.local(issue)[0]['verified']
    tx.write_text(json.dumps({'type': 'session_meta', 'payload': {'id': 'codex'}}) + '\n' +
                  json.dumps({'type': 'response_item', 'payload': {'role': 'user', 'content': [{'text': 'Fix #12'}]}}))
    assert not app.local(issue)[0]['verified']
    state.update(live=False, transcript=False, branch='main')
    assert app.local(issue) == []


@pytest.mark.parametrize('text,assigned', [
    ('Fix #12', True), ('Please implement org/repo#12', True),
    ('Address GitHub issue https://github.com/org/repo/issues/12', True),
    ('Fix other/repo#12', False),
    ('user@host % t issue start repo 12\norg/repo#12: Build desktop\nType stopped:', False),
    ('Why does this fail?\nFix #12\n', False),
    ('Here is an example mentioning org/repo#12.', False),
])
def test_task_assignment_excludes_pasted_output(mod, issue, text, assigned):
    assert mod.task_reference(text, issue) == assigned


def test_incidental_debugging_session_does_not_block_legacy_saved_work(mod, app, issue, args,
                                                                    local_scan, monkeypatch, capsys):
    state, marker, tx = local_scan
    marker.unlink()
    tx.write_text(json.dumps({'type': 'user', 'sessionId': 'sid', 'message': {
        'content': 'user@host % t issue start repo 12\norg/repo#12: Build desktop\nType stopped:'}}))
    mention = app.local(issue)[0]
    assert mention['incidental'] and not mention['verified'] and not mention.get('session')
    assert mention['owners'][0]['sid'] == 'sid'
    state['live'] = False
    tx.write_text(json.dumps({'type': 'user', 'sessionId': 'legacy', 'message': {'content': 'Build a desktop app'}}))
    original = mod.command
    monkeypatch.setattr(mod, 'command', lambda argv, **kw: 'Build desktop (#12)' if 'log' in argv else original(argv, **kw))
    saved = app.local(issue)[0]
    assert saved['resume'] and saved['verified'] and not saved['incidental']
    assert saved['association'] == 'workspace'
    monkeypatch.setattr(app, 'local', lambda i: [mention, saved])
    for provider in ('github', 'codex', 'cursor'):
        monkeypatch.setattr(app, provider, lambda i: [])
    report = app.scan(issue)
    assert report['matches'] == [saved] and report['mentions'] == [mention]
    monkeypatch.setattr(app, 'issue', lambda w: issue)
    monkeypatch.setattr(app, 'attach', lambda row: 7 if row == saved else pytest.fail('wrong session'))
    monkeypatch.setattr(app, 'takeover', lambda *a: pytest.fail('local work must not take over'))
    monkeypatch.setattr(app, 'launch', lambda *a: pytest.fail('must preserve saved workspace'))
    assert app.run_one(args, args.target) == 7
    output = capsys.readouterr().out
    assert 'Mention only (not an owner)' in output and 'Continue: t session resume repo 1 --days all' in output
    assert 'Working on: Fix #12' in output and 'Conversation: sid' in output
    args.action, args.json = 'status', True
    assert app.run_one(args, args.target) == 0
    machine = json.loads(capsys.readouterr().out)
    assert machine['matches'] == [saved] and machine['mentions'] == [mention]


def test_legacy_active_task_attaches_without_handoff(mod, app, issue, args, local_scan, monkeypatch):
    state, marker, tx = local_scan
    marker.unlink()
    tx.write_text(json.dumps({'type': 'user', 'sessionId': 'sid', 'message': {'content': 'Fix #12'}}))
    row = app.local(issue)[0]
    assert not row['verified'] and row['continuable'] and row['session'] == 'dev-repo-1'
    report = clean_report(); report['matches'] = [row]
    setup_run(app, issue, monkeypatch, iter([report]))
    monkeypatch.setattr(app, 'attach', lambda selected: 8 if selected == row else 0)
    monkeypatch.setattr(app, 'takeover', lambda *a: pytest.fail('must attach local session'))
    assert app.run_one(args, args.target) == 8
    tx.write_text(json.dumps({'type': 'user', 'sessionId': 'old-session', 'message': {'content': 'Fix #12'}}))
    row = app.local(issue)[0]
    assert row['incidental'] and not row.get('continuable')


@pytest.mark.parametrize('live', [True, False])
def test_slot_number_match_requires_review_before_continuation(mod, app, issue, args,
                                                              local_scan, monkeypatch, live):
    state, marker, tx = local_scan
    marker.unlink()
    state.update(branch='dev/repo-12', live=live)
    tx.write_text(json.dumps({'type': 'user', 'sessionId': 'sid', 'message': {'content': 'Unrelated work'}}))
    row = app.local(issue)[0]
    assert row['review_required'] and not row['verified'] and row['continuable']
    report = clean_report(); report['matches'] = [row]
    monkeypatch.setattr(mod.sys.stdin, 'isatty', lambda: False)
    with pytest.raises(mod.IssueError, match='review'):
        app.choose(report)
    monkeypatch.setattr(mod.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(app, 'pick_rows', lambda default, all_rows, *a, **kw: [all_rows[-1]])
    assert app.choose(report) is None  # Enter on the explicit unrelated action is enough.
    monkeypatch.setattr(app, 'pick_rows', lambda default, all_rows, *a, **kw: [all_rows[0]])
    setup_run(app, issue, monkeypatch, iter([report]))
    monkeypatch.setattr(app, 'attach', lambda selected: 7)
    monkeypatch.setattr(app, 'takeover', lambda *a: pytest.fail('local continuation must not take over'))
    assert app.run_one(args, args.target) == 7


def test_multiple_local_owners_never_fall_through_to_external_takeover(mod, app, issue, args,
                                                                     local_scan, monkeypatch):
    state, marker, tx = local_scan
    state['second'] = True
    row = app.local(issue)[0]
    assert len(row['owners']) == 2 and not row.get('continuable')
    report = clean_report(); report['matches'] = [row]
    setup_run(app, issue, monkeypatch, iter([report]))
    monkeypatch.setattr(app, 'choose', lambda r: row)
    monkeypatch.setattr(app, 'takeover', lambda *a: pytest.fail('not an external task'))
    with pytest.raises(mod.IssueError, match='no single resumable session'):
        app.run_one(args, args.target)


@pytest.mark.parametrize('interface,slot,state', [('cli', 'repo-1', 'attached'),
                                              ('desktop', 'repo-1', 'app'),
                                              ('foreground', 'repo:abc', 'running')])
def test_owner_details_and_picker_preserve_identity(mod, app, issue, monkeypatch, capsys, interface, slot, state):
    row = mod.candidate('local', '/w', 'recorded issue worktree', worktree='/w', branch='dev/repo-1',
                        host='local', pr='https://github.com/org/repo/pull/3')
    row['state'] = 'active'
    row['owners'] = [dict(host='local', slot=slot, sid='exact-id', state=state, context='active',
                         interface=interface, agent='codex', model='example-model',
                         summary='Build desktop\x1b[31m\nworkspace', last_turn_started_at='2026-10-23T12:00:00Z')]
    report = clean_report(); report['matches'] = [row, dict(row, id='/other')]
    app.display(issue, report)
    output = capsys.readouterr().out
    for text in ('example-model', 'Build desktop', 'Conversation: exact-id', 'Branch: dev/repo-1',
                 '2026-10-23T12:00:00Z', interface):
        assert text in output
    assert '\x1b' not in output
    assert ('Open session:' in output) == (interface != 'foreground')
    monkeypatch.setattr(mod.sys.stdin, 'isatty', lambda: False)
    with pytest.raises(mod.IssueError, match='review'):
        app.choose(report)
    monkeypatch.setattr(mod.sys.stdin, 'isatty', lambda: True)
    def pick(defaults, all_rows, *a, **kw):
        assert 'example-model' in all_rows[0] and 'Build desktop' in all_rows[0]
        assert 'Conversation: exact-id' in kw['details'][0]
        return [all_rows[0]]
    monkeypatch.setattr(app, 'pick_rows', pick)
    assert app.choose(report) == row


def test_remote_mentions_and_detail_commands(mod, app, issue, monkeypatch):
    app.cfg.hosts = {'remote': 'remote-host'}
    owner = dict(host='local', slot='repo-1', sid='sid', state='attached', context='active', agent='codex')
    row = mod.candidate('local', '/w', 'mention', worktree='/w', owners=[owner])
    monkeypatch.setattr(mod, 'command', lambda *a, **kw: json.dumps(dict(version=1, matches=[], mentions=[row], incomplete=[])))
    remote = app.remote(issue, 'remote', 'remote-host')[0]
    assert remote['incidental'] and remote['owners'][0]['host'] == 'remote'
    assert 'ssh -t remote-host' in '\n'.join(app.row_details(remote))
    remote.update(resume=True, alias='repo', slot='1')
    assert 'session resume repo 1' in '\n'.join(app.row_details(remote))


def test_local_prepared_reservation_is_verified_without_live_agent(mod, app, issue, local_scan):
    state, marker, tx = local_scan
    state.update(live=False, transcript=False)
    data = json.loads(marker.read_text())
    data['state'] = 'prepared'
    marker.write_text(json.dumps(data))
    (marker.parent / 't-issue-prompt').write_text('Address issue #12')
    row = app.local(issue)[0]
    assert row['state'] == 'prepared' and row['verified'] and row['prepared']
    assert not row.get('resume') and not row.get('session')
    report = clean_report(); report['matches'] = [row]
    assert app.choose(report) == row
    (marker.parent / 't-issue-prompt').unlink()
    incomplete = app.local(issue)[0]
    assert not incomplete.get('prepared') and not incomplete['verified']


def test_recovered_branch_is_evidence_for_verified_local_owner(mod, app, issue, local_scan, monkeypatch):
    state, marker, tx = local_scan
    data = json.loads(marker.read_text())
    data['handoff'] = {'pr': None, 'source': 'branch:issue-12'}
    marker.write_text(json.dumps(data))
    branch = mod.candidate('github', 'branch:issue-12', 'branch number', branch='issue-12', head='a'*40)
    monkeypatch.setattr(app, 'github', lambda i: [branch])
    monkeypatch.setattr(app, 'codex', lambda i: [])
    monkeypatch.setattr(app, 'cursor', lambda i: [])
    report = app.scan(issue)
    assert len(report['matches']) == 1
    assert app.choose(report)['session'] == 'dev-repo-1'
    assert report['matches'][0]['published_source'] == branch['id']
    state.update(second=True)
    report = app.scan(issue)
    assert len(report['matches']) == 2
    assert 'published_source' not in report['matches'][0]


def test_prepared_retry_requires_same_reservation_after_lock(mod, app, issue, args,
                                                             monkeypatch, tmp_path):
    prepared = dict(provider='local', id='/worktree', host='local', worktree='/worktree',
                    evidence='recorded', verified=True, prepared=True, state='prepared',
                    alias='repo', slot='1')
    report = clean_report(); report['matches'] = [prepared]
    setup_run(app, issue, monkeypatch, iter([report, report]))
    monkeypatch.setattr(mod, 'command', lambda *a: str(tmp_path))
    retried = []
    monkeypatch.setattr(app, 'retry', lambda row: retried.append(row) or
                        dict(host='local', session='dev-repo-1'))
    monkeypatch.setattr(app, 'launch', lambda *a: pytest.fail('must reuse reservation'))
    monkeypatch.setattr(app, 'takeover', lambda *a: pytest.fail('must not take over reservation'))
    monkeypatch.setattr(app, 'attach', lambda row: 0)
    assert app.run_one(args, args.target) == 0
    assert retried == [prepared]


@pytest.mark.parametrize('fresh_matches', [[],
    [dict(provider='local', id='/worktree', host='local', worktree='/worktree',
          evidence='recorded', verified=False, prepared=False, state='saved')],
    [dict(provider='local', id='/worktree', host='local', worktree='/worktree',
          evidence='recorded', verified=True, prepared=True, state='prepared'),
     dict(provider='local', id='/other', host='local', worktree='/other',
          evidence='recorded', verified=True, prepared=True, state='prepared')]])
def test_prepared_retry_blocks_changed_or_additional_reservation(mod, app, issue, args,
                                                                 monkeypatch, tmp_path, fresh_matches):
    selected = dict(provider='local', id='/worktree', host='local', worktree='/worktree',
                    evidence='recorded', verified=True, prepared=True, state='prepared',
                    alias='repo', slot='1')
    original = clean_report(); original['matches'] = [selected]
    latest = clean_report(); latest['matches'] = fresh_matches
    setup_run(app, issue, monkeypatch, iter([original, latest]))
    monkeypatch.setattr(mod, 'command', lambda *a: str(tmp_path))
    monkeypatch.setattr(app, 'retry', lambda *a: pytest.fail('changed reservation must not start'))
    monkeypatch.setattr(app, 'launch', lambda *a: pytest.fail('must not allocate new worktree'))
    with pytest.raises(mod.IssueError):
        app.run_one(args, args.target)


def test_remote_prepared_reservation_requires_on_host_recovery(mod, app, issue, args, monkeypatch):
    prepared = dict(provider='remote', id='cloud:/worktree', host='cloud', worktree='/worktree',
                    evidence='recorded', verified=True, prepared=True, state='prepared',
                    alias='repo', slot='1')
    report = clean_report(); report['matches'] = [prepared]
    setup_run(app, issue, monkeypatch, iter([report]))
    monkeypatch.setattr(app, 'takeover', lambda *a: pytest.fail('must not take over remote reservation'))
    monkeypatch.setattr(app, 'launch', lambda *a: pytest.fail('must not launch another agent'))
    with pytest.raises(mod.IssueError, match='host'):
        app.run_one(args, args.target)


def test_choose(mod, app, monkeypatch):
    report = clean_report()
    assert app.choose(report) is None
    row = dict(verified=True, session='dev-repo-1', provider='local', state='active',
               evidence='recorded', worktree='/work')
    report['matches'] = [row]
    assert app.choose(report) == row
    row['verified'] = False
    monkeypatch.setattr(mod.sys.stdin, 'isatty', lambda: False)
    with pytest.raises(mod.IssueError, match='review'):
        app.choose(report)
    monkeypatch.setattr(mod.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(app, 'pick_rows', lambda defaults, all_rows, *a, **kw: [all_rows[0]])
    assert app.choose(report) == row
    monkeypatch.setattr(app, 'pick_rows', lambda defaults, all_rows, *a, **kw: [all_rows[-1]])
    monkeypatch.setattr('builtins.input', lambda text: 'unrelated')
    assert app.choose(report) is None
    report['matches'].append(dict(verified=True, provider='cloud', state='running',
                                  evidence='issue reference', url='https://example.test/task'))
    observed = {}
    def pick(defaults, all_rows, *a, **kw):
        observed['rows'] = all_rows
        return [all_rows[-1]]
    monkeypatch.setattr(app, 'pick_rows', pick)
    assert app.choose(report) == report['matches'][-1]
    assert not any('dismiss' in value for value in observed['rows'])


def test_attach(mod, app, monkeypatch):
    calls = []
    monkeypatch.setattr(mod.subprocess, 'call', lambda a: calls.append(a) or 0)
    monkeypatch.delenv('TMUX', raising=False)
    row = dict(host='local', session='dev-repo-1')
    assert app.attach(row) == 0 and calls[-1][1] == 'attach-session'
    monkeypatch.setenv('TMUX', 'fake')
    app.attach(row)
    assert calls[-1][1] == 'switch-client'
    app.cfg.hosts = {'cloud': 'host'}
    app.attach(dict(row, host='cloud'))
    assert calls[-1][:3] == ['ssh', '-t', 'host']
    row.update(resume=True, alias='repo', slot='1')
    app.attach(row)
    assert '_t_resume repo 1 --days all' in calls[-1][-1]
    app.attach(dict(row, host='cloud'))
    assert 't session resume repo 1 --days all' in calls[-1][-1]


def test_takeover(mod, app, issue, monkeypatch):
    row = dict(provider='codex-cloud', id='task', pr='https://github.com/org/repo/pull/5')
    monkeypatch.setattr(mod.sys.stdin, 'isatty', lambda: False)
    with pytest.raises(mod.IssueError, match='interactive'): app.takeover(issue, row)
    monkeypatch.setattr(mod.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr('builtins.input', lambda t: '')
    with pytest.raises(mod.IssueError, match='cancelled'): app.takeover(issue, row)
    monkeypatch.setattr('builtins.input', lambda t: 'stopped')
    monkeypatch.setattr(app, 'codex', lambda i: [dict(id='task', state='running')])
    with pytest.raises(mod.IssueError, match='verified stopped'): app.takeover(issue, row)
    monkeypatch.setattr(app, 'codex', lambda i: [dict(id='task', state='ready')])
    monkeypatch.setattr(mod, 'command', lambda a: json.dumps(dict(state='open', head=dict(ref='fix', sha='a'*40,
                                                   repo=dict(clone_url='https://github.com/org/repo.git')))))
    assert app.takeover(issue, row)['branch'] == 'fix'
    row['pr'] = 'https://github.com/other/repo/pull/5'
    with pytest.raises(mod.IssueError, match='issue repository'): app.takeover(issue, row)
    row.pop('pr')
    with pytest.raises(mod.IssueError, match='issue repository'): app.takeover(issue, row)
    row['pr'] = 'https://github.com/org/repo/pull/5'
    monkeypatch.setattr(mod, 'command', lambda a: '{"state": "closed"}')
    with pytest.raises(mod.IssueError, match='open'): app.takeover(issue, row)


def test_launch_and_prompt(mod, app, issue, tmp_path, monkeypatch):
    gd = tmp_path / 'metadata'; gd.mkdir()
    commands = []
    def run(argv, **kw):
        commands.append(argv)
        if argv[0] == 'zsh':
            return 'noise\n1\t' + str(tmp_path / 'tree') if '_t_issue_prepare' in argv[-1] else ''
        if 'rev-parse' in argv: return str(gd)
        if 'symbolic-ref' in argv: return 'dev/repo-1'
        return ''
    monkeypatch.setattr(mod, 'command', run)
    handoff = dict(pr='https://github.com/org/repo/pull/3', branch='fix', head='a'*40,
                   clone_url='https://github.com/org/repo.git')
    row = app.launch(issue, clean_report(), 'codex', handoff)
    assert row['session'] == 'dev-repo-1'
    marker = json.loads((gd / 't-issue.json').read_text())
    assert marker['state'] == 'started'
    brief = (gd / 't-issue-prompt').read_text()
    assert brief.startswith('t issue association: org/repo#12; token=')
    assert 'Reuse PR' in brief and 'Fix the login' in brief
    assert (gd / 't-issue-prompt').stat().st_mode & 0o777 == 0o600
    app.launch(issue, clean_report(), 'claude', None)
    with pytest.raises(mod.IssueError, match='identity'):
        app.launch(issue, clean_report(), 'claude', dict(handoff, clone_url='file:///bad'))
    assert not any('reset' in call for call in commands)
    assert any('_t_issue_prepare repo codex ' + handoff['head'] in call[-1] for call in commands)


def setup_run(app, issue, monkeypatch, reports):
    monkeypatch.setattr(app, 'issue', lambda words: dict(issue))
    monkeypatch.setattr(app, 'scan', lambda *a: next(reports))


@pytest.mark.parametrize('mode', ['status', 'json', 'scan_host', 'closed', 'closed_json'])
def test_readonly_modes(mod, app, issue, args, monkeypatch, capsys, mode):
    if mode.startswith('closed'): issue['state'] = 'closed'
    elif mode == 'status': args.action = 'status'
    elif mode == 'json': args.action = 'status'; args.json = True
    else: setattr(args, mode, True); args.action = 'status'
    if mode == 'closed_json': args.json = True
    monkeypatch.setattr(mod, 'target', lambda *a: dict(issue))
    setup_run(app, issue, monkeypatch, iter([clean_report()]))
    assert app.run(args) == 0
    assert capsys.readouterr().out


def test_run_guardrails(mod, app, issue, args, monkeypatch, capsys, tmp_path):
    report = clean_report()
    report['incomplete'] = [{'source': 'cursor', 'reason': 'missing'}]
    setup_run(app, issue, monkeypatch, iter([report, report, report]))
    args.action = 'status'
    assert app.run_one(args, args.target) == 2
    args.action = 'start'
    with pytest.raises(mod.IssueError, match='incomplete'): app.run_one(args, args.target)
    args.allow_incomplete_check = True
    monkeypatch.setattr(mod, 'command', lambda *a: str(tmp_path))
    monkeypatch.setattr(app, 'scan', lambda *a: report)
    monkeypatch.setattr(app, 'launch', lambda *a: {'session': 'new'})
    monkeypatch.setattr(app, 'attach', lambda r: 7)
    assert app.run_one(args, args.target) == 7
    assert 'Incomplete:' in capsys.readouterr().out


def test_run_existing_rechecks_and_errors(mod, app, issue, args, monkeypatch, tmp_path):
    owner = dict(provider='local', id='w', evidence='recorded', verified=True, session='dev-repo-1',
                 state='active', worktree='/w')
    report = clean_report(); report['matches'] = [owner]
    setup_run(app, issue, monkeypatch, iter([report]))
    monkeypatch.setattr(app, 'attach', lambda r: 8)
    assert app.run_one(args, args.target) == 8
    monkeypatch.setattr(mod, 'command', lambda *a: str(tmp_path))
    setup_run(app, issue, monkeypatch, iter([clean_report(), report]))
    with pytest.raises(mod.IssueError, match='appeared'): app.run_one(args, args.target)
    partial = clean_report(); partial['incomplete'] = [{'source': 'github', 'reason': 'failure'}]
    setup_run(app, issue, monkeypatch, iter([clean_report(), partial]))
    with pytest.raises(mod.IssueError, match='recheck'): app.run_one(args, args.target)
    monkeypatch.setattr(app, 'choose', lambda r: None)
    setup_run(app, issue, monkeypatch, iter([report, report]))
    with pytest.raises(mod.IssueError, match='became active'): app.run_one(args, args.target)
    monkeypatch.setattr(mod.IssueCommand, 'run', lambda *a: (_ for _ in ()).throw(EOFError('cancelled')))
    assert mod.run(app.cfg, args, app.api) == 130


def test_parser_and_handler(t_mod, monkeypatch):
    args = t_mod.build_parser().parse_args(['issue', 'status', 'org/repo', '12', '--json'])
    assert args.action == 'status' and args.target == ['org/repo', '12'] and args.json
    with pytest.raises(SystemExit):
        t_mod.build_parser().parse_args(['issue', 'start', '12', '--codex', '--claude'])
    with pytest.raises(SystemExit):
        t_mod.build_parser().parse_args(['issue', '12'])
    monkeypatch.setattr(t_mod.runpy, 'run_path', lambda path: {'run': lambda cfg, args, api: 9})
    assert t_mod.cmd_issue(None, args) == 9


def test_retry_and_large_prompt(mod, app, issue, tmp_path, monkeypatch):
    gd = tmp_path / 'gitdir'; gd.mkdir()
    marker = {'state': 'prepared', 'agent': 'claude', 'token': 'token'}
    (gd / 't-issue.json').write_text(json.dumps(marker))
    calls = []
    monkeypatch.setattr(mod, 'command', lambda a, **kw: calls.append(a) or str(gd))
    row = dict(worktree='/w', alias='repo', slot='1')
    assert app.retry(row)['session'] == 'dev-repo-1'
    assert '_t_issue_launch' in calls[-1][-1]
    with pytest.raises(mod.IssueError, match='changed'): app.retry(row)
    issue['body'] = '界' * 80000
    prompt = app.prompt(issue, clean_report(), None, 'token')
    assert len(prompt.encode()) < 61000 and 'Context truncated' in prompt
    assert issue['url'] in prompt
    assert mod.worktrees('worktree /a\0\0') == [{'worktree': '/a'}]


def test_takeover_blocks_active_recheck(mod, app, issue, args, monkeypatch, tmp_path):
    row = dict(provider='codex-cloud', id='task', evidence='reference', state='running', url='u')
    report = clean_report(); report['matches'] = [row]
    setup_run(app, issue, monkeypatch, iter([report, report]))
    monkeypatch.setattr(app, 'choose', lambda r: row)
    monkeypatch.setattr(app, 'takeover', lambda *a: {'pr': 'u'})
    monkeypatch.setattr(mod, 'command', lambda *a: str(tmp_path))
    with pytest.raises(mod.IssueError, match='still active'): app.run_one(args, args.target)


@pytest.mark.parametrize('agent', ['codex', 'claude'])
def test_real_isolated_launch_quotes_brief_and_reserves_tree(tmp_path, agent):
    import shutil
    import time
    tmux = shutil.which('tmux')
    zsh = shutil.which('zsh')
    if not tmux or not zsh:
        pytest.skip('tmux and zsh required')
    home = tmp_path / 'home'; home.mkdir()
    bindir = home / 'bin'; bindir.mkdir()
    import uuid
    socket = '/tmp/t-issue-' + uuid.uuid4().hex
    env = {'HOME': str(home), 'ZDOTDIR': str(home), 'PATH': str(bindir) + ':' + os.environ['PATH'],
           'XDG_CONFIG_HOME': str(home / 'config'), 'XDG_CACHE_HOME': str(home / 'cache'),
           'XDG_STATE_HOME': str(home / 'state'), 'T_LOCAL_RC': str(home / 'local.zsh'),
           'T_RECOVERY_DISABLE': '1', 'T_AUTO_TRUST': '0', 'SHELL': zsh, 'TERM': 'xterm-256color',
           'SHELL_SESSIONS_DISABLE': '1'}
    (bindir / 'tmux').write_text('#!/bin/sh\nexec ' + shlex.join([tmux, '-S', socket, '-f', '/dev/null']) + ' "$@"\n')
    (bindir / 'gh').write_text('#!/bin/sh\nexit 1\n')
    record = home / 'agent.json'
    (bindir / agent).write_text('#!' + sys.executable + '\nimport json, pathlib, sys, time\n' +
        f'pathlib.Path({str(record)!r}).write_text(json.dumps(sys.argv[1:]))\ntime.sleep(30)\n')
    for file in bindir.iterdir(): file.chmod(0o755)
    # Ubuntu's system zshrc can prompt about shared completion permissions
    # before our sandbox zshrc loads. Keep that unrelated prompt out of tmux.
    (home / '.zshenv').write_text('skip_global_compinit=1\n')
    (home / '.zshrc').write_text('export PATH=' + shlex.quote(str(bindir)) + ':$PATH\n')
    repo = home / 'repo'
    def git(*a):
        return subprocess.run(['git', *a], env=env, check=True, capture_output=True, text=True).stdout
    git('init', '-b', 'main', str(repo))
    git('-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-m', 'initial')
    git('-C', str(repo), 'remote', 'add', 'origin', str(repo))
    git('-C', str(repo), 'fetch', 'origin')
    (home / 'local.zsh').write_text('DEV_REPOS[repo]=' + shlex.quote(str(repo)) + '\nDEV_WORKTREE_ROOT=' + shlex.quote(str(home / 'trees')) + '\n')
    prefix = 'source ' + shlex.quote(str(ROOT / 't.plugin.zsh')) + '; '
    def shell(code):
        return subprocess.run([zsh, '-lic', prefix + code], env=env, capture_output=True, text=True, timeout=20)
    try:
        prepared = shell('_t_issue_prepare repo ' + agent)
        assert prepared.returncode == 0, prepared.stderr
        slot, cwd = prepared.stdout.strip().splitlines()[-1].split('\t')
        assert slot == '1' and cwd != str(repo)
        gd = Path(git('-C', cwd, 'rev-parse', '--absolute-git-dir').strip())
        assert (gd / 't-issue-preparing').exists()
        blocked = shell('_dev_worktree_create repo 1')
        assert blocked.returncode != 0 and 'reserved' in blocked.stderr
        blocked = shell('_dev_resume_session dev-repo-1 ' + shlex.quote(cwd) + ' sid claude')
        assert blocked.returncode != 0 and 'reserved' in blocked.stderr
        blocked = shell('_dev_new_session dev-repo-1 ' + shlex.quote(cwd) + ' "" 1 claude')
        assert blocked.returncode != 0 and 'reserved' in blocked.stderr
        brief = gd / 'task brief'
        content = '--help\nA "quote", $(touch ' + str(home / 'INJECTED') + ') and `false`\nsecond line'
        brief.write_text(content)
        launch_command = shell('_dev_agent_new_cmd ' + shlex.join([agent, 'sid', str(brief)]))
        assert launch_command.returncode == 0 and content not in launch_command.stdout
        assert 'cat --' in launch_command.stdout
        launched = shell('_t_issue_launch ' + shlex.join(['repo', slot, cwd, agent, str(brief), 'token']))
        assert launched.returncode == 0, launched.stderr
        deadline = time.monotonic() + 5
        while not record.exists() and time.monotonic() < deadline: time.sleep(.05)
        assert record.exists(), shell('tmux capture-pane -p -t dev-repo-1').stdout
        assert json.loads(record.read_text())[-2:] == ['--', content]
        assert not (home / 'INJECTED').exists()
        assert not (gd / 't-issue-preparing').exists()
        assert shell('tmux show-environment -t dev-repo-1 T_ISSUE_TOKEN').stdout.strip() == 'T_ISSUE_TOKEN=token'
        assert shell('_t_issue_launch ' + shlex.join(['repo', slot, cwd, agent, str(brief), 'token'])).returncode != 0
        assert git('-C', str(repo), 'branch', '--show-current').strip() == 'main'
        # A branch that was invisible before fetch must never be reused or reset.
        git('-C', str(repo), 'branch', 'dev/repo-2')
        handoff = git('-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
                      'commit-tree', 'HEAD^{tree}', '-p', 'HEAD', '-m', 'published work').strip()
        next_tree = shell('_t_issue_prepare ' + shlex.join(['repo', agent, handoff]))
        assert next_tree.returncode == 0, next_tree.stderr
        next_slot, next_cwd = next_tree.stdout.strip().splitlines()[-1].split('\t')
        assert next_slot == '3'
        assert git('-C', next_cwd, 'rev-parse', 'HEAD').strip() == handoff
        assert git('-C', str(repo), 'rev-parse', 'dev/repo-2').strip() != handoff
    finally:
        subprocess.run([tmux, '-S', socket, 'kill-server'], env=env, capture_output=True)


def test_concurrent_invocations_launch_once(mod, cfg, issue, args, t_mod, monkeypatch, tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    gate = threading.Barrier(2)
    launched = []
    def scan_factory():
        first = True
        def scan(*a):
            nonlocal first
            report = clean_report()
            if first:
                first = False
                gate.wait(timeout=5)
            elif launched:
                report['matches'] = [dict(provider='local', id='new', verified=True,
                                          session='dev-repo-1', state='active')]
            return report
        return scan
    monkeypatch.setattr(mod, 'command', lambda *a: str(tmp_path))
    apps = [mod.IssueCommand(cfg, {'_t_source_root': lambda: str(ROOT)}) for _ in range(2)]
    for app in apps:
        monkeypatch.setattr(app, 'issue', lambda words: dict(issue))
        monkeypatch.setattr(app, 'scan', scan_factory())
        monkeypatch.setattr(app, 'display', lambda *a: None)
        monkeypatch.setattr(app, 'launch', lambda *a: launched.append('new') or {'session': 'new'})
        monkeypatch.setattr(app, 'attach', lambda r: 0)
    def run(app):
        try:
            return app.run_one(args, args.target)
        except mod.IssueError as error:
            assert 'appeared' in str(error)
            return 1
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(run, apps)) == [0, 1]
    assert launched == ['new']


def test_saved_resume_requires_complete_scan(mod, app, issue, args, monkeypatch):
    report = clean_report()
    report['matches'] = [dict(provider='local', id='w', evidence='saved', state='saved',
                              verified=True, resume=True, worktree='/w')]
    report['incomplete'] = [{'source': 'remote', 'reason': 'unreachable'}]
    setup_run(app, issue, monkeypatch, iter([report, report]))
    monkeypatch.setattr(app, 'attach', lambda r: 3)
    with pytest.raises(mod.IssueError, match='incomplete'): app.run_one(args, args.target)
    args.allow_incomplete_check = True
    assert app.run_one(args, args.target) == 3


@pytest.mark.parametrize('saved', [True, False])
def test_start_skips_unavailable_sources(mod, app, issue, args, monkeypatch, tmp_path, capsys, saved):
    app.cfg.hosts = {'offline': 'offline'}
    owner = dict(provider='local', id='w', evidence='saved', state='saved',
                 verified=True, resume=True, worktree='/w')
    monkeypatch.setattr(app, 'local', lambda i: [owner] if saved else [])
    monkeypatch.setattr(app, 'github', lambda i: [])
    monkeypatch.setattr(app, 'codex', lambda i: [])
    monkeypatch.delenv('CURSOR_API_KEY', raising=False)
    monkeypatch.setattr(app, 'remote', lambda *a: (_ for _ in ()).throw(mod.IssueError('ssh failed')))
    report = app.scan(issue)
    assert len(report['incomplete']) == 2
    assert all(f['skipped'] for f in report['incomplete'])
    setup_run(app, issue, monkeypatch, iter([report] if saved else [report, report]))
    monkeypatch.setattr(mod, 'command', lambda *a: str(tmp_path))
    monkeypatch.setattr(app, 'launch', lambda *a: {'session': 'new'})
    monkeypatch.setattr(app, 'attach', lambda row: 7)
    assert app.run_one(args, args.target) == 7
    assert 'Warning: skipped offline' in capsys.readouterr().out


def test_missing_codex_is_skipped(mod, app, issue, monkeypatch):
    monkeypatch.setattr(mod.shutil, 'which', lambda name: None)
    with pytest.raises(mod.DiscoverySkipped, match='not installed'):
        app.codex(issue)


def test_override_never_bypasses_local_scan_failure(mod, app, issue, args, monkeypatch):
    report = clean_report()
    report['incomplete'] = [{'source': 'local', 'reason': 'failed'}]
    setup_run(app, issue, monkeypatch, iter([report]))
    args.allow_incomplete_check = True
    with pytest.raises(mod.IssueError, match='local discovery'):
        app.run_one(args, args.target)


@pytest.mark.parametrize('kind', ['pr', 'branch'])
def test_recover_published_issue_work(mod, app, issue, args, monkeypatch, tmp_path, capsys, kind):
    row = mod.candidate('github', 'branch:issue/12', 'explicit issue reference',
                        branch='issue/12', head='a'*40, url='https://github.com/org/repo/tree/issue/12')
    if kind == 'pr':
        row.update(id='https://github.com/org/repo/pull/5', pr='https://github.com/org/repo/pull/5')
    report = clean_report(); report['matches'] = [row]
    late = dict(report, incomplete=[dict(source='offline', reason='ssh failed', skipped=True)])
    setup_run(app, issue, monkeypatch, iter([report, late]))
    monkeypatch.setattr(mod.sys.stdin, 'isatty', lambda: False)
    calls, launches = [], []
    def command(argv, **kw):
        calls.append(argv)
        if argv[0] == 'git': return str(tmp_path)
        if kind == 'branch':
            assert argv[-1].endswith('/branches/issue%2F12')
            return json.dumps({'commit': {'sha': 'a'*40}})
        return json.dumps(dict(state='open', head=dict(ref='issue/12', sha='a'*40,
                         repo={'clone_url': 'https://github.com/org/repo.git'})))
    monkeypatch.setattr(mod, 'command', command)
    monkeypatch.setattr(app, 'launch', lambda *a: launches.append(a) or {'session': 'new'})
    monkeypatch.setattr(app, 'takeover', lambda *a: pytest.fail('published recovery must not ask for stopped'))
    monkeypatch.setattr(app, 'attach', lambda r: 0)
    assert app.run_one(args, args.target) == 0
    handoff = launches[0][-1]
    assert handoff['head'] == 'a'*40 and handoff['branch'] == 'issue/12'
    assert handoff['pr'] == row.get('pr')
    assert len([a for a in calls if a[0] == 'gh']) == 2
    prompt = app.prompt(issue, report, handoff, 'token')
    assert ('Reuse PR' in prompt) == (kind == 'pr')
    assert 'unpushed changes' in prompt
    output = capsys.readouterr().out
    assert 'Warning: skipped offline' in output and 'Only pushed commits' in output


@pytest.mark.parametrize('change', ['head', 'removed', 'active', 'closed', 'branch_head'])
def test_recovery_rechecks_before_launch(mod, app, issue, args, monkeypatch, tmp_path, change):
    row = mod.candidate('github', 'pr', 'explicit issue reference',
                        branch='fix', head='a'*40, pr='https://github.com/org/repo/pull/5', url='pr')
    report = clean_report(); report['matches'] = [row]
    if change == 'branch_head':
        row.pop('pr')
    fresh = dict(report, matches=[dict(row)])
    if change == 'removed': fresh['matches'] = []
    if change == 'active': fresh['matches'][0]['state'] = 'running'
    setup_run(app, issue, monkeypatch, iter([report, fresh]))
    reads = []
    def command(argv, **kw):
        if argv[0] == 'git': return str(tmp_path)
        reads.append(argv)
        if change == 'closed' and len(reads) > 1: return '{"state":"closed"}'
        sha = ('b' if len(reads) > 1 else 'a') * 40
        if change == 'branch_head': return json.dumps({'commit': {'sha': sha}})
        return json.dumps(dict(state='open', head=dict(ref='fix', sha=sha,
                         repo={'clone_url': 'https://github.com/org/repo.git'})))
    monkeypatch.setattr(mod, 'command', command)
    monkeypatch.setattr(app, 'launch', lambda *a: pytest.fail('must not launch'))
    with pytest.raises(mod.IssueError, match='changed|active|open'):
        app.run_one(args, args.target)


def test_ambiguous_published_matches_require_review(mod, app, monkeypatch):
    row = mod.candidate('github', 'branch:issue-12', 'branch or task number (review required)',
                        branch='issue-12', head='a'*40, url='branch')
    report = clean_report(); report['matches'] = [row]
    monkeypatch.setattr(mod.sys.stdin, 'isatty', lambda: False)
    with pytest.raises(mod.IssueError, match='review'):
        app.choose(report)
    row['evidence'] = 'explicit issue reference'
    report['matches'].append(dict(row, id='another'))
    with pytest.raises(mod.IssueError, match='review'):
        app.choose(report)
    monkeypatch.setattr(mod.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(app, 'pick_rows', lambda defaults, all_rows, *a, **kw: [all_rows[0]])
    assert app.choose(report) == row


def test_private_metadata_is_atomic(mod, tmp_path, monkeypatch):
    path = tmp_path / 'marker'
    mod.private_write(path, 'old')
    assert path.stat().st_mode & 0o777 == 0o600
    monkeypatch.setattr(mod.os, 'replace', lambda *a: (_ for _ in ()).throw(OSError('disk error')))
    with pytest.raises(OSError): mod.private_write(path, 'new')
    assert path.read_text() == 'old' and list(tmp_path.iterdir()) == [path]


def test_takeover_changed_head(mod, app, issue, args, monkeypatch, tmp_path):
    row = dict(provider='github', id='pr', evidence='reference', state='unknown', url='pr', pr='pr', head='b'*40)
    report = clean_report(); report['matches'] = [row]
    setup_run(app, issue, monkeypatch, iter([report, report]))
    monkeypatch.setattr(app, 'choose', lambda r: row)
    monkeypatch.setattr(app, 'takeover', lambda *a: {'pr': 'pr', 'head': 'a'*40})
    monkeypatch.setattr(mod, 'command', lambda *a: str(tmp_path))
    with pytest.raises(mod.IssueError, match='published work changed'): app.run_one(args, args.target)


def test_standalone_json_check_never_launches(tmp_path):
    home = tmp_path / 'home'; home.mkdir()
    bins = home / 'bin'; bins.mkdir()
    config = home / 'config' / 't'; config.mkdir(parents=True)
    env = {'PATH': str(bins) + ':' + os.environ['PATH'], 'HOME': str(home),
           'XDG_CONFIG_HOME': str(home / 'config'), 'XDG_CACHE_HOME': str(home / 'cache'),
           'XDG_STATE_HOME': str(home / 'state'), 'T_RECOVERY_DISABLE': '1'}
    repo = home / 'repo'
    def git(*a):
        return subprocess.run(['git', *a], env=env, check=True, capture_output=True, text=True).stdout
    git('init', '-b', 'main', str(repo))
    git('-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '--allow-empty', '-m', 'initial')
    git('-C', str(repo), 'remote', 'add', 'origin', 'https://github.com/org/repo.git')
    git('-C', str(repo), 'update-ref', 'refs/remotes/origin/main', 'HEAD')
    (config / 'config.sh').write_text(f'DEV_REPOS[repo]={repo}\n')
    (bins / 'gh').write_text('#!' + sys.executable + '\nimport json, sys\n'
        'assert sys.argv[1] == "api"\n'
        'print(json.dumps({"title":"Repair login", "body":"Fix", "state":"open"}) '
        'if sys.argv[-1].endswith("/issues/12") else "[[]]")\n')
    (bins / 'codex').write_text('#!/bin/sh\n[ "$1 $2" = "cloud list" ] || exit 99\nprintf \'{"tasks": []}\\n\'\n')
    (bins / 'zsh').write_text('#!/bin/sh\nexit 0\n')
    for file in bins.iterdir(): file.chmod(0o755)
    result = subprocess.run([sys.executable, str(ROOT / 'bin/t'), 'issue', 'status', '12', '--json'],
                            cwd=repo, env=env, capture_output=True, text=True, timeout=15)
    assert result.returncode == 2, result.stderr
    report = json.loads(result.stdout)
    assert report['checked'] == ['local', 'github', 'codex-cloud']
    assert report['incomplete'][0]['source'] == 'cursor-cloud'
    assert report['matches'] == []
    assert git('-C', str(repo), 'branch', '--format=%(refname:short)').strip() == 'main'
    assert not list((repo / '.git').glob('t-issue*'))


def test_new_conversation_does_not_inherit_issue_owner(mod, app, issue, local_scan):
    state, marker, tx = local_scan
    # /new retains the tmux environment but changes the actual conversation.
    tx.write_text(json.dumps({'type': 'user', 'sessionId': 'sid',
                              'message': {'content': 'Unrelated task'}}))
    assert not app.local(issue)[0]['verified']


def test_desktop_owner_uses_existing_app_path(mod, app, issue, local_scan, monkeypatch):
    state, marker, tx = local_scan
    parse = app.api['_parse_rows']
    def app_rows(text):
        rows = parse(text)
        for row in rows: row['state'] = 'app'
        return rows
    app.api['_parse_rows'] = app_rows
    row = app.local(issue)[0]
    assert row['app'] and row['verified']
    calls = []
    monkeypatch.setattr(mod.subprocess, 'call', lambda a: calls.append(a) or 0)
    app.attach(row)
    assert '_t_open repo 1 --app' in calls[-1][-1]
    app.cfg.hosts['cloud'] = 'host'
    app.attach(dict(row, host='cloud'))
    assert 't session open repo 1 --app' in calls[-1][-1]


def test_install_workflow_preserves_prose_and_is_idempotent(mod, tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    codex = tmp_path / '.codex'; codex.mkdir()
    base = codex / 'AGENTS.md'; base.write_text('My existing instructions.\n'); base.chmod(0o640)
    override = codex / 'AGENTS.override.md'; override.write_text('Temporary preferences.\n')
    assert mod.install_workflow() == 0
    assert base.read_text().startswith('My existing instructions.\n')
    assert override.read_text().startswith('Temporary preferences.\n')
    assert base.stat().st_mode & 0o777 == 0o640
    claude = tmp_path / '.claude/CLAUDE.md'
    for path in [base, override, claude]:
        assert mod.workflow_policy() in path.read_text()
    snapshots = [(p, p.read_bytes(), p.stat().st_mtime_ns) for p in [base, override, claude]]
    assert mod.install_workflow() == 0
    assert all(p.read_bytes() == data and p.stat().st_mtime_ns == stamp for p, data, stamp in snapshots)
    monkeypatch.setattr(mod, 'workflow_policy', lambda: 'Updated policy.')
    assert mod.install_workflow() == 0
    assert 'Updated policy.' not in base.read_text()
    assert '<!-- t public-issues: begin -->' not in base.read_text()
    assert base.read_text().count('<!-- t agent-workflow: begin -->') == 1


def test_install_workflow_uses_custom_agent_homes(mod, tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'custom codex'))
    monkeypatch.setenv('CLAUDE_CONFIG_DIR', str(tmp_path / 'custom claude'))
    assert mod.install_workflow() == 0
    assert (tmp_path / 'custom codex/AGENTS.md').is_file()
    assert (tmp_path / 'custom claude/CLAUDE.md').is_file()
    assert not (tmp_path / '.codex').exists()
    assert not (tmp_path / '.claude').exists()


def test_install_workflow_keeps_empty_codex_override_inactive(mod, tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    codex = tmp_path / '.codex'; codex.mkdir()
    base = codex / 'AGENTS.md'; base.write_text('Existing global instructions.\n')
    override = codex / 'AGENTS.override.md'; override.write_text(' \n')
    assert mod.install_workflow() == 0
    assert override.read_text() == ' \n'
    assert base.read_text().startswith('Existing global instructions.\n')
    assert mod.workflow_policy() in base.read_text()


@pytest.mark.parametrize('existing', ['<!-- t public-issues: begin -->',
    '<!-- t public-issues: end --><!-- t public-issues: begin -->', 'symlink'])
def test_install_workflow_refuses_unsafe_files(mod, tmp_path, monkeypatch, existing, capsys):
    monkeypatch.setenv('HOME', str(tmp_path))
    codex = tmp_path / '.codex'; codex.mkdir()
    path = codex / 'AGENTS.md'
    target = tmp_path / 'untouched'; target.write_text('User-owned')
    if existing == 'symlink': path.symlink_to(target)
    else: path.write_text(existing)
    before = path.read_text()
    assert mod.install_workflow() == 1
    assert path.read_text() == before and target.read_text() == 'User-owned'
    assert ('symlink' if existing == 'symlink' else 'malformed') in capsys.readouterr().err


def test_install_workflow_detects_concurrent_edits(mod, tmp_path, monkeypatch):
    monkeypatch.setenv('HOME', str(tmp_path))
    path = tmp_path / '.codex/AGENTS.md'; path.parent.mkdir()
    path.write_text('before')
    # The consolidated installer uses snapshot identity checks during publication.
    helper = mod.runpy.run_path(str(ROOT / 'libexec/t_workflow.py'))
    original = helper['snapshot']
    reads = []
    def read(file):
        if file == path:
            reads.append(file)
            if len(reads) == 3:
                file.write_text('concurrent edit')
        return original(file)
    helper['sync'].__globals__['snapshot'] = read
    monkeypatch.setattr(mod.runpy, 'run_path', lambda *args: helper)
    assert mod.install_workflow() == 1
    assert path.read_text() == 'concurrent edit'
