"""Independent workflow preferences preserve legacy meaning and saved JSON."""
import copy
from pathlib import Path

import pytest

from test_workflow import workflow  # noqa: F401 -- isolated account configuration


@pytest.mark.parametrize('legacy,expected', [
    ({'delivery': 'pull_request'}, {'pr_open': 'automatic', 'pr_state': 'ready', 'pr_merge': 'on_request'}),
    ({'delivery': 'draft_pull_request'}, {'pr_open': 'automatic', 'pr_state': 'draft', 'pr_merge': 'on_request'}),
    ({'delivery': 'auto_merge'}, {'pr_open': 'automatic', 'pr_state': 'ready', 'pr_merge': 'automatic'}),
    ({'delivery': 'review'}, {'pr_open': 'on_request', 'pr_merge': 'on_request', 'delivery': 'review'}),
    ({'delivery': 'ask_before_pr'}, {'pr_open': 'on_request', 'pr_merge': 'on_request'}),
    ({'delivery': 'local_review'}, {'pr_open': 'automatic', 'pr_merge': 'on_request',
                                    'local_preview': 'when_ready', 'preview_approval': 'required'}),
    ({'local_preview': 'before_pr'}, {'local_preview': 'when_ready', 'preview_approval': 'required'}),
    ({'preview': 'ui_changes'}, {'local_preview': 'when_ready'}),
    ({'preview': 'before_implementation'}, {'design_approval': 'required'}),
    ({'preview': 'on_request'}, {'local_preview': 'on_request'}),
])
def test_legacy_preferences_are_derived_without_mutation(workflow, legacy, expected):
    before = copy.deepcopy(legacy)
    assert workflow.effective_behaviors(legacy) == expected
    assert legacy == before


def test_explicit_preferences_override_legacy_choices(workflow):
    source = {'delivery': 'draft_pull_request', 'preview': 'before_implementation',
              'pr_state': 'ready', 'pr_merge': 'automatic', 'design_approval': 'continue',
              'frontend_design': 'in_app', 'validation': 'full', 'test_timing': 'pr_ci'}
    effective = workflow.effective_behaviors(source)
    assert effective == {'pr_open': 'automatic', 'pr_state': 'ready', 'pr_merge': 'automatic',
                         'design_approval': 'continue', 'frontend_design': 'in_app', 'test_timing': 'pr_ci'}
    assert source['delivery'] == 'draft_pull_request' and source['validation'] == 'full'


def test_preview_before_pr_survives_legacy_ui_preview(workflow):
    assert workflow.effective_behaviors({'preview': 'ui_changes', 'delivery': 'local_review'}) == {
        'pr_open': 'automatic', 'pr_merge': 'on_request', 'local_preview': 'when_ready',
        'preview_approval': 'required'}


def test_design_approval_does_not_assume_prototype_location(workflow):
    effective = workflow.effective_behaviors({'preview': 'before_implementation'})
    assert effective == {'design_approval': 'required'}
    policy = dict(copy.deepcopy(workflow.DEFAULT), behaviors={'preview': 'before_implementation'})
    text = workflow._stage_text(policy)
    assert 'design preview before substantial implementation' in text
    assert 'wait for user feedback and approval' in text
    assert 'standalone HTML' not in text


def test_first_edit_materializes_choices_and_keeps_unrelated_preferences(workflow):
    policy = dict(copy.deepcopy(workflow.DEFAULT), behaviors={
        'delivery': 'draft_pull_request', 'preview': 'ui_changes', 'worktree': 'isolated', 'validation': 'full'})
    before = copy.deepcopy(policy)
    after = workflow.update_behavior(policy, 'cli_preview', 'install')
    assert policy == before
    assert after['behaviors'] == {
        'pr_open': 'automatic', 'pr_state': 'draft', 'pr_merge': 'on_request',
        'local_preview': 'when_ready', 'worktree': 'isolated', 'validation': 'full', 'cli_preview': 'install'}
    timed = workflow.update_behavior(after, 'test_timing', 'before_preview')
    assert timed['behaviors']['test_timing'] == 'before_preview'
    assert 'validation' not in timed['behaviors']
    assert 'delivery' not in timed['behaviors'] and 'preview' not in timed['behaviors']


