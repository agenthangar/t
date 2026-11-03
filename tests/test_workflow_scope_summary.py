"""The workflow tool hub distinguishes global defaults from the current repo."""
import copy
from types import SimpleNamespace

import pytest

from test_workflow import workflow  # noqa: F401 -- isolated workflow data


def configured_document(workflow):
    document = workflow.defaults()
    document['workflows']['fast'] = copy.deepcopy(document['workflows']['standard'])
    document['repositories']['/code/t'] = {'workflow': 'fast'}
    return document


def test_hub_summary_distinguishes_global_and_current_repository(t_mod, workflow):
    document = configured_document(workflow)
    before = copy.deepcopy(document)
    summary = t_mod._workflow_hub_scope_summary(vars(workflow), document, 'codex', ('t', '/code/t'))
    assert summary == 'Global: standard · t: fast'
    assert t_mod._workflow_hub_scope_summary(vars(workflow), document, 'codex', None) == 'Global: standard'
    assert document == before


@pytest.mark.parametrize('entry,disabled,expected', [
    (None, False, 'Global: standard · t: Inherit global'),
    ({'excluded': True}, False, 'Global: standard · t: Disabled'),
    ({'workflow': 'fast'}, True, 'Global: standard · t: fast (Disabled)'),
    ('inline', False, 'Global: standard · t: Custom'),
    ('inline', True, 'Global: standard · t: Custom (Disabled)'),
])
def test_hub_summary_reports_inherited_excluded_custom_and_disabled(t_mod, workflow, entry, disabled, expected):
    document = configured_document(workflow)
    if entry is None:
        document['repositories'].clear()
    elif entry == 'inline':
        policy = copy.deepcopy(document['workflows']['standard'])
        policy['enabled'] = not disabled
        document['repositories']['/code/t'] = {'inline': policy}
    else:
        document['repositories']['/code/t'] = entry
        document['workflows']['fast']['enabled'] = not disabled
    before = copy.deepcopy(document)
    assert t_mod._workflow_hub_scope_summary(vars(workflow), document, 'codex', ('t', '/code/t')) == expected
    assert document == before


def test_hub_summary_keeps_tool_catalogues_independent_and_absence_visible(t_mod, workflow):
    document = configured_document(workflow)
    claude = workflow.tool_document(document, 'claude')
    claude['workflows']['quick'] = copy.deepcopy(claude['workflows'][claude['default']])
    claude['default'] = 'quick'
    claude['workflows']['quick']['enabled'] = False
    claude['repositories'] = {}
    document = workflow.with_tool_document(document, 'claude', claude)
    assert t_mod._workflow_hub_scope_summary(vars(workflow), document, 'claude', ('t', '/code/t')) == (
        'Global: quick (Disabled) · t: Inherit global (Disabled)')
    assert t_mod._workflow_hub_scope_summary(vars(workflow), document, 'cursor', ('t', '/code/t'), False) == 'Not configured'
    assert t_mod._workflow_hub_scope_summary(vars(workflow), document, 'codex', ('t', '/code/t')) == 'Global: standard · t: fast'


def test_hub_repository_resolves_numbered_worktree_through_existing_config(t_mod, monkeypatch):
    cfg = t_mod.Config.__new__(t_mod.Config)
    cfg.repos = {'t': '/code/t'}
    cfg.worktree_root = '/code/.worktrees'
    monkeypatch.setattr(t_mod.os, 'getcwd', lambda: '/code/.worktrees/t/t-20/src')
    monkeypatch.setattr(t_mod.subprocess, 'run', lambda *a, **k: pytest.fail('known worktree should not query Git'))
    assert t_mod._workflow_hub_repository(cfg) == ('t', '/code/t')


def test_hub_repository_resolves_other_linked_worktree_with_one_git_read(t_mod, monkeypatch):
    cfg = SimpleNamespace(repos={'t': '/code/t'})
    monkeypatch.setattr(t_mod.os, 'getcwd', lambda: '/scratch/feature/src')
    calls = []
    def git(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=0, stdout='/code/t/.git\n')
    monkeypatch.setattr(t_mod.subprocess, 'run', git)
    assert t_mod._workflow_hub_repository(cfg) == ('t', '/code/t')
    assert len(calls) == 1
    assert calls[0][0] == ['git', '-C', '/scratch/feature/src', 'rev-parse', '--path-format=absolute', '--git-common-dir']
    assert calls[0][1]['timeout'] == 0.5


def test_hub_repository_handles_minimal_fixtures_and_unregistered_cwd(t_mod, monkeypatch):
    assert t_mod._workflow_hub_repository(SimpleNamespace()) is None
    monkeypatch.setattr(t_mod.os, 'getcwd', lambda: '/elsewhere')
    monkeypatch.setattr(t_mod.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=128, stdout=''))
    assert t_mod._workflow_hub_repository(SimpleNamespace(repos={'t': '/code/t'})) is None
    monkeypatch.setattr(t_mod.os, 'getcwd', lambda: '/code/t/src')
    assert t_mod._workflow_hub_repository(SimpleNamespace(repos={'t': '/code/t'})) == ('t', '/code/t')


def test_hub_resolves_context_once_and_only_changes_display(t_mod, workflow, monkeypatch):
    document = configured_document(workflow)
    cfg = SimpleNamespace(repos={'t': '/code/t'}, agent_default='codex',
                          _workflow_document=document, _workflow_initial=copy.deepcopy(document),
                          _workflow_before=('saved', None), _workflow_activate=False)
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    monkeypatch.setattr(t_mod.os, 'getcwd', lambda: '/scratch/feature')
    reads = []
    monkeypatch.setattr(t_mod.subprocess, 'run', lambda *a, **k: reads.append(a) or
                        SimpleNamespace(returncode=0, stdout='/code/t/.git\n'))
    scopes = []
    monkeypatch.setattr(t_mod, '_workflow_tool_scope', lambda ui, cfg, tool, action: scopes.append(tool))
    views = []
    class Menu:
        def pick(self, title, rows, **kwargs):
            views.append(kwargs['values'])
            return 'codex' if len(views) == 1 else 'back'
    before = copy.deepcopy(document)
    assert t_mod._workflow_hub(Menu(), cfg) is None
    assert len(reads) == 1 and scopes == ['codex']
    assert len(views) == 2
    assert all(view['codex'] == 'Global: standard · t: fast' for view in views)
    assert all(view['cursor'] == 'Not configured' for view in views)
    assert document == before
