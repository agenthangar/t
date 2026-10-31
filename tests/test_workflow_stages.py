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
        assert settings == (policy[stage] if stage in selected else workflow.main_session_settings(policy))
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
    assert workflow.stage_settings(policy, 'planning') == {'model': 'single-tool', 'effort': 'low'}


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


@pytest.mark.parametrize('main_effort', ['auto', 'high'])
def test_review_uses_actual_previous_implementation_effort(workflow, main_effort):
    policy = workflow.select_stages(workflow.DEFAULT, ['review'])
    policy['planning'] = {'model': 'inactive-planner', 'effort': 'ultra'}
    policy['implementation']['effort'] = main_effort
    text = workflow._stage_text(policy)
    assert 'Plan in the main conversation with gpt-6.1-sol' in text
    assert 'Plan with a bounded' not in text
    assert 'Review with a bounded gpt-6-astra sub-agent' in text
    assert 'inactive-planner' not in text and 'ultra' not in text
    if main_effort == 'auto':
        assert "previous implementation worker's actual concrete reasoning effort" in text
        assert 'Require that evidence' in text
        assert 'concrete main-session launch default medium as the baseline' not in text
    else:
        assert 'actual main-conversation implementation effort' in text
        assert 'concrete main-session launch default high as the baseline' in text


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


@pytest.mark.parametrize('selected', [None, 'planning', {}, [], ['other'],
                                      ['implementation', 'implementation'], [False], [[]]])
def test_model_stage_schema_rejects_invalid_selections(workflow, selected):
    policy = {**copy.deepcopy(workflow.DEFAULT), 'model_stages': selected}
    with pytest.raises(ValueError, match='model_stages'):
        workflow.validate_policy(policy)


@pytest.mark.parametrize('selected', [['implementation'], ['planning'], ['review'],
                                      ['planning', 'implementation', 'review']])
def test_all_three_model_stages_can_be_selected_independently(workflow, selected):
    policy = workflow.select_model_stages(workflow.DEFAULT, selected)
    assert workflow.model_stages(policy) == selected
    assert workflow.select_model_stages(policy, list(reversed(selected))) == policy
    assert workflow.separate_stages(policy) == [stage for stage in selected if stage != 'implementation']
    disabled = workflow.select_model_stages(policy, [])
    assert workflow.model_stages(disabled) == []
    assert disabled.get('model_stages') == policy.get('model_stages')
    assert disabled['implementation'] == policy['implementation']
    assert workflow.select_model_stages(disabled, []) == disabled
    assert workflow.model_stages(workflow.select_model_stages(disabled, selected)) == selected
    assert workflow.policy_summary(policy)['model_stages'] == selected


def test_legacy_stage_selection_preserves_main_and_noop_json(workflow):
    policy = workflow.select_stages(workflow.DEFAULT, ['review'])
    before = copy.deepcopy(policy)
    assert workflow.model_stages(policy) == ['implementation', 'review']
    assert workflow.select_model_stages(policy, ['review', 'implementation']) == policy
    assert workflow.validate_policy(policy) == before
    changed = workflow.select_model_stages(policy, ['review'])
    assert changed['model_stages'] == ['review']
    assert 'separate_stages' not in changed
    compat = workflow.select_stages(changed, ['planning'])
    assert compat['model_stages'] == ['planning', 'implementation']
    assert 'separate_stages' not in compat
    assert policy == before


def test_main_work_unselected_uses_each_tools_own_launch_defaults(workflow):
    policy = workflow.select_model_stages(workflow.DEFAULT, ['planning'])
    policy['session_defaults'] = {'codex': {'model': 'main-codex', 'effort': 'low'},
                                  'claude': {'model': 'main-claude', 'effort': 'high'}}
    assert workflow.main_session_settings(policy) == {'model': 'main-codex', 'effort': 'low'}
    assert workflow.main_session_settings(policy, 'claude') == {'model': 'main-claude', 'effort': 'high'}
    assert workflow.stage_settings(policy, 'implementation') == {'model': 'main-codex', 'effort': 'low'}
    assert workflow.stage_settings(policy, 'review') == {'model': 'main-codex', 'effort': 'low'}
    assert workflow.stage_settings(policy, 'planning') == policy['planning']
    text = workflow._stage_text(policy)
    assert 'Implement in the main conversation with main-codex' in text
    assert 'Review in the main conversation with main-codex' in text
    assert 'gpt-6.1-sol' not in text
    assert 'Codex session default: main-codex; reasoning: low' in workflow.summary_lines(policy)


