"""Scoped resets stay in the workflow transaction and restore live defaults."""
import copy

import pytest

from test_workflow import workflow  # noqa: F401
from test_workflow_tool_ui import ToolMenu, config
from test_tui import make_ui
from test_local_inference import local  # noqa: F401


def test_legacy_section_reset_preserves_other_effective_choices(workflow):
    policy = dict(copy.deepcopy(workflow.DEFAULT), behaviors={
        'delivery': 'draft_pull_request', 'preview': 'before_implementation',
        'preview_target': 'cli', 'preview_launch': 'automatic', 'validation': 'full',
        'cli_implement_before_approval': 'yes', 'frontend_design': 'in_app'})
    original = copy.deepcopy(policy)
    after = workflow.reset_behaviors(policy, ['frontend_enabled', 'frontend_browser', 'frontend_implement_before_approval'])
    assert workflow.preview_enabled(after['behaviors'], 'frontend')
    assert workflow.preview_enabled(after['behaviors'], 'cli')
    assert after['behaviors']['cli_enabled'] == 'enabled'
    assert after['behaviors']['preview_launch'] == 'automatic'
    assert after['behaviors']['pr_state'] == 'draft'
    assert after['behaviors']['cli_implement_before_approval'] == 'yes'
    after = workflow.reset_behaviors(after, ['pr_open', 'pr_state', 'pr_merge', 'cli_enabled', 'test_timing'])
    assert not set(after['behaviors']) & {'pr_open', 'pr_state', 'pr_merge', 'validation', 'cli_implement_before_approval'}
    assert after['behaviors']['frontend_design'] == 'in_app'
    assert policy == original
    with pytest.raises(ValueError, match='reset field'):
        workflow.reset_behaviors(policy, ['bogus'])


def test_reset_noop_keeps_legacy_storage_and_global_inheritance(workflow):
    policy = dict(copy.deepcopy(workflow.DEFAULT), behaviors={'preview': 'on_request'})
    assert workflow.reset_behaviors(policy, ['pr_review']) == policy
    custom = workflow.update_behavior(policy, 'cli_preview_timing', 'when_ready')
    reset = workflow.reset_behaviors(custom, ['cli_preview_timing'])
    assert workflow.preview_preferences(reset['behaviors'], 'cli')['timing'] == {'value': 'on_request', 'source': 'inherit'}
    reset = workflow.update_behavior(reset, 'local_preview', 'when_ready')
    assert workflow.preview_preferences(reset['behaviors'], 'cli')['timing']['value'] == 'when_ready'
    assert workflow.reset_behaviors(workflow.DEFAULT, ['pr_review']) == workflow.DEFAULT


@pytest.mark.parametrize('tool', ['codex', 'claude', 'cursor'])
def test_policy_defaults_are_tool_specific_and_do_not_touch_other_scopes(workflow, t_mod, tool):
    cfg = config(workflow)
    cfg._workflow_tool = tool
    cfg._workflow_document = workflow.tool_defaults(tool)
    policy = cfg._workflow_document['workflows']['standard']
    policy.update(enabled=False, stages_enabled=True, instructions='custom', behaviors={'pr_review': 'skip'},
                  inference={'mode': 'automatic'}, runners=copy.deepcopy(workflow.RUNNER_DEFAULT),
                  session_defaults={tool: {'model': 'custom-model'}})
    cfg._workflow_document['repositories']['/repo'] = {'workflow': 'standard', 'design_system': {'path': 'DESIGN.md'}}
    cfg._workflow_document['workflows']['other'] = copy.deepcopy(policy)
    before = copy.deepcopy(cfg._workflow_document)
    helper = t_mod._workflow_tool_helper(vars(workflow), tool)
    ui = ToolMenu(['reset', 'back'])
    t_mod._workflow_policy_menu(ui, cfg, helper, name='standard')
    assert cfg._workflow_document['workflows']['standard'] == workflow.tool_defaults(tool)['workflows']['standard']
    assert cfg._workflow_document['workflows']['other'] == before['workflows']['other']
    assert cfg._workflow_document['repositories'] == before['repositories']
    assert not workflow.config_path().exists()
    assert ui.views[0][2]['sections']['frontend_design'] == 'Behavior'
    assert ui.views[1][1][ui.views[1][2]['default']][0] == 'reset'
    assert ui.views[1][2]['values']['reset'] == 'Already at defaults'


