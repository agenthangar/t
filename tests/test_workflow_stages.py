"""Independent planning/review helper choices preserve saved workflows."""
import copy

import pytest

from test_workflow import workflow


@pytest.mark.parametrize('selected', [None, 'planning', {}, [], ['implementation'],
                                      ['planning', 'planning'], [False], [[]]])
def test_saved_stage_selection_rejects_invalid_values(workflow, selected):
    policy = copy.deepcopy(workflow.DEFAULT)
    policy['separate_stages'] = selected
    with pytest.raises(ValueError, match='separate_stages'):
        workflow.validate_policy(policy)


@pytest.mark.parametrize('version', [1, 2, 3])
def test_legacy_workflows_keep_both_stages_without_materializing_field(workflow, version):
    policy = copy.deepcopy(workflow.DEFAULT)
    document = ({'version': 1, 'global': policy, 'repositories': {}} if version == 1 else
                {'version': version, 'default': 'standard',
                 'workflows': {'standard': policy}, 'repositories': {}})
    loaded = workflow.validate(document)
    resolved = workflow.effective(loaded)
    assert workflow.separate_stages(resolved) == ['planning', 'review']
    assert 'separate_stages' not in resolved
    assert 'separate_stages' not in policy
    assert loaded['version'] == 3


def test_stage_selection_preserves_noops_saved_models_and_retained_selection(workflow):
    original = copy.deepcopy(workflow.DEFAULT)
    original['planning'] = {'model': 'saved-planner', 'effort': 'ultra'}
    original['review'] = {'model': 'saved-reviewer', 'effort': 'minimal'}
    assert workflow.select_stages(original, ['review', 'planning']) == original
    planning = workflow.select_stages(original, ['planning'])
    assert planning['separate_stages'] == ['planning']
    assert 'stages_enabled' not in planning
    assert workflow.select_stages(planning, ['planning']) == planning
    disabled = workflow.select_stages(planning, [])
    assert workflow.separate_stages(disabled) == []
    assert disabled['separate_stages'] == ['planning']
    assert disabled['planning'] == original['planning']
    assert disabled['review'] == original['review']
    assert workflow.select_stages(disabled, []) == disabled
    restored = workflow.select_stages(disabled, ['planning'])
    assert restored == {**planning, 'stages_enabled': True}
    both = workflow.select_stages(restored, ['review', 'planning'])
    assert workflow.separate_stages(both) == ['planning', 'review']
    assert both['planning'] == original['planning']
    assert both['review'] == original['review']
    assert original == {**workflow.DEFAULT,
                        'planning': {'model': 'saved-planner', 'effort': 'ultra'},
                        'review': {'model': 'saved-reviewer', 'effort': 'minimal'}}


@pytest.mark.parametrize('selected', [None, 'review', ['review', 'review'], ['coding'], [False]])
def test_selection_helper_validates_before_updating(workflow, selected):
    policy = copy.deepcopy(workflow.DEFAULT)
    before = copy.deepcopy(policy)
    with pytest.raises(ValueError, match='separate_stages'):
        workflow.select_stages(policy, selected)
    assert policy == before


@pytest.mark.parametrize('selected', [['planning'], ['review'], ['planning', 'review']])
def test_stage_settings_and_launch_defaults_are_independent(workflow, selected):
    policy = workflow.select_stages(workflow.DEFAULT, selected)
    assert workflow.main_session_settings(policy) == {'model': 'gpt-6.1-sol', 'effort': 'medium'}
    assert workflow.main_session_settings(policy, 'claude') == {}
    for stage in ('planning', 'review'):
        settings = workflow.stage_settings(policy, stage)
        assert settings == policy[stage if stage in selected else 'implementation']
        settings['model'] = 'changed-copy'
        assert policy['implementation']['model'] == 'gpt-6.1-sol'
        assert policy[stage]['model'] != 'changed-copy'
    assert workflow.stage_settings(policy, 'implementation') == policy['implementation']
    with pytest.raises(ValueError, match='unknown workflow stage'):
        workflow.stage_settings(policy, 'other')
    policy['enabled'] = False
    assert workflow.separate_stages(policy) == selected
    assert workflow.main_session_settings(policy) == {}


