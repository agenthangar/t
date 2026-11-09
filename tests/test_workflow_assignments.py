"""Assign repositories from a named workflow without leaving its transaction."""
import copy

import pytest

from test_workflow import workflow  # noqa: F401
from test_workflow_tool_ui import ToolMenu, config
from test_tui import make_ui


@pytest.fixture
def assignment_cfg(workflow, tmp_path):
    cfg = config(workflow)
    cfg.repos = {name: str(tmp_path / name) for name in ('assigned', 'global', 'custom', 'excluded', 'other')}
    doc = workflow.action(workflow.defaults(), 'add', 'fast')
    doc['repositories'] = {
        cfg.repos['assigned']: {'workflow': 'fast'},
        cfg.repos['global']: {'design_system': {'path': 'DESIGN.md'}},
        cfg.repos['custom']: {'inline': copy.deepcopy(workflow.DEFAULT), 'design_system': {'path': 'ui/README.md'}},
        cfg.repos['excluded']: {'workflow': 'fast', 'excluded': True},
        cfg.repos['other']: {'workflow': 'standard'},
    }
    cfg._workflow_document = doc
    cfg._workflow_initial = copy.deepcopy(doc)
    cfg.agents = {}
    return cfg


def test_assignment_summary_distinguishes_sources_and_disabled_policies(workflow, t_mod, assignment_cfg):
    cfg = assignment_cfg
    rows = {item['alias']: item for item in t_mod._workflow_assignment_rows(cfg, vars(workflow), 'fast')}
    assert {alias for alias, item in rows.items() if item['using']} == {'assigned'}
    assert rows['assigned']['label'] == 'Assigned · fast'
    assert rows['global']['label'] == 'Inherit global · standard'
    assert rows['custom']['label'] == 'Custom workflow'
    assert rows['excluded']['label'] == 'Workflows disabled'
    cfg._workflow_document['workflows']['fast']['enabled'] = False
    rows = t_mod._workflow_assignment_rows(cfg, vars(workflow), 'fast')
    assert rows[0]['using'] and rows[0]['label'] == 'Assigned · fast · Workflow disabled'
    cfg._workflow_document['repositories'][cfg.repos['custom']]['inline']['enabled'] = False
    assert next(item for item in t_mod._workflow_assignment_rows(cfg, vars(workflow), 'fast') if item['alias'] == 'custom')['label'] == 'Custom workflow · Workflow disabled'
    assert cfg._workflow_initial['workflows']['fast']['enabled']


def test_batch_assignment_keeps_sources_and_unrelated_settings(workflow, t_mod, assignment_cfg, monkeypatch):
    cfg = assignment_cfg
    before = copy.deepcopy(cfg._workflow_document)
    ui = ToolMenu(['repositories', 'assign', ['repo:global', 'repo:custom', 'repo:excluded'], 'back',
                   'behaviors', 'pull_requests', 'pr_review', 'skip', 'back', 'back', 'back'])
    t_mod._workflow_policy_menu(ui, cfg, vars(workflow), name='fast')
    after = cfg._workflow_document
    for alias in ('global', 'custom', 'excluded'):
        entry = after['repositories'][cfg.repos[alias]]
        assert entry['workflow'] == 'fast' and 'excluded' not in entry and 'inline' not in entry
        if 'design_system' in before['repositories'][cfg.repos[alias]]:
            assert entry['design_system'] == before['repositories'][cfg.repos[alias]]['design_system']
    assert after['repositories'][cfg.repos['other']] == before['repositories'][cfg.repos['other']]
    assert after['workflows']['standard'] == before['workflows']['standard']
    assert after['workflows']['fast']['behaviors']['pr_review'] == 'skip'
    assert cfg._workflow_initial == before
    main = next(view for view in ui.views[1:] if view[0] == 'Workflow · fast')
    assert main[1][main[2]['default']][0] == 'repositories'
    assert main[2]['values']['repositories'] == '4 using this workflow'
    batch = next(view for view in ui.views if view[0] == 'Assign repositories to fast')
    assert set(dict(batch[1])) == {'repo:global', 'repo:custom', 'repo:excluded', 'repo:other'}
    assert batch[2]['multi'] and not batch[2]['marked']
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    review = '\n'.join(t_mod._workflow_review(after, before))
    assert 'fast' in review and 'Repository' in review
    assert not workflow.config_path().exists()


@pytest.mark.parametrize('selected', [None, []])
def test_assignment_cancel_or_empty_keeps_exact_document(workflow, t_mod, assignment_cfg, selected):
    cfg = assignment_cfg
    before = copy.deepcopy(cfg._workflow_document)
    t_mod._workflow_repositories_menu(ToolMenu(['assign', selected, 'back']), cfg, vars(workflow), 'fast')
    assert cfg._workflow_document == before


def test_global_workflow_includes_inherited_repositories(workflow, t_mod, assignment_cfg):
    cfg = assignment_cfg
    ui = ToolMenu(['back'])
    t_mod._workflow_repositories_menu(ui, cfg, vars(workflow), 'standard')
    rows = t_mod._workflow_assignment_rows(cfg, vars(workflow), 'standard')
    assert {item['alias'] for item in rows if item['using']} == {'global', 'other'}
    assert ui.views[0][2]['sections']['repo:global'] == 'Using this workflow'
    assert ui.views[0][2]['sections']['repo:assigned'] == 'Other repositories'


@pytest.mark.parametrize('action,expected', [('use', {'workflow': 'fast'}), ('inherit', {}),
                                           ('disable', {'excluded': True})])