def test_reset_inherited_repo_defaults_does_not_create_override(workflow, t_mod):
    cfg = config(workflow)
    cfg.agents['api'] = 'codex'
    before = copy.deepcopy(cfg._workflow_document)
    t_mod._workflow_policy_menu(ToolMenu(['reset', 'back']), cfg, vars(workflow), repository='/repo')
    assert cfg._workflow_document == before


@pytest.mark.parametrize('scope', ['pull_requests', 'local_preview', 'frontend', 'cli', 'tests', 'all', 'design'])
def test_page_resets_include_hidden_children_but_keep_other_pages(workflow, t_mod, scope):
    policy = dict(copy.deepcopy(workflow.DEFAULT), behaviors={
        'pr_review': 'skip', 'frontend_enabled': 'disabled', 'cli_enabled': 'disabled',
        'frontend_preview_timing': 'on_request', 'cli_preview_timing': 'on_request',
        'cli_planning_approval': 'not_required', 'local_preview': 'on_request',
        'frontend_design': 'in_app', 'design_system': 'off', 'test_timing': 'pr_ci'},
        runners=copy.deepcopy(workflow.RUNNER_DEFAULT), instructions='Keep this')
    after = t_mod._workflow_reset_behaviors(vars(workflow), policy, scope)
    if scope in ('local_preview', 'all'):
        assert not any(k.startswith(('frontend_', 'cli_')) and k != 'frontend_design' for k in after['behaviors'])
    if scope in ('tests', 'all'):
        assert 'runners' not in after and 'test_timing' not in after['behaviors']
    else:
        assert after['runners'] == policy['runners']
    if scope == 'design':
        assert not {'frontend_design', 'design_system'} & after['behaviors'].keys()
    else:
        assert after['behaviors']['frontend_design'] == 'in_app'
    assert after['instructions'] == 'Keep this'
    if scope not in ('design', 'all'):
        actual = t_mod._workflow_behavior_section(ToolMenu(['reset', 'back']), vars(workflow), policy, scope)
        assert actual == after
    elif scope == 'all':
        actual = t_mod._workflow_behaviors_menu(ToolMenu(['reset', 'back']), vars(workflow), policy)
        assert actual == after


def test_binary_selectors_have_default_and_clear_legacy_switch(workflow, t_mod):
    original = dict(copy.deepcopy(workflow.DEFAULT), behaviors={'pr_review': 'skip', 'preview_target': 'cli'})
    ui = ToolMenu(['pr_review', '', 'back'])
    result = t_mod._workflow_behavior_section(ui, vars(workflow), original, 'pull_requests')
    assert ui.views[1][1][0] == ('', 'Use default · Enabled')
    assert 'pr_review' not in result['behaviors']
    ui = ToolMenu(['frontend_enabled', '', 'back'])
    result = t_mod._workflow_behavior_section(ui, vars(workflow), result, 'frontend')
    assert ui.views[1][1][0] == ('', 'Use default · Enabled')
    assert workflow.preview_enabled(result['behaviors'], 'frontend')
    assert workflow.preview_enabled(result['behaviors'], 'cli')


def test_design_reset_retains_repository_source(workflow, t_mod, monkeypatch):
    cfg = config(workflow)
    cfg._workflow_document['repositories']['/repo'] = {'design_system': {'mode': 'off'}}
    policy = dict(copy.deepcopy(workflow.DEFAULT), behaviors={'frontend_design': 'in_app', 'design_system': 'off'})
    after = t_mod._workflow_frontend_design_menu(ToolMenu(['reset', 'back']), cfg, vars(workflow), policy, name='standard')
    assert 'behaviors' not in after
    assert cfg._workflow_document['repositories']['/repo']['design_system'] == {'mode': 'off'}
    monkeypatch.setattr(workflow, 'design_system', lambda: {'discover': lambda p: {'candidates': [], 'warnings': []}, 'workspace': lambda p: p})
    after = t_mod._workflow_design_systems_menu(ToolMenu(['reset', 'back']), cfg, vars(workflow), policy, name='standard')
    assert after['behaviors'] == {'frontend_design': 'in_app'}
    assert cfg._workflow_document['repositories']['/repo']['design_system'] == {'mode': 'off'}


