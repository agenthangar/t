"""The issue verbs keep listing and inspection separate from starting work."""
import importlib.util
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import time
from types import SimpleNamespace
import uuid

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def mod():
    spec = importlib.util.spec_from_file_location('issue_verbs_test', ROOT / 'libexec/t_issue.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cfg(tmp_path):
    return SimpleNamespace(repos={'repo': str(tmp_path)}, hosts={},
                           repo_of_dir=lambda cwd: ('repo', '1'),
                           agent_for=lambda alias, override: override or 'claude')


def test_split_targets_supports_batch_repo_and_deduplication(mod):
    assert mod.split_targets(['repo', '12', '13', '12']) == ('repo', ['12', '13'])
    assert mod.split_targets(['12', '13'], 'repo') == ('repo', ['12', '13'])
    assert mod.split_targets(['repo']) == ('repo', [])
    assert mod.split_targets([]) == (None, [])
    with pytest.raises(mod.IssueError, match='once'):
        mod.split_targets(['repo', '12'], 'other')
    with pytest.raises(mod.IssueError, match='positive'):
        mod.split_targets(['repo', '0'])


def test_repository_infers_and_verifies_origin(mod, cfg, monkeypatch):
    monkeypatch.setattr(mod, 'command', lambda argv: 'git@github.com:Org/Repo.git\n')
    assert mod.repository(None, cfg) == {
        'repo': 'org/repo', 'alias': 'repo', 'directory': cfg.repos['repo']}
    assert mod.repository('other/repo', cfg, require_checkout=False) == {'repo': 'other/repo'}
    with pytest.raises(mod.IssueError, match='registered checkout'):
        mod.repository('other/repo', cfg)


def test_issue_list_is_read_only_and_filters_pull_requests(mod, cfg, monkeypatch, capsys):
    calls = []
    def command(argv, **kwargs):
        calls.append(argv)
        if argv[0] == 'git':
            return 'https://github.com/org/repo.git\n'
        return json.dumps([[{'number': 12, 'title': 'Fix issue', 'state': 'open',
                             'html_url': 'https://github.com/org/repo/issues/12'},
                            {'number': 13, 'title': 'A pull request', 'pull_request': {'url': 'x'}}]])
    monkeypatch.setattr(mod, 'command', command)
    app = mod.IssueCommand(cfg, {'_t_source_root': lambda: str(ROOT)})
    args = SimpleNamespace(action='list', repo='repo', state='open', json=True)
    assert app.run(args) == 0
    assert [row['number'] for row in json.loads(capsys.readouterr().out)] == [12]
    assert any('state=open' in ' '.join(call) for call in calls if call[0] == 'gh')
    assert all(call[0] in {'gh', 'git'} for call in calls)


def test_batch_starts_every_selected_issue_before_attaching(mod, cfg, monkeypatch):
    app = mod.IssueCommand(cfg, {'_t_source_root': lambda: str(ROOT)})
    events = []
    monkeypatch.setattr(app, 'start_targets', lambda args: [['12'], ['13'], ['14']])
    def run_one(args, words, *, attach=True):
        events.append(('start', words[0], attach))
        if words[0] == '13':
            raise mod.IssueError('provider unavailable')
        return {'session': 'dev-repo-' + words[0]}
    monkeypatch.setattr(app, 'run_one', run_one)
    monkeypatch.setattr(app, 'attach_many', lambda rows: events.append(('attach', rows)) or 0)
    args = SimpleNamespace(action='start')
    assert app.run(args) != 0
    assert events[:3] == [('start', '12', False), ('start', '13', False), ('start', '14', False)]
    assert events[3][0] == 'attach'
    assert len(events[3][1]) == 2


def test_start_without_ids_uses_shared_multiselect(mod, cfg, monkeypatch):
    app = mod.IssueCommand(cfg, {'_t_source_root': lambda: str(ROOT)})
    rows = [dict(repo='org/repo', number=n, state=state, title=f'Issue {n}',
                 url=f'https://github.com/org/repo/issues/{n}', labels=[], assignees=[])
            for n, state in ((12, 'open'), (13, 'closed'), (14, 'open'))]
    monkeypatch.setattr(app, 'issues', lambda repo, state: rows)
    observed = {}
    def choose(default, all_rows, *args, **kwargs):
        observed['default'], observed['all'], observed['multi'] = default, all_rows, kwargs.get('multi', True)
        return [all_rows[0], all_rows[2]]
    monkeypatch.setattr(app, 'pick_rows', choose)
    assert app.start_targets(SimpleNamespace(target=[], repo=None)) == [
        ['https://github.com/org/repo/issues/12'],
        ['https://github.com/org/repo/issues/14']]
    assert len(observed['default']) == 2 and len(observed['all']) == 3
    assert observed['multi']


def test_picker_passes_private_rows_to_shared_ui_and_cleans_up(mod, cfg, monkeypatch):
    app = mod.IssueCommand(cfg, {'_t_source_root': lambda: str(ROOT)})
    monkeypatch.setattr(mod.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setenv('COV_CORE_SOURCE', 'must-not-leak')
    rows = ['org/repo\t12\tissue\tOne', 'org/repo\t13\tissue\tTwo']
    calls = []
    def run(argv, **kwargs):
        defaults, all_rows = map(Path, argv[5:7])
        assert defaults.read_text() == rows[0] + '\n'
        assert all_rows.read_text() == '\n'.join(rows) + '\n'
        assert defaults.stat().st_mode & 0o777 == 0o600
        assert kwargs['env']['T_PICKER_ALL_LABEL'] == 'All issues'
        assert 'COV_CORE_SOURCE' not in kwargs['env']
        calls.append(defaults.parent)
        return SimpleNamespace(returncode=0, stdout=rows[1] + '\n' + rows[1] + '\n')
    monkeypatch.setattr(mod.subprocess, 'run', run)
    assert app.pick_rows(rows[:1], rows, 'start', 'Open issues', 'legend') == [rows[1]]
    assert not calls[0].exists()


def test_owner_picker_has_private_details_preview(mod, cfg, monkeypatch):
    app = mod.IssueCommand(cfg, {'_t_source_root': lambda: str(ROOT)})
    monkeypatch.setattr(mod.sys.stdin, 'isatty', lambda: True)
    row = 'owner\t0\twork\tSession summary'
    directories = []
    def run(argv, **kw):
        directory = Path(argv[5]).parent
        directories.append(directory)
        detail = directory / 'detail-0'
        assert detail.read_text() == 'Conversation: exact-id\nWorking on: full summary'
        assert detail.stat().st_mode & 0o777 == 0o600
        assert any(a.startswith('--preview=cat -- ') and a.endswith('/detail-{2}') for a in argv)
        assert '--preview-window=down,35%,wrap' in argv
        return SimpleNamespace(returncode=0, stdout=row+'\n')
    monkeypatch.setattr(mod.subprocess, 'run', run)
    assert app.pick_rows([row], [row], 'continue', 'work', 'details below', multi=False,
                         details=['Conversation: exact-id\nWorking on: full summary']) == [row]
    assert not directories[0].exists()


def test_picker_rejects_cancellation_invalid_rows_and_multi_owner(mod, cfg, monkeypatch):
    app = mod.IssueCommand(cfg, {'_t_source_root': lambda: str(ROOT)})
    monkeypatch.setattr(mod.sys.stdin, 'isatty', lambda: True)
    responses = iter([(130, ''), (127, ''), (0, ''), (0, 'other\n'), (0, 'a\nb\n'), (0, 'a\n')])
    def run(argv, **kwargs):
        status, stdout = next(responses)
        assert '_t_fzf_single' in argv[2]
        assert kwargs['env']['T_PICKER_ALL_LABEL'] == 'Existing work'
        return SimpleNamespace(returncode=status, stdout=stdout)
    monkeypatch.setattr(mod.subprocess, 'run', run)
    with pytest.raises(mod.IssueCancelled, match='cancelled'):
        app.pick_rows(['a'], ['a', 'b'], 'continue', 'work', 'legend', multi=False)
    for message in ('unavailable', 'invalid', 'invalid', 'invalid'):
        with pytest.raises(mod.IssueError, match=message):
            app.pick_rows(['a'], ['a', 'b'], 'continue', 'work', 'legend', multi=False)
    assert app.pick_rows(['a'], ['a', 'b'], 'continue', 'work', 'legend', multi=False) == ['a']
    monkeypatch.setattr(mod.sys.stdin, 'isatty', lambda: False)
    with pytest.raises(mod.IssueError, match='terminal'):
        app.pick_rows(['a'], ['a'], 'start', 'issues', 'legend')


def test_attach_many_opens_extra_tabs_after_dedup_and_prints_fallback(mod, cfg, monkeypatch, capsys):
    app = mod.IssueCommand(cfg, {'_t_source_root': lambda: str(ROOT)})
    monkeypatch.delenv('TMUX', raising=False)
    rows = [{'host': 'local', 'session': 'dev-repo-1'},
            {'host': 'local', 'session': 'dev-repo-2'}]
    calls = []
    def call(argv):
        calls.append(argv)
        return 1 if argv[0] == 'zsh' else 0
    monkeypatch.setattr(mod.subprocess, 'call', call)
    assert app.attach_many([rows[0], rows[0], rows[1]]) == 0
    assert len(calls) == 2
    assert '_dev_open_tab' in calls[0][-1]
    assert calls[1] == ['tmux', 'attach-session', '-t', '=dev-repo-1']
    assert 'Attach with: tmux attach-session -t =dev-repo-2' in capsys.readouterr().out
    assert app.attach_many([]) == 0


def test_attach_many_keeps_app_resume_path_and_first_result(mod, cfg, monkeypatch):
    app = mod.IssueCommand(cfg, {'_t_source_root': lambda: str(ROOT)})
    app_row = {'host': 'local', 'session': 'dev-repo-2', 'alias': 'repo', 'slot': '2', 'app': True}
    main_row = {'host': 'local', 'session': 'dev-repo-1'}
    monkeypatch.setattr(mod.subprocess, 'call', lambda argv: 7 if argv[0] == 'zsh' else 0)
    assert app.attach_many([main_row, app_row]) == 7


def test_mixed_number_and_url_deduplicate_canonical_issue(mod, cfg, monkeypatch):
    cfg.repos['other'] = cfg.repos['repo'] + '/other'
    monkeypatch.setattr(mod, 'command', lambda argv: 'git@github.com:' +
                        ('other/repo' if argv[2] == cfg.repos['other'] else 'org/repo') + '.git\n')
    app = mod.IssueCommand(cfg, {'_t_source_root': lambda: str(ROOT)})
    url = 'https://github.com/org/repo/issues/12'
    assert app.start_targets(SimpleNamespace(target=['repo', '12', url, '13'], repo=None)) == [
        ['repo', '12'], ['repo', '13']]
    with pytest.raises(mod.IssueError, match='does not match'):
        app.start_targets(SimpleNamespace(target=[url], repo='other'))


def test_status_rejects_missing_or_multiple_ids_before_discovery(mod, cfg, monkeypatch):
    app = mod.IssueCommand(cfg, {'_t_source_root': lambda: str(ROOT)})
    monkeypatch.setattr(app, 'run_one', lambda *a, **kw: pytest.fail('unexpected discovery'))
    monkeypatch.setattr(app, 'select_issues', lambda repo: pytest.fail('unexpected picker'))
    for words in ([], ['12', '13']):
        with pytest.raises(mod.IssueError, match='usage'):
            app.run(SimpleNamespace(action='status', target=words, repo=None))


def test_closed_and_failed_items_do_not_stop_batch(mod, cfg, monkeypatch, capsys):
    app = mod.IssueCommand(cfg, {'_t_source_root': lambda: str(ROOT)})
    monkeypatch.setattr(app, 'start_targets', lambda args: [['12'], ['13'], ['14']])
    events = []
    def run_one(args, words, *, attach=True):
        events.append(words[0])
        if words[0] == '12':
            return None  # closed issue
        if words[0] == '13':
            raise mod.IssueError('incomplete scan')
        return {'host': 'local', 'session': 'dev-repo-14'}
    monkeypatch.setattr(app, 'run_one', run_one)
    monkeypatch.setattr(app, 'attach_many', lambda rows: events.append(rows) or 0)
    assert app.run(SimpleNamespace(action='start')) == 1
    assert events == ['12', '13', '14', [{'host': 'local', 'session': 'dev-repo-14'}]]
    assert 'incomplete scan' in capsys.readouterr().err


def test_cancel_during_issue_batch_keeps_prior_launch_and_stops_remaining(
        mod, cfg, monkeypatch, capsys):
    app = mod.IssueCommand(cfg, {'_t_source_root': lambda: str(ROOT)})
    monkeypatch.setattr(app, 'start_targets', lambda args: [['12'], ['13'], ['14']])
    events = []

    def run_one(args, words, *, attach=True):
        events.append(('start', words[0], attach))
        if words[0] == '13':
            raise mod.IssueCancelled('selection cancelled')
        return {'host': 'local', 'session': 'dev-repo-' + words[0]}

    monkeypatch.setattr(app, 'run_one', run_one)
    monkeypatch.setattr(app, 'attach_many', lambda rows: events.append(
        ('attach', rows)) or 0)
    assert app.run(SimpleNamespace(action='start')) == 130
    assert events == [('start', '12', False), ('start', '13', False)]
    assert 'remaining issues left unchanged' in capsys.readouterr().err


@pytest.mark.parametrize('error', ['cancel', 'eof'])
def test_issue_wrapper_maps_aborted_selection_to_130(mod, cfg, monkeypatch, error):
    failure = mod.IssueCancelled('cancelled') if error == 'cancel' else EOFError()
    monkeypatch.setattr(mod.IssueCommand, 'run', lambda *a: (_ for _ in ()).throw(failure))
    assert mod.run(cfg, SimpleNamespace(action='start'),
                   {'_t_source_root': lambda: str(ROOT)}) == 130


def test_dispatch_launches_two_issues_before_any_attach(mod, cfg, tmp_path, monkeypatch):
    app = mod.IssueCommand(cfg, {'_t_source_root': lambda: str(ROOT)})
    monkeypatch.setattr(app, 'start_targets', lambda args: [['12'], ['13']])
    monkeypatch.setattr(app, 'issue', lambda words: dict(
        repo='org/repo', alias='repo', directory=str(tmp_path), number=int(words[0]),
        url=f'https://github.com/org/repo/issues/{words[0]}', title='Fix',
        body='', comments=[], state='open'))
    monkeypatch.setattr(app, 'scan', lambda *a: dict(matches=[], incomplete=[], checked=['local']))
    monkeypatch.setattr(app, 'display', lambda *a: None)
    monkeypatch.setattr(mod, 'command', lambda argv, **kw: str(tmp_path))
    events = []
    def launch(issue, report, agent, handoff):
        events.append(('launch', issue['number'], agent))
        return {'host': 'local', 'session': f'dev-repo-{issue["number"]}'}
    monkeypatch.setattr(app, 'launch', launch)
    monkeypatch.setattr(app, 'attach_many', lambda rows: events.append(
        ('attach', [r['session'] for r in rows])) or 0)
    args = SimpleNamespace(action='start', allow_incomplete_check=False,
                           codex=False, claude=False, scan_host=False, json=False)
    assert app.run(args) == 0
    assert events == [('launch', 12, 'claude'), ('launch', 13, 'claude'),
                      ('attach', ['dev-repo-12', 'dev-repo-13'])]


def test_batch_starts_two_real_isolated_agents_before_attaching(mod, tmp_path, monkeypatch):
    tmux, zsh = shutil.which('tmux'), shutil.which('zsh')
    if not tmux or not zsh:
        pytest.skip('tmux and zsh required')
    home = tmp_path / 'home'
    home.mkdir()
    bindir = home / 'bin'
    bindir.mkdir()
    socket = '/tmp/t-issue-batch-' + uuid.uuid4().hex
    env = {'HOME': str(home), 'ZDOTDIR': str(home),
           'PATH': str(bindir) + ':' + os.environ['PATH'],
           'XDG_CONFIG_HOME': str(home / 'config'), 'XDG_CACHE_HOME': str(home / 'cache'),
           'XDG_STATE_HOME': str(home / 'state'), 'T_LOCAL_RC': str(home / 'local.zsh'),
           'T_RECOVERY_DISABLE': '1', 'T_AUTO_TRUST': '0', 'SHELL': zsh,
           'TERM': 'xterm-256color', 'SHELL_SESSIONS_DISABLE': '1'}
    (bindir / 'tmux').write_text('#!/bin/sh\nexec ' +
        shlex.join([tmux, '-S', socket, '-f', '/dev/null']) + ' "$@"\n')
    (bindir / 'claude').write_text('#!' + sys.executable + '\n'
        'import json, pathlib, sys, time\n'
        'pathlib.Path("agent-started").write_text(json.dumps(sys.argv[1:]))\n'
        'time.sleep(30)\n')
    for file in bindir.iterdir():
        file.chmod(0o755)
    (home / '.zshenv').write_text('skip_global_compinit=1\n')
    (home / '.zshrc').write_text('export PATH=' + shlex.quote(str(bindir)) + ':$PATH\n')
    repo = home / 'repo'
    def git(*argv):
        return subprocess.run(['git', *argv], env=env, check=True,
                              capture_output=True, text=True).stdout
    git('init', '-b', 'main', str(repo))
    git('-C', str(repo), '-c', 'user.name=Test', '-c', 'user.email=test@example.invalid',
        'commit', '--allow-empty', '-m', 'initial')
    git('-C', str(repo), 'remote', 'add', 'origin', str(repo))
    git('-C', str(repo), 'fetch', 'origin')
    (home / 'local.zsh').write_text('DEV_REPOS[repo]=' + shlex.quote(str(repo)) + '\n'
        'DEV_WORKTREE_ROOT=' + shlex.quote(str(home / 'trees')) + '\n')
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    cfg = SimpleNamespace(repos={'repo': str(repo)}, hosts={},
                          repo_of_dir=lambda cwd: ('repo', '1'),
                          agent_for=lambda alias, override: override or 'claude')
    app = mod.IssueCommand(cfg, {'_t_source_root': lambda: str(ROOT)})
    monkeypatch.setattr(app, 'start_targets', lambda args: [['12'], ['13']])
    monkeypatch.setattr(app, 'issue', lambda words: dict(
        repo='org/repo', alias='repo', directory=str(repo), number=int(words[0]),
        url=f'https://github.com/org/repo/issues/{words[0]}', title='Fix',
        body='', comments=[], state='open'))
    monkeypatch.setattr(app, 'scan', lambda *a: dict(matches=[], incomplete=[], checked=['local']))
    monkeypatch.setattr(app, 'display', lambda *a: None)
    attached = []
    def verify_before_attach(rows):
        assert [row['session'] for row in rows] == ['dev-repo-1', 'dev-repo-2']
        deadline = time.monotonic() + 5
        trees = [home / 'trees' / 'repo' / 'repo-1', home / 'trees' / 'repo' / 'repo-2']
        while not all((tree / 'agent-started').exists() for tree in trees) and time.monotonic() < deadline:
            time.sleep(.05)
        for slot, tree in enumerate(trees, 1):
            assert (tree / 'agent-started').exists(), f'agent {slot} did not start'
            assert git('-C', str(tree), 'branch', '--show-current').strip() == f'dev/repo-{slot}'
            gitdir = Path(git('-C', str(tree), 'rev-parse', '--absolute-git-dir').strip())
            assert json.loads((gitdir / 't-issue.json').read_text())['state'] == 'started'
            assert subprocess.run(['tmux', 'has-session', '-t', '=dev-repo-' + str(slot)],
                                  env=env, capture_output=True).returncode == 0
        attached.extend(rows)
        return 0
    monkeypatch.setattr(app, 'attach_many', verify_before_attach)
    args = SimpleNamespace(action='start', allow_incomplete_check=False,
                           codex=False, claude=False, scan_host=False, json=False)
    try:
        assert app.run(args) == 0
        assert len(attached) == 2
        assert git('-C', str(repo), 'branch', '--show-current').strip() == 'main'
    finally:
        subprocess.run([tmux, '-S', socket, 'kill-server'], env=env, capture_output=True)