def test_legacy_leave_uncommitted_survives_unrelated_edits(workflow):
    policy = dict(copy.deepcopy(workflow.DEFAULT), behaviors={'delivery': 'review'})
    after = workflow.update_behavior(policy, 'frontend_preview', 'fast_reload')
    assert after['behaviors']['delivery'] == 'review'
    assert workflow.update_behavior(after, 'pr_open', 'on_request')['behaviors']['delivery'] == 'review'
    text = workflow._stage_text(after)
    assert 'Leave changes uncommitted for user review' in text
    automatic = workflow.update_behavior(after, 'pr_open', 'automatic')
    assert 'delivery' not in automatic['behaviors']
    assert 'Leave changes uncommitted' not in workflow._stage_text(automatic)


def test_clearing_pr_open_removes_legacy_combined_restriction(workflow):
    policy = dict(copy.deepcopy(workflow.DEFAULT), behaviors={'delivery': 'review'})
    after = workflow.update_behavior(policy, 'pr_open', '')
    assert after['behaviors'] == {'pr_merge': 'on_request'}
    assert 'Leave changes uncommitted' not in workflow._stage_text(after)


def test_clearing_optional_preference_does_not_save_a_default(workflow):
    policy = copy.deepcopy(workflow.DEFAULT)
    assert workflow.update_behavior(policy, 'pr_state', None) == policy
    assert workflow.update_behavior(policy, 'pr_state', '') == policy
    after = workflow.update_behavior(policy, 'pr_state', 'draft')
    assert workflow.update_behavior(after, 'pr_state', '') == policy


def test_summary_only_shows_selected_effective_preferences(workflow):
    policy = copy.deepcopy(workflow.DEFAULT)
    empty = workflow.summary_lines(policy)
    assert 'PRs, previews & tests: no preferences set' in empty
    assert not any('No preference' in line for line in empty)
    selected = workflow.summary_lines(workflow.update_behavior(policy, 'pr_state', 'draft'))
    assert 'Pull request state: Draft' in selected
    assert not any(line.startswith('Local preview:') for line in selected)


@pytest.mark.parametrize('key,value', [
    ('unknown', 'anything'), ('pr_open', 'sometimes'), ('test_timing', 'never'), ('pr_state', False),
])
def test_invalid_behavior_edit_is_rejected(workflow, key, value):
    with pytest.raises(ValueError, match='behavior choice'):
        workflow.update_behavior(copy.deepcopy(workflow.DEFAULT), key, value)


@pytest.mark.parametrize('key', [
    'pr_open', 'pr_state', 'pr_merge', 'preview_target', 'local_preview', 'preview_approval', 'cli_preview', 'frontend_design',
    'design_approval', 'frontend_preview', 'test_timing',
])
def test_each_new_preference_validates_and_renders_as_instructions(workflow, key):
    for value, (label, prose) in workflow.BEHAVIORS[key]['choices'].items():
        policy = workflow.update_behavior(copy.deepcopy(workflow.DEFAULT), key, value)
        assert workflow.validate_policy(policy) == policy
        if key == 'local_preview' and value == 'before_pr':
            assert policy['behaviors'] == {'local_preview': 'when_ready', 'preview_approval': 'required'}
            continue  # Legacy combined values render as independent preferences.
        assert prose in workflow._stage_text(policy)
        assert workflow.BEHAVIORS[key]['label'] + ': ' + label in workflow.summary_lines(policy)
    policy['behaviors'][key] = 'invalid-value'
    with pytest.raises(ValueError, match='behavior'):
        workflow.validate_policy(policy)


def test_render_uses_effective_choices_but_summary_and_storage_keep_raw_json(workflow):
    source = {'delivery': 'draft_pull_request', 'preview': 'ui_changes', 'validation': 'full', 'test_timing': 'pr_ci'}
    policy = dict(copy.deepcopy(workflow.DEFAULT), behaviors=source)
    document = workflow.update(workflow.defaults(), policy)
    original = copy.deepcopy(document)
    text = workflow.render(document)
    assert workflow.BEHAVIORS['pr_state']['choices']['draft'][1] in text
    assert workflow.BEHAVIORS['local_preview']['choices']['when_ready'][1] in text
    assert workflow.BEHAVIORS['test_timing']['choices']['pr_ci'][1] in text
    assert workflow.BEHAVIORS['delivery']['choices']['draft_pull_request'][1] not in text
    assert workflow.BEHAVIORS['validation']['choices']['full'][1] not in text
    assert workflow.policy_summary(policy)['behaviors'] == source
    assert document == original
    workflow.save(document, None, propagate=False)
    loaded, _ = workflow.load()
    assert loaded == original
    lines = workflow.summary_lines(policy)
    assert not any(line.startswith(('Delivery:', 'Validation:')) for line in lines)
    assert 'Pull request state: Draft' in lines


