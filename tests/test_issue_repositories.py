"""Repository selection for issue verbs stays explicit and safe off-repo."""
import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def mod():
    spec = importlib.util.spec_from_file_location('issue_repositories_test', ROOT / 'libexec/t_issue.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return SimpleNamespace(repos={'api': str(tmp_path / 'api'), 'web': str(tmp_path / 'web')},
                           hosts={}, repo_of_dir=lambda cwd: (None, None),
                           agent_for=lambda alias, override: override or 'claude')


@pytest.fixture
def app(mod, cfg):
    return mod.IssueCommand(cfg, {'_t_source_root': lambda: str(ROOT)})


def test_resolve_repo_uses_explicit_and_inferred_repo_without_picker(app, cfg, monkeypatch):
    monkeypatch.setattr(app, 'pick_rows', lambda *a, **kw: pytest.fail('picker not needed'))
    assert app.resolve_repo('web') == 'web'
    cfg.repo_of_dir = lambda cwd: ('api', '1')
    assert app.resolve_repo(None) == 'api'


def test_missing_repo_uses_shared_single_repository_picker(app, mod, monkeypatch):
    monkeypatch.setattr(mod.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(mod.sys.stdout, 'isatty', lambda: True)
    observed = {}
    def choose(default, all_rows, *args, **kwargs):
        observed.update(default=default, all_rows=all_rows, args=args, kwargs=kwargs)
        return [next(row for row in all_rows if 'web' in row)]
    monkeypatch.setattr(app, 'pick_rows', choose)
    assert app.resolve_repo(None) == 'web'
    assert len(observed['all_rows']) == 2
    assert observed['kwargs']['multi'] is False
    assert observed['kwargs']['noun'] == 'repositories'


def test_multiple_numbers_resolve_repo_only_once(app, mod, monkeypatch):
    calls = []
    def resolve(repo, **kwargs):
        calls.append((repo, kwargs))
        return 'api'
    monkeypatch.setattr(app, 'resolve_repo', resolve)
    monkeypatch.setattr(mod, 'command', lambda argv, **kwargs: 'git@github.com:org/api.git\n')
    args = SimpleNamespace(action='start', target=['12', '13'], repo=None,
                           json=False, scan_host=False)
    assert app.start_targets(args) == [['api', '12'], ['api', '13']]
    assert len(calls) == 1


def test_issue_url_never_needs_repository_picker(app, mod, monkeypatch):
    monkeypatch.setattr(app, 'resolve_repo', lambda *a, **kw: pytest.fail('URL identifies repo'))
    monkeypatch.setattr(mod, 'command', lambda argv, **kwargs: 'git@github.com:org/api.git\n'
                        if argv[2] == app.cfg.repos['api'] else 'git@github.com:org/web.git\n')
    url = 'https://github.com/org/api/issues/12'
    args = SimpleNamespace(action='start', target=[url], repo=None,
                           json=False, scan_host=False)
    assert app.start_targets(args) == [[url]]


@pytest.mark.parametrize('action,json_mode,scan_host', [
    ('start', False, True), ('status', True, False),
])
def test_missing_repo_noninteractive_modes_fail_before_provider(app, mod, monkeypatch,
                                                                action, json_mode, scan_host):
    monkeypatch.setattr(app, 'pick_rows', lambda *a, **kw: pytest.fail('no interactive picker'))
    monkeypatch.setattr(mod, 'command', lambda *a, **kw: pytest.fail('no GitHub or Git request'))
    args = SimpleNamespace(action=action, target=['12'], repo=None,
                           json=json_mode, scan_host=scan_host)
    call = lambda: app.start_targets(args)
    with pytest.raises(mod.IssueError, match='repo'):
        call()


def test_repository_selection_cancel_and_empty_configuration(app, mod, cfg, monkeypatch):
    monkeypatch.setattr(mod.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(mod.sys.stdout, 'isatty', lambda: True)
    monkeypatch.setattr(app, 'pick_rows', lambda *a, **kw: (_ for _ in ()).throw(
        mod.IssueError('selection cancelled')))
    with pytest.raises(mod.IssueError, match='cancelled'):
        app.resolve_repo(None)
    cfg.repos.clear()
    with pytest.raises(mod.IssueError, match='t repo clone'):
        app.resolve_repo(None)


def test_redirected_output_does_not_open_repository_picker(app, mod, monkeypatch):
    monkeypatch.setattr(mod.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(mod.sys.stdout, 'isatty', lambda: False)
    monkeypatch.setattr(app, 'pick_rows', lambda *a, **kw: pytest.fail('picker must stay closed'))
    with pytest.raises(mod.IssueError, match='specify'):
        app.resolve_repo(None)
