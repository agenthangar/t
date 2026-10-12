"""Issue listing uses the same repository scope as other t list commands."""
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def mod():
    spec = importlib.util.spec_from_file_location('issue_list_test', ROOT / 'libexec/t_issue.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def listing(mod, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cfg = SimpleNamespace(
        repos={name: str(tmp_path / name) for name in ('api', 'api_copy', 'web')},
        hosts={}, repo_of_dir=lambda cwd: (None, None),
        agent_for=lambda alias, override: override or 'claude')
    app = mod.IssueCommand(cfg, {
        '_t_source_root': lambda: str(ROOT),
        'Style': lambda tty=None: SimpleNamespace(y='', r='', b='', c='', g='', w=''),
        '_columns': lambda: 100,
        '_truncate': lambda text, width: text[:width],
    })
    monkeypatch.setattr(app, 'pick_rows', lambda *a, **kw: pytest.fail('list must not open a picker'))
    origins = {'api': 'Org/API', 'api_copy': 'Org/API', 'web': 'Org/Web'}

    def command(argv, **kwargs):
        assert argv[:3] == ['git', '-C', argv[2]]
        alias = Path(argv[2]).name
        return f'https://github.com/{origins[alias]}.git\n'

    monkeypatch.setattr(mod, 'command', command)
    return app, cfg, mod


def args(*, repo=None, repository=None, all=False, json=False, state='open'):
    return SimpleNamespace(action='list', repo=repo, repository=repository,
                           all=all, json=json, state=state)


def issue(repo, number, title='A title'):
    return dict(repo=repo, number=number, title=title, state='open',
                url=f'https://github.com/{repo}/issues/{number}', labels=[], assignees=[])


def test_outside_checkout_lists_each_registered_github_repository_once(listing, monkeypatch, capsys):
    app, cfg, _ = listing
    seen = []

    def get_issues(repo, state):
        seen.append((repo, state))
        return [issue(repo, 12 if repo == 'org/api' else 34)]

    monkeypatch.setattr(app, 'issues', get_issues)
    assert app.list_issues(args()) == 0
    output = capsys.readouterr().out
    assert len(seen) == 2
    assert {repo for repo, state in seen} == {'org/api', 'org/web'}
    assert all(state == 'open' for _, state in seen)
    assert 'REPO' in output and 'ISSUE' in output and 'STATE' in output and 'TITLE' in output
    assert 'org/api' in output and 'org/web' in output
    assert '#12' in output and '#34' in output


def test_inferred_and_explicit_repository_scope_and_all_override(listing, monkeypatch, capsys):
    app, cfg, _ = listing
    cfg.repo_of_dir = lambda cwd: ('api', '1')
    seen = []
    monkeypatch.setattr(app, 'issues', lambda repo, state: seen.append(repo) or [])

    assert app.list_issues(args()) == 0
    assert seen == ['org/api']
    capsys.readouterr()

    seen.clear()
    assert app.list_issues(args(repo='web')) == 0
    assert seen == ['org/web']
    capsys.readouterr()

    seen.clear()
    assert app.list_issues(args(all=True)) == 0
    assert set(seen) == {'org/api', 'org/web'}
    assert len(seen) == 2


def test_list_json_works_outside_checkout_and_has_canonical_repo(listing, monkeypatch, capsys):
    app, _, _ = listing
    monkeypatch.setattr(app, 'issues', lambda repo, state: [issue(repo, 17, repo)])
    assert app.list_issues(args(json=True)) == 0
    rows = json.loads(capsys.readouterr().out)
    assert len(rows) == 2
    assert {row['repo'] for row in rows} == {'org/api', 'org/web'}
    assert all(row['url'].startswith('https://github.com/' + row['repo']) for row in rows)


def test_explicit_github_slug_works_without_registered_checkout(listing, monkeypatch, capsys):
    app, cfg, _ = listing
    cfg.repos.clear()
    seen = []
    monkeypatch.setattr(app, 'issues', lambda repo, state: seen.append(repo) or [issue(repo, 9)])
    assert app.list_issues(args(repo='Else/Repo', json=True)) == 0
    assert seen == ['else/repo']
    assert json.loads(capsys.readouterr().out)[0]['repo'] == 'else/repo'