def test_main_work_unselected_native_defaults_do_not_reveal_inactive_values(workflow):
    policy = workflow.select_model_stages(workflow.DEFAULT, ['review'])
    policy['planning'] = {'model': 'inactive-planner', 'effort': 'ultra'}
    policy['implementation'] = {'model': 'inactive-implementation', 'effort': 'max'}
    assert workflow.main_session_settings(policy) == {}
    assert workflow.main_session_settings(policy, 'claude') == {}
    assert workflow.stage_settings(policy, 'planning') == {'model': '', 'effort': ''}
    assert workflow.stage_settings(policy, 'implementation') == {'model': '', 'effort': ''}
    text = workflow._stage_text(policy)
    assert 'the current native model' in text
    assert 'the current native reasoning setting' in text
    assert 'resolve a concrete supported main-session baseline before delegating' in text
    assert 'inactive-planner' not in text and 'inactive-implementation' not in text
    assert 'ultra' not in text and ' max' not in text


def test_implementation_only_does_not_delegate_planning_or_review(workflow):
    policy = workflow.select_model_stages(workflow.DEFAULT, ['implementation'])
    policy['planning']['model'] = 'inactive-planner'
    policy['review']['model'] = 'inactive-reviewer'
    text = workflow._stage_text(policy)
    assert 'Plan in the main conversation with gpt-6.1-sol' in text
    assert 'Review in the main conversation with gpt-6.1-sol' in text
    assert 'Plan with a bounded' not in text and 'Review with a bounded' not in text
    assert 'inactive-planner' not in text and 'inactive-reviewer' not in text


def test_model_stage_selector_rejects_invalid_edits(workflow):
    for selected in (None, 'planning', ['other'], ['planning', 'planning'], [False]):
        with pytest.raises(ValueError, match='model_stages'):
            workflow.select_model_stages(workflow.DEFAULT, selected)


def test_worker_effort_is_distinct_from_inherited_main_effort(workflow):
    policy = workflow.select_model_stages(workflow.DEFAULT, ['implementation'])
    assert workflow.stage_settings(policy, 'implementation') == policy['implementation']
    assert workflow.stage_settings(policy, 'implementation')['effort'] == 'auto'
    for stage in ('planning', 'review'):
        assert workflow.stage_settings(policy, stage) == workflow.main_session_settings(policy)
        assert workflow.stage_settings(policy, stage)['effort'] == 'medium'
    assert workflow.main_session_settings(policy)['effort'] == 'medium'


def test_review_supports_task_selected_effort_without_changing_relative_default(workflow):
    assert workflow.DEFAULT['review']['effort'] == 'one-level-lower'
    policy = workflow.select_model_stages(workflow.DEFAULT, ['review'])
    policy['review']['effort'] = 'auto'
    assert workflow.validate_policy(policy) == policy
    assert workflow.stage_settings(policy, 'review')['effort'] == 'auto'
    text = workflow._stage_text(policy)
    assert 'Review with a bounded gpt-6-astra sub-agent' in text
    assert 'low for straightforward reviews, medium for ordinary changes' in text
    assert 'high or higher for complex changes or substantial risk' in text
    assert 'Choose a concrete supported effort before delegation' in text
    assert 'one supported reasoning-effort level below' not in text
    unselected = workflow.select_model_stages(policy, ['implementation'])
    text = workflow._stage_text(unselected)
    assert 'Review in the main conversation with gpt-6.1-sol' in text
    assert 'low for straightforward reviews' not in text


def test_task_selected_implementation_uses_worker_with_fixed_main_session(workflow):
    policy = workflow.select_model_stages(workflow.DEFAULT, ['implementation'])
    before = copy.deepcopy(policy)
    assert workflow.implementation_worker(policy)
    assert workflow.implementation_worker({**policy, 'enabled': False})
    text = workflow._stage_text(policy)
    assert 'Delegate implementation to a bounded gpt-6.1-sol worker' in text
    assert 'Choose a concrete supported effort before spawning the worker' in text
    assert 'actionable plan' in text and 'acceptance criteria' in text
    assert 'return its changes and validation evidence' in text
    assert 'without repeating routine implementation' in text
    assert 'the main session keeps its fixed launch effort medium' in text
    assert 'disclose the exact limitation' in text
    assert 'never pretend the main session\'s effort changed' in text
    assert 'Plan in the main conversation with gpt-6.1-sol; reasoning effort: medium.' in text
    assert 'Review in the main conversation with gpt-6.1-sol; reasoning effort: medium.' in text
    assert 'Main session: gpt-6.1-sol · Medium' in workflow.summary_lines(policy)
    assert workflow.main_session_settings(policy) == {'model': 'gpt-6.1-sol', 'effort': 'medium'}
    assert policy == before