def test_stage_and_runner_resets_are_scoped(workflow, t_mod):
    cfg = config(workflow)
    policy = cfg._workflow_document['workflows']['standard']
    policy['planning']['model'] = 'custom'
    policy['behaviors'] = {'pr_review': 'skip'}
    policy['runners'] = dict(copy.deepcopy(workflow.RUNNER_DEFAULT), timeout_seconds=42)
    policy['model_stages'] = ['planning']
    policy['stages_enabled'] = True
    t_mod._workflow_policy_menu(ToolMenu(['planning', 'reset', 'stages_enabled', ['__default__'], 'back']), cfg, vars(workflow), name='standard')
    after = cfg._workflow_document['workflows']['standard']
    assert after['planning'] == workflow.DEFAULT['planning']
    assert 'model_stages' not in after and 'stages_enabled' not in after
    assert after['behaviors'] == policy['behaviors']
    t_mod._workflow_runners_menu(ToolMenu(['reset', 'back']), cfg, vars(workflow), name='standard')
    assert 'runners' not in cfg._workflow_document['workflows']['standard']
    default = workflow.reset_policy(after, 'inference')
    assert 'inference' not in default
    permission = dict(copy.deepcopy(workflow.DEFAULT), permissions={'codex': 'full', 'claude': 'manual'})
    assert workflow.reset_policy(permission, 'permissions')['permissions'] == {'claude': 'manual'}
    with pytest.raises(ValueError, match='reset scope'):
        workflow.reset_policy(after, 'nope')


@pytest.mark.parametrize('width,height', [(80, 24), (44, 20), (32, 12)])
def test_grouped_workflow_viewport_and_reset_focus(workflow, t_mod, width, height):
    cfg = config(workflow)
    policy = cfg._workflow_document['workflows']['standard']
    policy['stages_enabled'] = False
    ui = make_ui(t_mod, [], width, height)
    pick = ui.pick
    def navigate(label, rows, **kw):
        keys = iter(['j'] * (len(rows) - 1) + ['q'])
        ui.read_key = lambda: next(keys)
        return pick(label, rows, **kw)
    ui.pick = navigate
    t_mod._workflow_policy_menu(ui, cfg, vars(workflow), name='standard')
    for frame in ui.frames:
        plain = [t_mod._ANSI_RE.sub('', line) for line in frame]
        assert sum(t_mod._term_rows(line, width) for line in plain) <= height
        assert any('▸' in line for line in plain)
    text = '\n'.join(t_mod._ANSI_RE.sub('', line) for frame in ui.frames for line in frame)
    assert 'MODELS' in text and 'BEHAVIOR' in text and 'MANAGE' in text


@pytest.mark.parametrize('keys,expected', [(['j','j','j','j','space','enter'], ['default']),
    (['j','j','j','j','space','k','space','enter'], ['review']),
    (['j','j','j','j','space','k','k','k','k','space','enter'], [''])])
def test_multiple_model_default_is_an_exclusive_choice(t_mod, keys, expected):
    ui = make_ui(t_mod, keys)
    rows = [('', 'One model'), ('planning', 'Planning'), ('implementation', 'Implementation'), ('review', 'Review'), ('default', 'Use default')]
    assert ui.pick('Models', rows, multi=True, marked={1, 2}, empty_choice='', exclusive_choices=('default',)) == expected


