"""Opening an issue delegates to GitHub's configured browser without starting work."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def mod():
    spec = importlib.util.spec_from_file_location('issue_open_test', ROOT / 'libexec/t_issue.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return SimpleNamespace(repos={'web': str(tmp_path / 'web')}, hosts={},
                           repo_of_dir=lambda cwd: (None, None))


@pytest.fixture
def app(mod, cfg):
    return mod.IssueCommand(cfg, {'_t_source_root': lambda: str(ROOT)})


def args(*target, repo=None):
    return SimpleNamespace(action='open', target=list(target), repo=repo)


def test_open_explicit_target_uses_gh_browser(app, mod, monkeypatch, capsys):
    monkeypatch.setenv('GH_BROWSER', 'my-browser')
    monkeypatch.setenv('COV_CORE_SOURCE', 'private-measurement-state')
    monkeypatch.setattr(app, 'issue', lambda *a: pytest.fail('opening must not scan issue work'))
    monkeypatch.setattr(app, 'select_issues', lambda *a, **kw: pytest.fail('explicit IDs bypass picker'))
    calls = []

    def call(argv, **kwargs):
        calls.append((argv, kwargs))
        return 0

    monkeypatch.setattr(mod.subprocess, 'call', call)
    assert app.open_issues(args('org/web', '12', '13')) == 0
    assert [argv for argv, _ in calls] == [
        ['gh', 'issue', 'view', '12', '--repo', 'org/web', '--web'],
        ['gh', 'issue', 'view', '13', '--repo', 'org/web', '--web'],
    ]
    assert all(kwargs['env']['GH_BROWSER'] == 'my-browser' for _, kwargs in calls)
    assert all(kwargs['env']['GH_PROMPT_DISABLED'] == '1' for _, kwargs in calls)
    assert all(kwargs['env']['GIT_TERMINAL_PROMPT'] == '0' for _, kwargs in calls)
    assert all('COV_CORE_SOURCE' not in kwargs['env'] for _, kwargs in calls)
    assert capsys.readouterr().out.splitlines() == [
        'Opening https://github.com/org/web/issues/12',
        'Opening https://github.com/org/web/issues/13',
    ]


def test_open_without_target_uses_shared_issue_picker(app, mod, monkeypatch):
    url12 = 'https://github.com/org/web/issues/12'
    url13 = 'https://github.com/org/web/issues/13'
    selected = []

    def choose(repo, *, action):
        selected.append((repo, action))
        return [[url12], [url13]]

    monkeypatch.setattr(app, 'select_issues', choose)
    calls = []
    monkeypatch.setattr(mod.subprocess, 'call', lambda argv, **kw: calls.append(argv) or 0)
    assert app.open_issues(args(repo='org/web')) == 0
    assert selected == [('org/web', 'open')]
    assert [argv[3] for argv in calls] == ['12', '13']


def test_open_picker_uses_browser_action_and_preserves_closed_issues(app, mod, monkeypatch):
    url12 = 'https://github.com/org/web/issues/12'
    url13 = 'https://github.com/org/web/issues/13'
    rows = [dict(repo='org/web', number=number, state=state, title=title, url=url,
                 labels=[], assignees=[])
            for number, state, title, url in ((12, 'open', 'One', url12),
                                               (13, 'closed', 'Two', url13))]
    monkeypatch.setattr(app, 'issues', lambda repo, state: rows)
    observed = {}

    def pick(default, all_rows, action, label, legend, **kwargs):
        observed.update(default=default, all_rows=all_rows, action=action,
                        label=label, legend=legend, kwargs=kwargs)
        return all_rows

    monkeypatch.setattr(app, 'pick_rows', pick)
    assert app.select_issues('org/web', action='open') == [[url12], [url13]]
    assert observed['action'] == 'open'
    assert observed['label'] == 'Open issues'
    assert 'browser' in observed['legend']
    assert len(observed['default']) == 1
    assert len(observed['all_rows']) == 2


def test_open_infers_current_checkout_alias(app, mod, cfg, monkeypatch):
    cfg.repo_of_dir = lambda cwd: ('web', '1')
    git_calls = []

    def command(argv, **kwargs):
        git_calls.append(argv)
        return 'git@github.com:Org/Web.git\n'

    monkeypatch.setattr(mod, 'command', command)
    calls = []
    monkeypatch.setattr(mod.subprocess, 'call', lambda argv, **kw: calls.append(argv) or 0)
    assert app.open_issues(args('12')) == 0
    assert git_calls == [['git', '-C', cfg.repos['web'], 'remote', 'get-url', 'origin']]
    assert calls == [['gh', 'issue', 'view', '12', '--repo', 'org/web', '--web']]


def test_open_accepts_issue_urls_without_registered_checkout(app, mod, monkeypatch):
    monkeypatch.setattr(app, 'resolve_repo', lambda *a, **kw: pytest.fail('URL has repo'))
    monkeypatch.setattr(mod, 'command', lambda *a, **kw: pytest.fail('URL needs no local checkout'))
    url = 'https://github.com/Org/Web/issues/12'
    calls = []
    monkeypatch.setattr(mod.subprocess, 'call', lambda argv, **kw: calls.append(argv) or 0)
    assert app.open_issues(args(url, url)) == 0
    assert calls == [['gh', 'issue', 'view', '12', '--repo', 'org/web', '--web']]


def test_open_rejects_malformed_or_mismatched_targets_before_browser(app, mod, monkeypatch):
    monkeypatch.setattr(mod.subprocess, 'call', lambda *a, **kw: pytest.fail('must validate all targets first'))
    good = 'https://github.com/org/web/issues/12'
    other = 'https://github.com/other/web/issues/13'
    with pytest.raises(mod.IssueError, match='targets'):
        app.open_issues(args('org/web', '12', 'abc'))
    with pytest.raises(mod.IssueError, match='does not match'):
        app.open_issues(args(good, other, repo='org/web'))
    with pytest.raises(mod.IssueError, match='GitHub owner/repo'):
        app.open_issues(args('https://github.com/my%20org/web/issues/12'))


def test_open_missing_repo_noninteractive_fails_without_picker_or_browser(
        app, mod, monkeypatch):
    monkeypatch.setattr(mod.sys.stdin, 'isatty', lambda: False)
    monkeypatch.setattr(mod.sys.stdout, 'isatty', lambda: False)
    monkeypatch.setattr(app, 'select_issues', lambda *a, **kw: pytest.fail('no picker'))
    monkeypatch.setattr(mod.subprocess, 'call', lambda *a, **kw: pytest.fail('no browser'))
    with pytest.raises(mod.IssueError, match='repo'):
        app.open_issues(args('12'))


def test_open_closed_issue_and_browser_failure_propagation(app, mod, monkeypatch):
    monkeypatch.setattr(app, 'issue', lambda *a: pytest.fail('closed issues may open without an API check'))
    seen = []

    def call(argv, **kwargs):
        seen.append(argv)
        return 7

    monkeypatch.setattr(mod.subprocess, 'call', call)
    assert app.open_issues(args('org/web', '12', '13')) == 7
    assert seen == [['gh', 'issue', 'view', '12', '--repo', 'org/web', '--web']]


def test_open_browser_cancel_stops_batch(app, mod, monkeypatch):
    seen = []
    monkeypatch.setattr(mod.subprocess, 'call', lambda argv, **kw: seen.append(argv) or 130)
    assert app.open_issues(args('org/web', '12', '13')) == 130
    assert len(seen) == 1


def test_open_no_issues_exits_successfully(app, mod, monkeypatch, capsys):
    monkeypatch.setattr(app, 'issues', lambda repo, state: [])
    monkeypatch.setattr(mod.subprocess, 'call', lambda *a, **kw: pytest.fail('no browser'))
    assert app.open_issues(args(repo='org/web')) == 0
    assert 'No issues' in capsys.readouterr().out


def test_open_picker_cancel_returns_130_without_browser(mod, cfg, monkeypatch):
    cfg.repo_of_dir = lambda cwd: ('web', '1')
    monkeypatch.setattr(mod, 'command', lambda *a, **kw: 'git@github.com:org/web.git\n')
    monkeypatch.setattr(mod.IssueCommand, 'select_issues', lambda *a, **kw: (_ for _ in ()).throw(
        mod.IssueCancelled('selection cancelled')))
    monkeypatch.setattr(mod.subprocess, 'call', lambda *a, **kw: pytest.fail('no browser on cancel'))
    assert mod.run(cfg, args(), {'_t_source_root': lambda: str(ROOT)}) == 130


def test_open_browser_unavailable_reports_error_without_traceback(mod, cfg, monkeypatch, capsys):
    def missing(*a, **kw):
        raise OSError('missing gh')

    monkeypatch.setattr(mod.subprocess, 'call', missing)
    assert mod.run(cfg, args('org/web', '12'), {'_t_source_root': lambda: str(ROOT)}) == 1
    assert 'missing gh' in capsys.readouterr().err