def test_required_ci_preference_preserves_authoritative_rules(workflow):
    policy = workflow.update_behavior(copy.deepcopy(workflow.DEFAULT), 'test_timing', 'pr_ci')
    policy = workflow.update_behavior(policy, 'pr_merge', 'automatic')
    text = workflow._stage_text(policy)
    assert 'required CI' in text and 'branch protections intact' in text
    assert 'repository rules remain authoritative' in text
    assert 'Never bypass required checks or branch protections' in text


@pytest.mark.parametrize('legacy', [{'local_preview': 'before_pr'}, {'delivery': 'local_review'}])
def test_explicit_preview_approval_overrides_legacy_gate(workflow, legacy):
    source = {**legacy, 'preview_approval': 'not_required'}
    before = copy.deepcopy(source)
    effective = workflow.effective_behaviors(source)
    assert effective['local_preview'] == 'when_ready'
    assert effective['preview_approval'] == 'not_required'
    assert source == before
    policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': source}
    text = workflow._stage_text(policy)
    assert workflow.BEHAVIORS['preview_approval']['choices']['not_required'][1] in text
    assert workflow.BEHAVIORS['preview_approval']['choices']['required'][1] not in text
    assert workflow.BEHAVIORS['local_preview']['choices']['before_pr'][1] not in text


def test_preview_timing_and_approval_edit_and_clear_independently(workflow):
    policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': {'local_preview': 'before_pr'}}
    after = workflow.update_behavior(policy, 'local_preview', 'on_request')
    assert after['behaviors'] == {'local_preview': 'on_request', 'preview_approval': 'required'}
    after = workflow.update_behavior(after, 'preview_approval', 'not_required')
    assert after['behaviors'] == {'local_preview': 'on_request', 'preview_approval': 'not_required'}
    after = workflow.update_behavior(after, 'local_preview', '')
    assert after['behaviors'] == {'preview_approval': 'not_required'}
    after = workflow.update_behavior(after, 'preview_approval', '')
    assert 'behaviors' not in after
    cleared_gate = workflow.update_behavior(policy, 'preview_approval', '')
    assert cleared_gate['behaviors'] == {'local_preview': 'when_ready'}
    cleared_timing = workflow.update_behavior(policy, 'local_preview', '')
    assert cleared_timing['behaviors'] == {'preview_approval': 'required'}
    assert policy['behaviors'] == {'local_preview': 'before_pr'}


def test_legacy_preview_is_read_only_until_explicit_edit(workflow):
    policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': {'local_preview': 'before_pr'}}
    document = workflow.update(workflow.defaults(), policy)
    before = copy.deepcopy(document)
    text = workflow.render(document)
    summary = workflow.summary_lines(policy)
    assert 'Local preview: Automatically' in summary
    assert 'Preview approval before PR: Wait for my approval' in summary
    assert workflow.BEHAVIORS['preview_approval']['choices']['required'][1] in text
    assert workflow.BEHAVIORS['local_preview']['choices']['before_pr'][1] not in text
    assert workflow.policy_summary(policy)['behaviors'] == {'local_preview': 'before_pr'}
    assert document == before
    assert not workflow.config_path().exists()
    edited = workflow.update_behavior(policy, 'pr_state', 'draft')
    assert edited['behaviors'] == {'local_preview': 'when_ready', 'preview_approval': 'required',
                                   'pr_state': 'draft'}


def test_request_only_preview_with_required_approval_keeps_both_rules(workflow):
    policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': {
        'local_preview': 'on_request', 'preview_approval': 'required'}}
    text = workflow._stage_text(policy)
    assert workflow.BEHAVIORS['local_preview']['choices']['on_request'][1] in text
    assert workflow.BEHAVIORS['preview_approval']['choices']['required'][1] in text
    assert workflow.BEHAVIORS['local_preview']['choices']['when_ready'][1] not in text


def test_empty_instruction_editor_clears_custom_text_without_saving(workflow, monkeypatch):
    document = workflow.defaults()
    document['workflows']['standard']['instructions'] = 'Text to remove.'
    before = copy.deepcopy(document)

    def clear_editor(argv):
        Path(argv[-1]).write_text('')
        return 0

    monkeypatch.setattr(workflow.subprocess, 'call', clear_editor)
    status, after = workflow.edit_instructions(document)
    assert status == 0
    assert workflow.instructions(after) == ''
    assert document == before
    assert not workflow.config_path().exists()