def test_inference_resets_and_individual_default_choices(local):
    ui = ToolMenu(['reset', 'back'])
    assert local.workflow_menu(ui, dict(local.DEFAULT, mode='automatic', timeout_seconds=900)) == local.DEFAULT
    ui = ToolMenu(['mode', '', 'on_cloud_failure', 'on_cloud_failure', '', 'back', 'context_tokens', '', 'timeout_seconds', '', 'back'])
    assert local.workflow_menu(ui, dict(local.DEFAULT, mode='automatic', context_tokens=8192, timeout_seconds=900, on_cloud_failure='local')) == local.DEFAULT
    ui = ToolMenu(['mode', None, 'context_tokens', None, 'on_cloud_failure', None, 'back'])
    original = dict(local.DEFAULT, mode='automatic')
    assert local.workflow_menu(ui, original) == original


def test_inference_reset_interrupt_preserves_staged_changes(local, workflow, t_mod, monkeypatch):
    cfg = config(workflow)
    cfg._workflow_document['workflows']['standard']['inference'] = dict(local.DEFAULT, mode='automatic')
    monkeypatch.setattr(workflow, 'inference', lambda: vars(local))
    class Interrupted(ToolMenu):
        def pick(self, label, rows, **kwargs):
            if label == 'Inference & routing' and any(view[0] == label for view in self.views):
                raise KeyboardInterrupt
            return super().pick(label, rows, **kwargs)
    with pytest.raises(KeyboardInterrupt):
        t_mod._workflow_policy_menu(Interrupted(['inference', 'reset']), cfg, vars(workflow), name='standard')
    assert 'inference' not in cfg._workflow_document['workflows']['standard']
    assert not workflow.config_path().exists()


def test_local_model_default_is_configuration_only(local, monkeypatch):
    monkeypatch.setattr(local, 'catalogue', lambda *args, **kwargs: {'models': []})
    assert local.select_model(ToolMenu([''])) == {'provider': local.DEFAULT['provider'], 'model': local.DEFAULT_MODEL}


def test_reset_review_shows_changes_and_keeps_transaction_baseline(workflow, t_mod, monkeypatch):
    cfg = config(workflow)
    cfg._workflow_document['workflows']['standard']['behaviors'] = {'pr_review': 'skip', 'pr_merge': 'automatic'}
    cfg._workflow_initial = copy.deepcopy(cfg._workflow_document)
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    t_mod._workflow_policy_menu(ToolMenu(['behaviors', 'pull_requests', 'reset', 'back', 'back', 'back']), cfg, vars(workflow), name='standard')
    assert cfg._workflow_initial['workflows']['standard']['behaviors']['pr_review'] == 'skip'
    review = '\n'.join(t_mod._workflow_review(cfg._workflow_document, cfg._workflow_initial))
    assert 'Agent review before PR' in review and 'Merge pull request' in review
    assert not workflow.config_path().exists()


def test_reset_counts_leaf_settings_and_pluralizes(t_mod):
    assert t_mod._workflow_reset_value({'behaviors': {'a': 1, 'b': 2}}, {}) == '2 overrides'
    assert t_mod._workflow_reset_value({'behaviors': {'a': 1}}, {}) == '1 override'
    assert t_mod._workflow_reset_value({}, {}) == 'Already at defaults'


def test_all_permissions_page_reset_preserves_other_preferences(workflow, t_mod):
    policy = dict(copy.deepcopy(workflow.DEFAULT), permissions={'codex': 'full', 'claude': 'manual'}, behaviors={'pr_review': 'skip'})
    after = t_mod._workflow_permissions_menu(ToolMenu(['reset', 'back']), vars(workflow), policy)
    assert 'permissions' not in after and after['behaviors'] == policy['behaviors']


def test_inference_browsing_interrupt_does_not_materialize_defaults(local, workflow, t_mod, monkeypatch):
    cfg = config(workflow)
    cfg._workflow_document['workflows']['standard']['inference'] = {'mode': 'automatic'}
    before = copy.deepcopy(cfg._workflow_document)
    monkeypatch.setattr(workflow, 'inference', lambda: vars(local))
    monkeypatch.setattr(local, 'select_model', lambda *args: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt) as exc:
        t_mod._workflow_policy_menu(ToolMenu(['inference', 'model']), cfg, vars(workflow), name='standard')
    assert not hasattr(exc.value, 'workflow_inference')
    assert cfg._workflow_document == before
    assert not workflow.config_path().exists()