def test_explicit_repo_and_all_conflict_before_provider(listing, monkeypatch):
    app, _, mod = listing
    monkeypatch.setattr(app, 'issues', lambda *a: pytest.fail('should reject before provider'))
    for supplied in (args(repo='api', all=True), args(repository='api', all=True)):
        with pytest.raises(mod.IssueError, match='--all|once|repository'):
            app.list_issues(supplied)


def test_partial_provider_failure_reports_error_and_preserves_successes(listing, monkeypatch, capsys):
    app, _, mod = listing

    def get_issues(repo, state):
        if repo == 'org/api':
            raise mod.IssueError('gh failed (exit 4)')
        return [issue('org/web', 42, 'Keep successful row')]

    monkeypatch.setattr(app, 'issues', get_issues)
    assert app.list_issues(args(json=True)) != 0
    output = capsys.readouterr()
    assert [row['number'] for row in json.loads(output.out)] == [42]
    assert 'api' in output.err and 'gh failed' in output.err


def test_unreadable_registered_remote_is_reported_with_other_repositories(listing, monkeypatch, capsys):
    app, cfg, mod = listing
    cfg.repos.pop('api_copy')

    def get_origin(argv, **kwargs):
        if Path(argv[2]).name == 'api':
            raise mod.IssueError('git failed (exit 2)')
        return 'git@github.com:Org/Web.git\n'

    monkeypatch.setattr(mod, 'command', get_origin)
    monkeypatch.setattr(app, 'issues', lambda repo, state: [issue(repo, 7)])
    assert app.list_issues(args(json=True)) != 0
    output = capsys.readouterr()
    assert [row['repo'] for row in json.loads(output.out)] == ['org/web']
    assert 'api' in output.err and 'git failed' in output.err


def test_valid_repositories_with_no_matching_issues_have_scoped_empty_message(
        listing, monkeypatch, capsys):
    app, _, _ = listing
    monkeypatch.setattr(app, 'issues', lambda repo, state: [])
    assert app.list_issues(args(repo='api', state='closed')) == 0
    output = capsys.readouterr()
    assert 'No closed issues in api.' in output.out
    assert output.err == ''


def test_all_provider_failures_are_nonzero_with_valid_json(listing, monkeypatch, capsys):
    app, _, mod = listing

    def unavailable(repo, state):
        raise mod.IssueError('gh failed (exit 4)')

    monkeypatch.setattr(app, 'issues', unavailable)
    assert app.list_issues(args(json=True)) != 0
    output = capsys.readouterr()
    assert json.loads(output.out) == []
    assert 'org/api' in output.err and 'org/web' in output.err


def test_bad_origin_in_explicit_scope_does_not_widen_to_other_repositories(
        listing, monkeypatch, capsys):
    app, _, mod = listing

    def get_origin(argv, **kwargs):
        if Path(argv[2]).name == 'api':
            raise mod.IssueError('git failed (exit 2)')
        return 'git@github.com:Org/Web.git\n'

    monkeypatch.setattr(mod, 'command', get_origin)
    monkeypatch.setattr(app, 'issues', lambda *a: pytest.fail('do not fetch a different repository'))
    assert app.list_issues(args(repo='api', json=True)) != 0
    output = capsys.readouterr()
    assert json.loads(output.out) == []
    assert 'api' in output.err and 'git failed' in output.err


def test_empty_registration_is_graceful_without_picker(listing, capsys):
    app, cfg, _ = listing
    cfg.repos.clear()
    assert app.list_issues(args()) == 0
    assert 'No repositories registered.' in capsys.readouterr().out
    assert app.list_issues(args(json=True)) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_empty_results_with_a_failed_repository_are_marked_incomplete(listing, monkeypatch, capsys):
    app, _, mod = listing

    def get_issues(repo, state):
        if repo == 'org/api':
            raise mod.IssueError('gh failed (exit 4)')
        return []

    monkeypatch.setattr(app, 'issues', get_issues)
    assert app.list_issues(args()) == 1
    output = capsys.readouterr()
    assert 'Issue list incomplete' in output.out
    assert 'org/api' in output.err