def test_pr_opening_preference_names_timing_and_request_requirement(workflow):
    spec = workflow.BEHAVIORS['pr_open']
    assert spec['label'] == 'When to open PR'
    assert spec['choices']['automatic'][0] == 'Automatically when ready'
    assert spec['choices']['on_request'][0] == 'Only when I ask'
    for value, label in [('automatic', 'Automatically when ready'), ('on_request', 'Only when I ask')]:
        policy = workflow.update_behavior(workflow.DEFAULT, 'pr_open', value)
        assert 'When to open PR: ' + label in workflow.summary_lines(policy)


@pytest.mark.parametrize('target,prefix', [('frontend', 'For frontend changes only: '),
                                          ('cli', 'For command-line tool changes only: ')])
def test_preview_target_conditions_timing_and_approval_gate(workflow, target, prefix):
    policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': {
        'preview_target': target, 'local_preview': 'when_ready', 'preview_approval': 'required'}}
    text = workflow._stage_text(policy)
    for key, value in [('local_preview', 'when_ready'), ('preview_approval', 'required')]:
        label = workflow.BEHAVIORS[key]['label']
        prose = workflow.BEHAVIORS[key]['choices'][value][1]
        assert label + ': ' + prefix + prose in text
        assert label + ': ' + prose not in text
    assert workflow.BEHAVIORS['preview_target']['choices'][target][1] in text
    assert workflow.policy_summary(policy)['behaviors']['preview_target'] == target


@pytest.mark.parametrize('target', [None, 'both'])
def test_legacy_and_both_applicability_preserve_existing_preview_prose(workflow, target):
    behaviors = {'local_preview': 'on_request', 'preview_approval': 'required'}
    if target:
        behaviors['preview_target'] = target
    policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': behaviors}
    before = copy.deepcopy(policy)
    text = workflow._stage_text(policy)
    for key, value in [('local_preview', 'on_request'), ('preview_approval', 'required')]:
        assert workflow.BEHAVIORS[key]['label'] + ': ' + workflow.BEHAVIORS[key]['choices'][value][1] in text
    assert policy == before
    assert ('preview_target' in workflow.effective_behaviors(behaviors)) == bool(target)


@pytest.mark.parametrize('target', ['frontend', 'cli', 'both'])
def test_preview_target_alone_does_not_request_preview_or_approval(workflow, target):
    policy = workflow.update_behavior(workflow.DEFAULT, 'preview_target', target)
    assert policy['behaviors'] == {'preview_target': target}
    text = workflow._stage_text(policy)
    assert 'does not request a preview by itself' in text
    assert 'When to preview:' not in text
    assert 'Preview approval before PR:' not in text
    assert 'wait until the user has reviewed the local preview' not in text


def test_changing_preview_target_preserves_independent_methods_and_design(workflow):
    methods = {'frontend_preview': 'fast_reload', 'cli_preview': 'run',
               'frontend_design': 'standalone_html', 'design_approval': 'required'}
    policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': dict(methods)}
    for target in ('frontend', 'cli', 'both'):
        policy = workflow.update_behavior(policy, 'preview_target', target)
        assert policy['behaviors'] == {**methods, 'preview_target': target}
        text = workflow._stage_text(policy)
        assert 'For frontend previews,' in text
        assert 'For command-line tool previews,' in text
        assert 'For frontend design,' in text
    assert workflow.update_behavior(policy, 'preview_target', '')['behaviors'] == methods
    assert workflow.BEHAVIORS['frontend_design']['label'] == 'Design approach'
    assert workflow.BEHAVIORS['frontend_preview']['label'] == 'Web preview method'
    assert workflow.BEHAVIORS['frontend_preview']['choices']['fast_reload'][0] == 'Fast reload'
    assert workflow.BEHAVIORS['frontend_preview']['choices']['rebuild'][0] == 'Build'


@pytest.mark.parametrize('value', ['backend', 'all', False, [], None])
def test_invalid_saved_preview_target_is_rejected(workflow, value):
    policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': {'preview_target': value}}
    with pytest.raises(ValueError, match='behavior'):
        workflow.validate_policy(policy)


def test_preview_target_read_only_summary_keeps_legacy_raw_values(workflow):
    policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': {
        'local_preview': 'before_pr', 'preview_target': 'frontend'}}
    document = workflow.update(workflow.defaults(), policy)
    before = copy.deepcopy(document)
    text = workflow.render(document)
    assert 'Preview approval before PR: For frontend changes only:' in text
    assert 'Local preview applies to: Frontend' in workflow.summary_lines(policy)
    assert workflow.policy_summary(policy)['behaviors']['local_preview'] == 'before_pr'
    assert document == before
    assert not workflow.config_path().exists()
