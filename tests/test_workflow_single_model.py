"""Single-model preferences are scoped, explicit, and edited transactionally."""
import copy
from types import SimpleNamespace

import pytest

from test_config import Menu
from test_workflow import workflow


@pytest.mark.parametrize('defaults', [[], {'other': {}}, {'codex': []},
    {'codex': {'extra': 'x'}}, {'codex': {'model': '--unsafe'}},
    {'codex': {'model': 'two words'}}, {'codex': {'model': None}},
    {'codex': {'effort': 'auto'}}, {'claude': {'effort': 1}}])
def test_single_model_rejects_invalid_settings(workflow, defaults):
    policy = dict(copy.deepcopy(workflow.DEFAULT), session_defaults=defaults)
    with pytest.raises(ValueError, match='session'):
        workflow.validate_policy(policy)


def test_single_model_keeps_old_policies_and_disabled_stages(workflow):
    original = workflow.defaults()
    assert 'session_defaults' not in workflow.validate(original)['workflows']['standard']
    policy = dict(copy.deepcopy(workflow.DEFAULT), stages_enabled=False,
                  session_defaults={'codex': {'model': 'custom/model', 'effort': 'high'},
                                    'claude': {'effort': 'low'}})
    document = workflow.update(original, policy, '/repo')
    assert workflow.effective(document) == workflow.effective(original)
    assert workflow.effective(document, '/repo') == policy
    assert 'custom/model' in workflow.render(document)
    assert 'Codex session default: custom/model; reasoning: high' in workflow.summary_lines(policy)
    policy['stages_enabled'] = True
    assert 'custom/model' not in workflow.render(workflow.update(original, policy))
    assert policy['planning'] == workflow.DEFAULT['planning']
    policy['enabled'] = False
    assert 'custom/model' not in workflow.render(workflow.update(original, policy))


def test_repository_model_editor_preserves_global_defaults(workflow, t_mod, monkeypatch):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    cfg = SimpleNamespace(repos={'api': '/repo'}, agents={'api': 'codex'}, agent_default='claude',
                          models={'codex': 'global-model'}, efforts={'codex': 'low'})
    t_mod._workflow_session(cfg)
    original = copy.deepcopy(cfg._workflow_document)
    cfg._workflow_session_catalogs = {'codex': [dict(id='repo-model', resolved='repo-model',
        label='repo-model', efforts=['low', 'high'], fast=False, ultrafast=False)]}
    menu = Menu(['stages_enabled', '0', 'main_model', 'repo-model', 'main_effort', 'high', 'back'])
    t_mod._workflow_policy_menu(menu, cfg, vars(workflow), repository='/repo')
    policy = workflow.effective(cfg._workflow_document, '/repo')
    assert not policy['stages_enabled']
    assert policy['session_defaults'] == {'codex': {'model': 'repo-model', 'effort': 'high'}}
    assert cfg.models == {'codex': 'global-model'} and cfg.efforts == {'codex': 'low'}
    assert workflow.effective(cfg._workflow_document) == workflow.effective(original)
    assert not workflow.config_path().exists()
    review = '\n'.join(t_mod._workflow_review(cfg._workflow_document)).split('Repository /repo:', 1)[1]
    assert 'Codex session default: repo-model; reasoning: high' in review
    assert 'Planning:' not in review and 'Implementation:' not in review and 'Review:' not in review
    before = copy.deepcopy(cfg._workflow_document)
    t_mod._workflow_policy_menu(Menu(['main_model', None, 'back']), cfg, vars(workflow), repository='/repo')
    assert cfg._workflow_document == before
    t_mod._workflow_policy_menu(Menu(['stages_enabled', '1', 'back']), cfg, vars(workflow), repository='/repo')
    assert workflow.effective(cfg._workflow_document, '/repo')['session_defaults'] == policy['session_defaults']
    t_mod._workflow_policy_menu(Menu(['stages_enabled', '0', 'main_model', '', 'back']), cfg, vars(workflow), repository='/repo')
    assert 'session_defaults' not in workflow.effective(cfg._workflow_document, '/repo')


def test_development_flow_keeps_section_focus_without_chatter(workflow, t_mod):
    class Inspect(Menu):
        def __init__(self):
            super().__init__(['pull_requests', 'pr_state', 'draft', 'back',
                              'local_preview', 'local_preview', 'before_pr', 'back',
                              'tests', 'test_timing', 'pr_ci', 'back', 'back'])
            self.context, self.views = [], []
        def done(self, label, value):
            self.context.append((label, value))
        def pick(self, label, rows, **kwargs):
            self.views.append((label, rows, kwargs))
            return super().pick(label, rows, **kwargs)
    ui = Inspect()
    policy = copy.deepcopy(workflow.DEFAULT)
    result = t_mod._workflow_behaviors_menu(ui, vars(workflow), policy)
    assert result['behaviors'] == {'pr_state': 'draft', 'local_preview': 'before_pr', 'test_timing': 'pr_ci'}
    assert 'behaviors' not in policy
    first = ui.views[0]
    assert first[0] == 'PRs, previews & tests'
    assert set(first[2]['values'].values()) == {'Existing rules'}
    assert dict(first[1]) == {'pull_requests': 'Pull requests ›', 'local_preview': 'Local preview ›',
                              'frontend_design': 'Frontend design ›', 'tests': 'Tests ›', 'back': 'Back'}
    assert dict(ui.views[2][1])['draft'] == 'Draft'
    assert ui.views[3][2]['default'] == 1
    assert ui.views[8][2]['default'] == 1
    assert ui.views[-1][2]['default'] == 3
    assert ui.context == []