def test_repository_assignment_actions_keep_design_source(workflow, t_mod, assignment_cfg, action, expected):
    cfg = assignment_cfg
    path = cfg.repos['global']
    t_mod._workflow_assign_repository_menu(ToolMenu([action]), cfg, vars(workflow), 'fast', 'global', path)
    assert cfg._workflow_document['repositories'][path] == dict(expected, design_system={'path': 'DESIGN.md'})


def test_choose_another_workflow_and_source_access(workflow, t_mod, assignment_cfg, monkeypatch):
    cfg = assignment_cfg
    called = []
    monkeypatch.setattr(t_mod, '_workflow_design_system_menu', lambda *args, **kw: called.append(args[3:]))
    ui = ToolMenu(['choose', 'name:standard', 'design_system', 'back'])
    t_mod._workflow_assign_repository_menu(ui, cfg, vars(workflow), 'fast', 'assigned', cfg.repos['assigned'])
    assert cfg._workflow_document['repositories'][cfg.repos['assigned']]['workflow'] == 'standard'
    assert called == [('assigned', cfg.repos['assigned'])]
    assert ui.views[-1][1][ui.views[-1][2]['default']][0] == 'design_system'


@pytest.mark.parametrize('empty', [True, False])
def test_no_available_repositories_never_opens_empty_picker(workflow, t_mod, assignment_cfg, empty):
    cfg = assignment_cfg
    if empty:
        cfg.repos = {}
    else:
        for path in cfg.repos.values():
            cfg._workflow_document = workflow.action(cfg._workflow_document, 'use', 'fast', path)
    before = copy.deepcopy(cfg._workflow_document)
    ui = ToolMenu(['assign', 'back'])
    t_mod._workflow_repositories_menu(ui, cfg, vars(workflow), 'fast')
    assert all(view[0] == 'Workflow · fast · Repositories' for view in ui.views)
    assert 'assign' in ui.views[0][2]['disabled']
    assert ui.views[0][2]['values']['assign'] == ('No registered repositories' if empty else 'All assigned')
    assert cfg._workflow_document == before


def test_assignment_batch_is_atomic_before_staging(workflow, t_mod, assignment_cfg):
    cfg = assignment_cfg
    before = copy.deepcopy(cfg._workflow_document)
    helper = dict(vars(workflow))
    calls = []
    def action(document, verb, name, path):
        calls.append(path)
        if len(calls) == 2:
            raise ValueError('changed assignment')
        return workflow.action(document, verb, name, path)
    helper['action'] = action
    with pytest.raises(ValueError, match='changed assignment'):
        t_mod._workflow_repositories_menu(ToolMenu(['assign', ['repo:custom', 'repo:global']]), cfg, helper, 'fast')
    assert cfg._workflow_document == before


def test_interruption_keeps_completed_assignment_without_publishing(workflow, t_mod, assignment_cfg):
    cfg = assignment_cfg
    class Interrupted(ToolMenu):
        def pick(self, label, rows, **kwargs):
            if label == 'Workflow · fast · Repositories' and any(view[0] == 'Assign repositories to fast' for view in self.views):
                raise KeyboardInterrupt
            return super().pick(label, rows, **kwargs)
    with pytest.raises(KeyboardInterrupt):
        t_mod._workflow_policy_menu(Interrupted(['repositories', 'assign', ['repo:other']]), cfg, vars(workflow), name='fast')
    assert cfg._workflow_document['repositories'][cfg.repos['other']]['workflow'] == 'fast'
    assert cfg._workflow_initial['repositories'][cfg.repos['other']]['workflow'] == 'standard'
    assert not workflow.config_path().exists()


@pytest.mark.parametrize('tool', ['codex', 'claude', 'cursor'])
def test_assignment_changes_only_selected_tool(workflow, t_mod, assignment_cfg, tool):
    cfg = assignment_cfg
    document = workflow.defaults()
    doc = workflow.action(workflow.tool_document(document, tool), 'add', 'fast')
    cfg._workflow_document = workflow.with_tool_document(document, tool, doc)
    cfg._workflow_initial = copy.deepcopy(cfg._workflow_document)
    t_mod._workflow_tool_scope(ToolMenu([]), cfg, tool, lambda scoped:
        t_mod._workflow_repositories_menu(ToolMenu(['assign', ['repo:global'], 'back']), scoped,
                                         t_mod._workflow_tool_helper(vars(workflow), tool), 'fast'))
    assert workflow.tool_document(cfg._workflow_document, tool)['repositories'][cfg.repos['global']]['workflow'] == 'fast'
    for other in workflow.TOOLS:
        if other != tool:
            assert workflow.tool_document(cfg._workflow_document, other) == workflow.tool_document(cfg._workflow_initial, other)


@pytest.mark.parametrize('width,height', [(80, 24), (44, 20), (32, 12)])
def test_assignment_picker_keyboard_and_narrow_viewport(workflow, t_mod, assignment_cfg, width, height):
    cfg = assignment_cfg
    ui = make_ui(t_mod, ['enter', 'space', 'j', 'space', 'enter', 'q'], width, height)
    t_mod._workflow_repositories_menu(ui, cfg, vars(workflow), 'fast')
    # Available order: custom, excluded, global, other. Space advances focus.
    assert cfg._workflow_document['repositories'][cfg.repos['custom']]['workflow'] == 'fast'
    assert cfg._workflow_document['repositories'][cfg.repos['global']]['workflow'] == 'fast'
    for frame in ui.frames:
        assert sum(t_mod._term_rows(line, width) for line in frame) <= height
        assert any('▸' in t_mod._ANSI_RE.sub('', line) for line in frame)