def test_disabled_stages_keep_single_tool_defaults(workflow):
    policy = workflow.select_stages(workflow.DEFAULT, [])
    policy['session_defaults'] = {'codex': {'model': 'single-tool', 'effort': 'low'},
                                  'claude': {'model': 'other-tool'}}
    assert workflow.main_session_settings(policy) == {'model': 'single-tool', 'effort': 'low'}
    assert workflow.main_session_settings(policy, 'claude') == {'model': 'other-tool'}
    assert workflow.stage_settings(policy, 'planning') == policy['implementation']


def test_named_and_repository_choices_are_scoped_and_read_only(workflow, tmp_path):
    document = workflow.action(workflow.defaults(), 'add', name='review-only')
    policy = workflow.select_stages(workflow.scoped(document, name='review-only'), ['review'])
    document = workflow.update(document, policy, name='review-only')
    repository = str(tmp_path.resolve() / 'repo')
    document = workflow.action(document, 'use', name='review-only', repository=repository)
    original = copy.deepcopy(document)
    assert workflow.separate_stages(workflow.effective(document)) == ['planning', 'review']
    assert workflow.separate_stages(workflow.effective(document, repository)) == ['review']
    summary = workflow.catalogue(document)['workflows']['review-only']
    assert summary['workflow'] == policy
    assert summary['separate_stages'] == ['review']
    assert summary['stages_enabled'] and summary['stages_active']
    assert 'Separate models: Review' in workflow.summary_lines(policy)
    inline = workflow.select_stages(workflow.resolved(document, repository), ['planning'])
    overridden = workflow.update(document, inline, repository=repository)
    assert workflow.separate_stages(workflow.effective(overridden, repository)) == ['planning']
    assert workflow.separate_stages(overridden['workflows']['review-only']) == ['review']
    excluded = workflow.action(document, 'exclude', repository=repository)
    assert not workflow.policy_summary(workflow.effective(excluded, repository))['stages_active']
    workflow.render(document)
    assert document == original
    assert not workflow.config_path().exists()


def test_planning_only_instructions_keep_review_in_main(workflow):
    policy = workflow.select_stages(workflow.DEFAULT, ['planning'])
    policy['review'] = {'model': 'inactive-reviewer', 'effort': 'ultra'}
    text = workflow._stage_text(policy)
    assert 'Plan with a bounded gpt-6-astra sub-agent' in text
    assert 'Review in the main conversation with gpt-6.1-sol' in text
    assert 'Review with a bounded' not in text
    assert 'inactive-reviewer' not in text and 'ultra' not in text
    assert 'independently of its model and reasoning effort' in text
    assert 'do not enforce model selection or switch an active conversation' in text


@pytest.mark.parametrize('main_effort,baseline', [('auto', 'medium'), ('high', 'high')])
def test_review_only_uses_actual_main_planning_effort(workflow, main_effort, baseline):
    policy = workflow.select_stages(workflow.DEFAULT, ['review'])
    policy['planning'] = {'model': 'inactive-planner', 'effort': 'ultra'}
    policy['implementation']['effort'] = main_effort
    text = workflow._stage_text(policy)
    assert 'Plan in the main conversation with gpt-6.1-sol' in text
    assert 'Plan with a bounded' not in text
    assert 'Review with a bounded gpt-6-astra sub-agent' in text
    assert 'inactive-planner' not in text and 'ultra' not in text
    assert 'actual main-conversation planning effort' in text
    assert f'concrete main-session launch default {baseline} as the baseline' in text


def test_disabled_helpers_render_no_inactive_models_but_keep_other_preferences(workflow):
    policy = workflow.select_stages(workflow.DEFAULT, ['review'])
    policy = workflow.select_stages(policy, [])
    policy['planning']['model'] = 'inactive-planner'
    policy['review']['model'] = 'inactive-reviewer'
    policy['instructions'] = 'Keep my editable instructions.'
    text = workflow._stage_text(policy)
    assert 'Model stages are disabled' in text
    assert 'inactive-planner' not in text and 'inactive-reviewer' not in text
    assert 'Keep my editable instructions.' in text
    assert workflow.policy_summary(policy)['separate_stages'] == []