def test_fixed_or_unselected_implementation_does_not_delegate_worker(workflow):
    policy = workflow.select_model_stages(workflow.DEFAULT, ['implementation'])
    policy['implementation']['effort'] = 'high'
    assert not workflow.implementation_worker(policy)
    text = workflow._stage_text(policy)
    assert 'Implement in the main conversation with gpt-6.1-sol; reasoning effort: high.' in text
    assert 'Delegate implementation' not in text
    assert workflow.main_session_settings(policy)['effort'] == 'high'
    policy['implementation']['effort'] = 'auto'
    policy = workflow.select_model_stages(policy, ['planning'])
    policy['session_defaults'] = {'codex': {'model': 'coordinator', 'effort': 'low'}}
    assert not workflow.implementation_worker(policy)
    text = workflow._stage_text(policy)
    assert 'Implement in the main conversation with coordinator; reasoning effort: low.' in text
    assert 'Delegate implementation' not in text
    disabled = workflow.select_model_stages(policy, [])
    assert not workflow.implementation_worker(disabled)
    assert disabled['implementation']['effort'] == 'auto'


def test_relative_implementation_worker_uses_actual_planning_handoff(workflow):
    policy = workflow.select_model_stages(workflow.DEFAULT, ['planning', 'implementation', 'review'])
    policy['implementation']['effort'] = 'one-level-lower'
    assert workflow.validate_policy(policy) == policy
    assert workflow.implementation_worker(policy)
    assert workflow.main_session_settings(policy) == {'model': 'gpt-6.1-sol', 'effort': 'medium'}
    text = workflow._stage_text(policy)
    assert 'Delegate implementation to a bounded gpt-6.1-sol worker' in text
    assert 'one supported reasoning-effort level below the previous planning stage' in text
    assert "previous planning worker's actual concrete reasoning effort" in text
    assert "previous implementation worker's actual concrete reasoning effort" in text
    assert 'carried in its handoff' in text
    assert 'Require its handoff to include the actual model and concrete reasoning effort used' in text
    assert 'Include the actual model and concrete reasoning effort used in its handoff' in text
    assert 'if those settings cannot be verified, report that limitation explicitly' in text
    assert 'report the missing effort and obtain it before delegating' in text
    assert 'Do not substitute saved stage preferences, symbolic effort choices' in text
    assert 'concrete main-session launch default medium as the baseline' not in text
    assert 'clamping to its supported minimum' in text
    assert 'baseline is already at or below that minimum' in text
    assert 'Choose a concrete supported effort before delegation' in text
    assert 'Main session: gpt-6.1-sol · Medium' in workflow.summary_lines(policy)


def test_relative_implementation_uses_main_planning_when_planner_unselected(workflow):
    policy = workflow.select_model_stages(workflow.DEFAULT, ['implementation'])
    policy['implementation']['effort'] = 'one-level-lower'
    policy['planning'] = {'model': 'inactive-planner', 'effort': 'ultra'}
    text = workflow._stage_text(policy)
    assert 'actual main-conversation planning effort' in text
    assert 'concrete main-session launch default medium as the baseline' in text
    assert 'inactive-planner' not in text and 'ultra' not in text
    assert "previous planning worker's" not in text


def test_relative_review_uses_unselected_implementation_native_unknown_honestly(workflow):
    policy = workflow.select_model_stages(workflow.DEFAULT, ['review'])
    policy['implementation'] = {'model': 'inactive-implementation', 'effort': 'ultra'}
    text = workflow._stage_text(policy)
    assert 'one supported reasoning-effort level below the previous implementation stage' in text
    assert 'actual main-conversation implementation effort' in text
    assert 'If the native effort is unknown, disclose that limitation' in text
    assert 'resolve a concrete supported main-session baseline before delegating; do not guess' in text
    assert 'inactive-implementation' not in text and 'ultra' not in text
    assert 'launch default medium as the baseline' not in text
    assert "previous implementation worker's" not in text


def test_relative_planning_effort_remains_invalid(workflow):
    policy = copy.deepcopy(workflow.DEFAULT)
    policy['planning']['effort'] = 'one-level-lower'
    with pytest.raises(ValueError, match='planning effort'):
        workflow.validate_policy(policy)
