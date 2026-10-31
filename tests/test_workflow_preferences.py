"""Independent workflow preferences preserve legacy meaning and saved JSON."""
import copy
from pathlib import Path

import pytest

from test_workflow import workflow  # noqa: F401 -- isolated account configuration


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
    assert 'Agent review before PR: Enabled' in empty
    assert 'PRs, previews & tests: no preferences set' not in empty
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


def test_required_ci_preference_preserves_authoritative_rules(workflow):
    policy = workflow.update_behavior(copy.deepcopy(workflow.DEFAULT), 'test_timing', 'pr_ci')
    policy = workflow.update_behavior(policy, 'pr_merge', 'automatic')
    text = workflow._stage_text(policy)
    assert 'required CI' in text and 'branch protections intact' in text
    assert 'repository rules remain authoritative' in text
    assert 'Never bypass required checks or branch protections' in text


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


@pytest.mark.parametrize('value', ['automatic', 'on_request'])
def test_frontend_browser_controls_opening_without_requesting_preview(workflow, value):
    policy = workflow.update_behavior(workflow.DEFAULT, 'frontend_browser', value)
    assert policy['behaviors'] == {'frontend_browser': value}
    text = workflow._stage_text(policy)
    assert 'For frontend previews,' in text
    assert "local preview URL in the user's browser" in text
    assert 'does not itself authorize starting a preview' in text
    assert 'or change selected preview timing, approval or pull request preferences' in text
    assert 'When to preview:' not in text
    assert 'Preview approval before PR:' not in text
    assert 'When to open PR:' not in text
    if value == 'automatic':
        assert 'after the preview server or build is ready' in text
        assert 'Frontend — Open in browser: Override · Automatically' in workflow.summary_lines(policy)
    else:
        assert 'wait for an explicit user request before opening' in text
        assert 'Frontend — Open in browser: Override · Only when I ask' in workflow.summary_lines(policy)
    assert workflow.update_behavior(policy, 'frontend_browser', '') == workflow.DEFAULT


def test_frontend_browser_render_and_summary_preserve_raw_legacy_policy(workflow):
    policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': {
        'local_preview': 'before_pr', 'frontend_preview': 'fast_reload',
        'frontend_browser': 'on_request'}}
    document = workflow.update(workflow.defaults(), policy)
    before = copy.deepcopy(document)
    text = workflow.render(document)
    assert 'Open in browser: For frontend previews, wait for an explicit user request' in text
    assert 'Frontend — Open in browser: Override · Only when I ask' in workflow.summary_lines(policy)
    assert workflow.policy_summary(policy)['behaviors'] == policy['behaviors']
    assert document == before
    assert not workflow.config_path().exists()


@pytest.mark.parametrize('value', ['always', 'never', False, [], None])
def test_invalid_saved_frontend_browser_is_rejected(workflow, value):
    policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': {'frontend_browser': value}}
    with pytest.raises(ValueError, match='behavior'):
        workflow.validate_policy(policy)


@pytest.mark.parametrize('value', ['backend', 'all', False, [], None])
def test_invalid_saved_preview_target_is_rejected(workflow, value):
    policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': {'preview_target': value}}
    with pytest.raises(ValueError, match='behavior'):
        workflow.validate_policy(policy)


@pytest.mark.parametrize('target,expected', [
    (None, (True, True)), ('frontend', (True, False)),
    ('cli', (False, True)), ('both', (True, True)),
])
def test_preview_switches_derive_legacy_defaults_and_override_independently(workflow, target, expected):
    behaviors = {'preview_target': target} if target else {}
    before = copy.deepcopy(behaviors)
    assert tuple(workflow.preview_enabled(behaviors, kind) for kind in ('frontend', 'cli')) == expected
    for index, kind in enumerate(('frontend', 'cli')):
        for value in ('enabled', 'disabled'):
            overridden = {**behaviors, kind + '_enabled': value}
            assert workflow.preview_enabled(overridden, kind) == (value == 'enabled')
            other = ('frontend', 'cli')[1 - index]
            assert workflow.preview_enabled(overridden, other) == expected[1 - index]
    assert behaviors == before


def test_preview_switch_rejects_unknown_kind(workflow):
    with pytest.raises(ValueError, match='preview kind'):
        workflow.preview_enabled({}, 'backend')


def test_toggling_preview_switch_restores_saved_frontend_settings(workflow):
    policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': {
        'frontend_enabled': 'enabled', 'frontend_design': 'in_app',
        'frontend_browser': 'on_request', 'frontend_preview': 'rebuild'}}
    disabled = workflow.update_behavior(policy, 'frontend_enabled', 'disabled')
    assert disabled['behaviors'] == {**policy['behaviors'], 'frontend_enabled': 'disabled'}
    assert 'frontend_preview' not in workflow.visible_behaviors(disabled['behaviors'])
    assert workflow.update_behavior(disabled, 'frontend_enabled', 'enabled') == policy


@pytest.mark.parametrize('kind', ['frontend', 'cli'])
@pytest.mark.parametrize('value', ['automatic', 'never', False, [], None])
def test_invalid_saved_preview_switch_is_rejected(workflow, kind, value):
    policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': {kind + '_enabled': value}}
    with pytest.raises(ValueError, match='behavior'):
        workflow.validate_policy(policy)



@pytest.mark.parametrize('legacy,expected', [
    ({'delivery': 'pull_request'}, {'pr_open': 'automatic', 'pr_state': 'ready', 'pr_merge': 'on_request'}),
    ({'delivery': 'draft_pull_request'}, {'pr_open': 'automatic', 'pr_state': 'draft', 'pr_merge': 'on_request'}),
    ({'delivery': 'auto_merge'}, {'pr_open': 'automatic', 'pr_state': 'ready', 'pr_merge': 'automatic'}),
    ({'delivery': 'review'}, {'pr_open': 'on_request', 'pr_merge': 'on_request', 'delivery': 'review'}),
    ({'delivery': 'local_review'}, {'pr_open': 'automatic', 'pr_merge': 'on_request',
                                  'local_preview': 'when_ready', 'frontend_implement_before_approval': 'no'}),
    ({'local_preview': 'before_pr'}, {'local_preview': 'when_ready', 'frontend_implement_before_approval': 'no'}),
    ({'preview': 'before_implementation'}, {'frontend_implement_before_approval': 'no'}),
    ({'design_approval': 'required'}, {'frontend_implement_before_approval': 'no'}),
    ({'design_approval': 'continue'}, {'frontend_implement_before_approval': 'yes'}),
    ({'preview_approval': 'required'}, {'frontend_implement_before_approval': 'no'}),
    ({'preview_approval': 'not_required'}, {'frontend_implement_before_approval': 'yes'}),
    ({'preview': 'before_implementation', 'design_approval': 'continue'}, {'frontend_implement_before_approval': 'yes'}),
    ({'preview': 'ui_changes'}, {'local_preview': 'when_ready'}),
    ({'preview': 'on_request'}, {'local_preview': 'on_request'}),
])
def test_legacy_preferences_are_derived_without_mutation(workflow, legacy, expected):
    before = copy.deepcopy(legacy)
    assert workflow.effective_behaviors(legacy) == expected
    assert legacy == before


@pytest.mark.parametrize('legacy', [
    {'local_preview': 'before_pr', 'design_approval': 'continue'},
    {'delivery': 'local_review', 'design_approval': 'continue'},
    {'preview': 'before_implementation', 'preview_approval': 'required'},
    {'preview_approval': 'required', 'design_approval': 'continue'},
])
@pytest.mark.parametrize('value', ['yes', 'no'])
def test_explicit_implementation_default_wins_over_all_legacy_gates(workflow, legacy, value):
    source = {**legacy, 'frontend_implement_before_approval': value}
    before = copy.deepcopy(source)
    effective = workflow.effective_behaviors(source)
    assert effective['frontend_implement_before_approval'] == value
    assert 'preview_approval' not in effective and 'design_approval' not in effective
    assert source == before


@pytest.mark.parametrize('legacy', [{'local_preview': 'before_pr'}, {'delivery': 'local_review'}])
def test_real_legacy_preview_gate_precedes_design_continue(workflow, legacy):
    effective = workflow.effective_behaviors({**legacy, 'design_approval': 'continue'})
    assert effective['frontend_implement_before_approval'] == 'no'
    assert workflow.effective_behaviors({**legacy, 'preview_approval': 'not_required'})['frontend_implement_before_approval'] == 'yes'


@pytest.mark.parametrize('timing', ['when_ready', 'on_request'])
def test_approval_before_implementation_allows_preview_work_without_deadlock(workflow, timing):
    policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': {
        'cli_enabled': 'disabled', 'local_preview': timing, 'frontend_implement_before_approval': 'no'}}
    text = workflow._stage_text(policy)
    assert 'Wait for explicit user approval of a lightweight preview before substantial implementation' in text
    assert 'Allow the preview or prototype work needed to obtain approval' in text
    if timing == 'when_ready':
        assert 'Prepare an initial lightweight local preview for approval before substantial implementation' in text
        assert 'When the work is ready to try' not in text
    else:
        assert 'Do not automatically launch a preview; wait for a preview request, then obtain approval' in text
        assert 'Prepare an initial lightweight local preview' not in text
    assert 'Preview approval before PR:' not in text
    assert 'Design approval:' not in text
    assert 'When to open PR:' not in text


@pytest.mark.parametrize('frontend,cli', [('enabled', 'enabled'), ('enabled', 'disabled'),
                                          ('disabled', 'enabled'), ('disabled', 'disabled')])
def test_disabled_preview_kinds_hide_guidance_and_retain_defaults_and_overrides(workflow, frontend, cli):
    behaviors = {'frontend_enabled': frontend, 'cli_enabled': cli,
                 'local_preview': 'when_ready',
                 'frontend_preview_timing': 'on_request', 'frontend_implement_before_approval': 'no',
                 'cli_preview_timing': 'on_request', 'cli_implement_before_approval': 'no',
                 'frontend_design': 'in_app', 'frontend_preview': 'fast_reload',
                 'frontend_browser': 'automatic', 'cli_preview': 'install',
                 'pr_open': 'on_request', 'test_timing': 'pr_ci'}
    policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': behaviors}
    before = copy.deepcopy(policy)
    visible = workflow.visible_behaviors(behaviors)
    text = workflow._stage_text(policy)
    for kind, state in [('frontend', frontend), ('cli', cli)]:
        scope = 'For frontend changes —' if kind == 'frontend' else 'For command-line tool changes —'
        assert (scope in text) == (state == 'enabled')
        for key in workflow.PREVIEW_OVERRIDES[kind].values():
            assert (key in visible) == (state == 'enabled' and key in behaviors)
    assert ('frontend_browser' in visible) == (frontend == 'enabled')
    assert ('cli_preview' in visible) == (cli == 'enabled')
    assert 'When to open PR:' in text and 'When to run tests:' in text
    assert workflow.policy_summary(policy)['behaviors'] == behaviors
    assert policy == before
    reenabled = workflow.update_behavior(policy, 'frontend_enabled', 'enabled')
    assert reenabled['behaviors'] == {**behaviors, 'frontend_enabled': 'enabled'}


@pytest.mark.parametrize('legacy', [
    {'design_approval': 'required'}, {'design_approval': 'continue'},
    {'preview': 'before_implementation', 'design_approval': 'continue'},
    {'preview_approval': 'required'}, {'local_preview': 'before_pr'},
    {'delivery': 'local_review'},
])
def test_read_only_legacy_bytes_and_clear_do_not_resurrect_approval_aliases(workflow, legacy):
    policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': legacy}
    document = workflow.update(workflow.defaults(), policy)
    before = copy.deepcopy(document)
    raw = workflow.encode(document)
    text = workflow.render(document)
    summary = workflow.summary_lines(policy)
    assert 'Preview approval before PR:' not in text and 'Design approval:' not in text
    assert not any('Preview approval before PR:' in line or 'Design approval:' in line for line in summary)
    assert workflow.policy_summary(policy)['behaviors'] == legacy
    assert workflow.encode(document) == raw and document == before
    assert not workflow.config_path().exists()
    cleared = workflow.update_behavior(policy, 'frontend_implement_before_approval', '')
    effective = workflow.effective_behaviors(cleared.get('behaviors', {}))
    assert 'frontend_implement_before_approval' not in effective
    assert 'preview_approval' not in effective and 'design_approval' not in effective
    assert 'Implement before approval:' not in workflow._stage_text(cleared)
    changed = workflow.update_behavior(policy, 'frontend_implement_before_approval', 'yes')
    assert changed['behaviors']['frontend_implement_before_approval'] == 'yes'
    assert 'preview_approval' not in changed['behaviors'] and 'design_approval' not in changed['behaviors']


@pytest.mark.parametrize('key', [
    'frontend_implement_before_approval', 'frontend_preview_timing', 'cli_preview_timing',
])
def test_preview_default_and_override_values_validate_without_saving(workflow, key):
    for value in workflow.BEHAVIORS[key]['choices']:
        policy = workflow.update_behavior(workflow.DEFAULT, key, value)
        assert policy['behaviors'] == {key: value}
        assert workflow.validate_policy(policy) == policy
        assert workflow.update_behavior(policy, key, '') == workflow.DEFAULT
    for value in ('inherit', 'invalid', False, None):
        policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': {key: value}}
        with pytest.raises(ValueError, match='behavior'):
            workflow.validate_policy(policy)


def test_behavior_catalogue_accepts_documented_choices_and_rejects_unknown_values(workflow):
    for key, spec in workflow.BEHAVIORS.items():
        for value in spec['choices']:
            policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': {key: value}}
            assert workflow.validate_policy(policy) == policy
            before = copy.deepcopy(policy)
            workflow._stage_text(policy)
            workflow.summary_lines(policy)
            assert policy == before
        invalid = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': {key: 'unknown-choice'}}
        with pytest.raises(ValueError, match='behavior'):
            workflow.validate_policy(invalid)


def test_frontend_browser_keeps_resolved_preview_and_pr_rules(workflow):
    policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': {
        'local_preview': 'when_ready',
        'frontend_preview_timing': 'on_request', 'frontend_implement_before_approval': 'no',
        'pr_open': 'on_request', 'pr_merge': 'on_request'}}
    for value in ('automatic', 'on_request'):
        after = workflow.update_behavior(policy, 'frontend_browser', value)
        assert after['behaviors'] == {**policy['behaviors'], 'frontend_browser': value}
        assert workflow.preview_preferences(after['behaviors'], 'frontend') == {
            'enabled': True, 'timing': {'value': 'on_request', 'source': 'override'},
            'approval': {'value': None, 'source': 'inherit'},
            'launch': {'value': value, 'source': 'override'}}
        text = workflow._stage_text(after)
        assert 'wait for an explicit user request or approval before pushing' in text
        assert 'Wait for an explicit user request or approval before merging' in text
        assert workflow.update_behavior(after, 'frontend_browser', '') == policy


@pytest.mark.parametrize('stages_enabled,selected', [
    (True, ['planning', 'implementation', 'review']),
    (True, ['implementation']),
    (False, ['planning', 'implementation', 'review']),
])
def test_skip_pr_review_removes_reviewer_requirement_and_retains_delivery(workflow, stages_enabled, selected):
    policy = {**copy.deepcopy(workflow.DEFAULT), 'stages_enabled': stages_enabled,
              'model_stages': selected, 'behaviors': {
                  'pr_review': 'skip', 'pr_open': 'on_request', 'pr_state': 'draft',
                  'pr_merge': 'on_request', 'test_timing': 'pr_ci',
                  'local_preview': 'on_request', 'frontend_implement_before_approval': 'no'}}
    before = copy.deepcopy(policy)
    text = workflow._stage_text(policy)
    assert 'Review with a bounded' not in text
    assert 'Review in the main conversation with' not in text
    assert 'Review the actual diff and validation against the plan' not in text
    assert 'Skip the pre-PR agent review pass' in text
    assert 'Run neither a main-session review pass nor a review worker solely from this workflow' in text
    assert 'including explicit review requirements' in text
    assert 'required external approvals' in text
    assert 'PR timing, state and merge preferences' in text
    assert 'main conversation still owns correctness, selected tests and previews, fixes and delivery' in text
    assert 'When to open PR:' in text and 'Pull request state:' in text and 'Merge pull request:' in text
    assert 'When to run tests:' in text
    assert 'wait for a preview request, then obtain approval before substantial implementation' in text
    if stages_enabled:
        assert '3. Finish delivery in the main conversation' in text
        assert 'merge conflicts and required CI through completion' in text
    assert 'Agent review before PR: Disabled' in workflow.summary_lines(policy)
    assert workflow.policy_summary(policy)['review_skipped'] is True
    assert policy == before


@pytest.mark.parametrize('stages_enabled,selected,worker', [
    (True, ['review'], True), (True, ['implementation'], False),
    (False, ['review'], False),
])
def test_explicit_pr_review_uses_selected_worker_or_main_agent(workflow, stages_enabled, selected, worker):
    policy = {**copy.deepcopy(workflow.DEFAULT), 'stages_enabled': stages_enabled,
              'model_stages': selected, 'behaviors': {'pr_review': 'review'}}
    text = workflow._stage_text(policy)
    assert 'Before opening a pull request, review the actual diff and validation results and address findings' in text
    if worker:
        assert 'Review with a bounded ' + policy['review']['model'] in text
    elif stages_enabled:
        assert 'Review in the main conversation with' in text
    else:
        assert 'otherwise review in the main conversation' in text
        assert 'Review with a bounded' not in text
    assert workflow.pr_review_skipped(policy) is False
    assert 'Agent review before PR: Enabled' in workflow.summary_lines(policy)


def test_clearing_skip_restores_saved_review_stage_and_its_model_settings(workflow):
    policy = {**copy.deepcopy(workflow.DEFAULT), 'model_stages': ['planning', 'review']}
    before = copy.deepcopy(policy)
    normal = workflow._stage_text(policy)
    assert 'Review with a bounded ' + policy['review']['model'] in normal
    skipped = workflow.update_behavior(policy, 'pr_review', 'skip')
    assert skipped['review'] == policy['review']
    assert skipped['model_stages'] == policy['model_stages']
    assert workflow.policy_summary(skipped)['model_stages'] == ['planning', 'review']
    assert 'Review stage: skipped before PR; saved model settings retained' in workflow.summary_lines(skipped)
    assert workflow.update_behavior(skipped, 'pr_review', '') == policy
    assert workflow._stage_text(workflow.update_behavior(skipped, 'pr_review', '')) == normal
    reviewing = workflow.update_behavior(skipped, 'pr_review', 'review')
    assert reviewing['review'] == policy['review']
    assert 'Review with a bounded ' + policy['review']['model'] in workflow._stage_text(reviewing)
    assert policy == before


def test_disabled_workflow_suppresses_pr_review_choice(workflow):
    policy = {**copy.deepcopy(workflow.DEFAULT), 'enabled': False,
              'behaviors': {'pr_review': 'skip'}}
    assert workflow.pr_review_skipped(policy) is False
    assert workflow.policy_summary(policy)['review_skipped'] is False
    assert 'Agent review before PR:' not in workflow._stage_text(policy)


@pytest.mark.parametrize('value', ['always', 'never', 'on_request', False, None])
def test_invalid_saved_pr_review_choice_is_rejected(workflow, value):
    policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': {'pr_review': value}}
    with pytest.raises(ValueError, match='behavior'):
        workflow.validate_policy(policy)


@pytest.mark.parametrize('method', ['install', 'run'])
@pytest.mark.parametrize('launch', ['automatic', 'on_request'])
def test_cli_launch_composes_with_method_and_preserves_other_preferences(workflow, method, launch):
    behaviors = {'cli_preview': method, 'local_preview': 'when_ready',
                 'frontend_implement_before_approval': 'no', 'pr_open': 'on_request',
                 'pr_merge': 'on_request', 'test_timing': 'pr_ci'}
    policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': behaviors}
    before = copy.deepcopy(policy)
    after = workflow.update_behavior(policy, 'cli_launch', launch)
    assert after['behaviors'] == {**behaviors, 'cli_launch': launch}
    text = workflow._stage_text(after)
    assert 'Run command: For command-line tool previews,' in text
    assert ('configured preview method' if launch == 'on_request' else 'configured method') in text
    assert 'Launch only the supported preview command, never arbitrary production or destructive commands' in text
    assert 'does not itself request a preview or change PR or test preferences' in text
    assert 'For an interactive CLI, use a supported usable interactive terminal when available' in text
    assert 'report the limitation and exact command without claiming it ran' in text
    assert 'Wait for explicit user approval of a lightweight preview before substantial implementation' in text
    assert 'When to open PR:' in text and 'Merge pull request:' in text and 'When to run tests:' in text
    if launch == 'automatic':
        assert 'once the selected CLI preview has been prepared, launch its repository-supported preview command' in text
        assert 'CLI — Run command: Override · Automatically' in workflow.summary_lines(after)
    else:
        assert 'wait for an explicit user request before launching' in text
        assert 'CLI — Run command: Override · Only when I ask' in workflow.summary_lines(after)
        if method == 'run':
            assert 'prepare the repository-supported development command' in text
            assert 'run the repository-supported development command directly' not in text
        else:
            assert 'Preparing the development installation does not launch its preview command' in text
    assert workflow.update_behavior(after, 'cli_launch', '') == policy
    assert policy == before


def test_automatic_cli_launch_respects_request_only_child_timing(workflow):
    behaviors = {'local_preview': 'when_ready', 'cli_preview_timing': 'on_request',
                 'cli_preview': 'run', 'cli_launch': 'automatic'}
    policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': behaviors}
    assert workflow.preview_preferences(behaviors, 'cli')['timing'] == {
        'value': 'on_request', 'source': 'override'}
    text = workflow._stage_text(policy)
    assert 'For command-line tool changes — When to preview: Prepare a local preview only when the user requests it' in text
    assert 'do not start a request-only preview before the user requests it' in text
    assert 'CLI preview method: For command-line tool previews,' in text
    assert behaviors['cli_preview_timing'] == 'on_request'


@pytest.mark.parametrize('launch', ['automatic', 'on_request'])
def test_disabled_cli_hides_launch_guidance_without_deleting_setting(workflow, launch):
    behaviors = {'cli_enabled': 'disabled', 'cli_launch': launch,
                 'cli_preview': 'run', 'local_preview': 'on_request'}
    policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': behaviors}
    before = copy.deepcopy(policy)
    assert 'cli_launch' not in workflow.visible_behaviors(behaviors)
    assert 'For command-line tool changes — Run command:' not in workflow._stage_text(policy)
    assert not any(line.startswith('CLI — Run command:') for line in workflow.summary_lines(policy))
    assert workflow.policy_summary(policy)['behaviors'] == behaviors
    enabled = workflow.update_behavior(policy, 'cli_enabled', 'enabled')
    assert enabled['behaviors'] == {**behaviors, 'cli_enabled': 'enabled'}
    assert 'For command-line tool changes — Run command:' in workflow._stage_text(enabled)
    assert policy == before


def test_cli_launch_is_optional_independent_and_read_only(workflow):
    policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': {'cli_preview': 'run'}}
    baseline = workflow._stage_text(policy)
    assert 'For command-line tool changes — Run command:' not in baseline
    selected = workflow.update_behavior(policy, 'cli_launch', 'on_request')
    document = workflow.update(workflow.defaults(), selected)
    before = workflow.encode(document)
    workflow.render(document)
    workflow.summary_lines(selected)
    assert workflow.encode(document) == before
    assert not workflow.config_path().exists()
    cleared_method = workflow.update_behavior(selected, 'cli_preview', '')
    assert cleared_method['behaviors'] == {'cli_launch': 'on_request'}
    assert workflow.update_behavior(selected, 'cli_launch', '') == policy
    assert workflow._stage_text(workflow.update_behavior(selected, 'cli_launch', '')) == baseline


@pytest.mark.parametrize('value', ['always', 'never', 'run', False, None])
def test_invalid_saved_cli_launch_choice_is_rejected(workflow, value):
    policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': {'cli_launch': value}}
    with pytest.raises(ValueError, match='behavior'):
        workflow.validate_policy(policy)



@pytest.mark.parametrize('kind', ['frontend', 'cli'])
def test_preview_inherits_live_defaults_and_clear_removes_only_override(workflow, kind):
    policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': {'local_preview': 'when_ready'}}
    child = workflow.PREVIEW_OVERRIDES[kind]['timing']
    assert workflow.PREVIEW_DEFAULTS == {'timing': 'local_preview', 'approval': 'preview_pr_approval', 'launch': 'preview_launch'}
    assert workflow.preview_preferences(policy['behaviors'], kind) == {
        'enabled': True, 'timing': {'value': 'when_ready', 'source': 'inherit'},
        'approval': {'value': None, 'source': 'inherit'}, 'launch': {'value': None, 'source': 'inherit'}}
    overridden = workflow.update_behavior(policy, child, 'on_request')
    changed = workflow.update_behavior(overridden, 'local_preview', 'on_request')
    assert workflow.preview_preferences(changed['behaviors'], kind)['timing'] == {
        'value': 'on_request', 'source': 'override'}
    changed = workflow.update_behavior(changed, 'local_preview', 'when_ready')
    assert workflow.preview_preferences(changed['behaviors'], kind)['timing']['value'] == 'on_request'
    assert workflow.update_behavior(changed, child, '') == policy
    assert workflow.preview_preferences({}, kind) == {
        'enabled': True, 'timing': {'value': None, 'source': 'inherit'},
        'approval': {'value': None, 'source': 'inherit'}, 'launch': {'value': None, 'source': 'inherit'}}


@pytest.mark.parametrize('legacy', [
    {'implement_before_approval': 'no'}, {'preview_approval': 'required'},
    {'preview': 'before_implementation'}, {'design_approval': 'required'},
    {'implement_before_approval': 'no', 'cli_implement_before_approval': 'no'},
])
def test_legacy_implementation_approval_applies_only_to_frontend(workflow, legacy):
    policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': {**legacy, 'local_preview': 'when_ready'}}
    before = copy.deepcopy(policy)
    effective = workflow.effective_behaviors(policy['behaviors'])
    assert effective['frontend_implement_before_approval'] == 'no'
    assert 'implement_before_approval' not in effective
    assert 'cli_implement_before_approval' not in effective
    text = workflow._stage_text(policy)
    cli = [line for line in text.splitlines() if line.startswith('For command-line tool changes —')]
    frontend = [line for line in text.splitlines() if line.startswith('For frontend changes —')]
    assert len(cli) == 1 and 'When the work is ready to try' in cli[0]
    assert 'approval' not in cli[0] and 'prototype' not in cli[0]
    assert len(frontend) == 2 and 'initial lightweight local preview' in frontend[0]
    assert 'Wait for explicit user approval' in frontend[1]
    assert policy == before
    cleared = workflow.update_behavior(policy, 'frontend_implement_before_approval', '')
    assert 'frontend_implement_before_approval' not in workflow.effective_behaviors(cleared.get('behaviors', {}))
    assert 'Implement before approval:' not in workflow._stage_text(cleared)


@pytest.mark.parametrize('value', ['yes', 'no'])
def test_direct_frontend_gate_wins_over_shared_legacy_and_has_no_inheritance(workflow, value):
    source = {'implement_before_approval': 'no' if value == 'yes' else 'yes',
              'frontend_implement_before_approval': value, 'cli_implement_before_approval': 'no'}
    effective = workflow.effective_behaviors(source)
    assert effective == {'frontend_implement_before_approval': value}
    policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': source}
    summary = workflow.summary_lines(policy)
    assert 'Frontend — Implement before approval: ' + value.title() in summary
    assert not any('CLI — Implement before approval:' in line or
                   'Local preview · Global — Implement before approval:' in line for line in summary)
    assert not any('Implement before approval: Inherit' in line for line in summary)
    assert workflow.BEHAVIORS['frontend_implement_before_approval']['label'] == 'Implement before approval'


def test_retired_cli_gate_remains_raw_and_inert_on_unrelated_edits(workflow):
    policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': {
        'cli_implement_before_approval': 'no', 'cli_enabled': 'enabled', 'cli_launch': 'automatic'}}
    before = copy.deepcopy(policy)
    text = workflow._stage_text(policy)
    assert 'Implement before approval:' not in text
    assert 'approval' not in workflow.BEHAVIORS['cli_enabled']['choices']['enabled'][1]
    for value in ('automatic', 'on_request'):
        assert 'approval' not in workflow.BEHAVIORS['cli_launch']['choices'][value][1]
    changed = workflow.update_behavior(policy, 'cli_preview', 'run')
    assert changed['behaviors']['cli_implement_before_approval'] == 'no'
    assert workflow.policy_summary(policy)['behaviors'] == policy['behaviors']
    assert policy == before


@pytest.mark.parametrize('kind', ['frontend', 'cli'])
@pytest.mark.parametrize('setting,parent,initial,override,changed', [
    ('approval', 'preview_pr_approval', 'required', 'not_required', 'not_required'),
    ('launch', 'preview_launch', 'automatic', 'on_request', 'on_request'),
])
def test_preview_common_defaults_have_live_independent_child_overrides(workflow, kind, setting, parent, initial, override, changed):
    policy = workflow.update_behavior(workflow.DEFAULT, parent, initial)
    child = workflow.PREVIEW_OVERRIDES[kind][setting]
    assert workflow.preview_preferences(policy['behaviors'], kind)[setting] == {'value': initial, 'source': 'inherit'}
    selected = workflow.update_behavior(policy, child, override)
    assert workflow.preview_preferences(selected['behaviors'], kind)[setting] == {'value': override, 'source': 'override'}
    other = 'frontend' if kind == 'cli' else 'cli'
    assert workflow.preview_preferences(selected['behaviors'], other)[setting] == {'value': initial, 'source': 'inherit'}
    revised = workflow.update_behavior(selected, parent, changed)
    assert revised['behaviors'][child] == override
    assert workflow.update_behavior(revised, child, '') == workflow.update_behavior(policy, parent, changed)
    assert workflow.preview_preferences({}, kind)[setting] == {'value': None, 'source': 'inherit'}


def test_shared_pr_gate_is_distinct_from_legacy_implementation_gate(workflow):
    policy = {**workflow.DEFAULT, 'behaviors': {'preview_approval': 'required',
        'preview_pr_approval': 'not_required', 'cli_preview_pr_approval': 'required'}}
    raw = copy.deepcopy(policy)
    resolved = workflow.effective_behaviors(policy['behaviors'])
    assert resolved['frontend_implement_before_approval'] == 'no'
    assert workflow.preview_preferences(resolved, 'frontend')['approval']['value'] == 'not_required'
    assert workflow.preview_preferences(resolved, 'cli')['approval']['value'] == 'required'
    text = workflow._stage_text(policy)
    assert 'For frontend changes — Implement before approval: Wait for explicit user approval' in text
    assert 'For frontend changes — Approval before PR: Do not add a preview approval gate' in text
    assert 'For command-line tool changes — Approval before PR: Before pushing' in text
    assert 'For command-line tool changes — Implement before approval:' not in text
    assert policy == raw


@pytest.mark.parametrize('kind', ['frontend', 'cli'])
def test_common_preview_launch_respects_request_only_timing(workflow, kind):
    behaviors = {'local_preview': 'on_request', 'preview_launch': 'automatic',
                 'preview_pr_approval': 'required'}
    policy = {**workflow.DEFAULT, 'behaviors': behaviors}
    raw = copy.deepcopy(policy)
    scope = 'For frontend changes' if kind == 'frontend' else 'For command-line tool changes'
    text = workflow._stage_text(policy)
    launch_label = 'Open in browser' if kind == 'frontend' else 'Run command'
    assert scope + ' — ' + launch_label + ':' in text
    assert 'do not prepare, open or run a preview until the user requests it' in text
    assert scope + ' — Approval before PR:' in text
    assert policy == raw
    disabled = workflow.update_behavior(policy, kind + '_enabled', 'disabled')
    text = workflow._stage_text(disabled)
    assert scope + ' — ' + launch_label + ':' not in text
    assert scope + ' — Approval before PR:' not in text


def test_inherited_cli_launch_does_not_contradict_run_method(workflow):
    policy = {**workflow.DEFAULT, 'behaviors': {'preview_launch': 'on_request', 'cli_preview': 'run'}}
    text = workflow._stage_text(policy)
    assert 'prepare the repository-supported development command' in text
    assert 'run the repository-supported development command directly' not in text
    assert 'Wait for an explicit user request before launching it' in text


def test_new_common_preview_preferences_remain_read_only_and_do_not_request_preview(workflow):
    policy = {**workflow.DEFAULT, 'behaviors': {'preview_launch': 'automatic', 'preview_pr_approval': 'not_required'}}
    before = copy.deepcopy(policy)
    text = workflow._stage_text(policy)
    assert ' — When to preview:' not in text
    assert 'does not itself authorize starting a preview' in text
    assert workflow.policy_summary(policy)['behaviors'] == before['behaviors']
    assert workflow.summary_lines(policy)
    assert policy == before
    assert not workflow.config_path().exists()


def test_test_timing_uses_when_and_ci_labels(workflow):
    assert workflow.BEHAVIORS['test_timing']['label'] == 'When to run tests'
    assert workflow.BEHAVIORS['test_timing']['choices']['pr_ci'][0] == 'On PR CI'


@pytest.mark.parametrize('key', ['preview_pr_approval', 'preview_launch',
                               'frontend_preview_pr_approval', 'cli_preview_pr_approval'])
def test_shared_preview_preferences_reject_invalid_choices(workflow, key):
    with pytest.raises(ValueError, match='behavior'):
        workflow.validate_policy({**workflow.DEFAULT, 'behaviors': {key: 'invalid'}})


def test_common_preview_launch_and_approval_both_disabled_are_inactive(workflow):
    policy = {**workflow.DEFAULT, 'behaviors': {'frontend_enabled': 'disabled', 'cli_enabled': 'disabled',
        'preview_pr_approval': 'required', 'preview_launch': 'automatic', 'local_preview': 'when_ready'}}
    text = workflow._stage_text(policy)
    assert ' — When to preview:' not in text
    assert ' — Open in browser:' not in text and ' — Run command:' not in text
    assert ' — Approval before PR:' not in text
    assert policy['behaviors']['preview_pr_approval'] == 'required'
    assert policy['behaviors']['preview_launch'] == 'automatic'


def test_preview_summaries_name_global_inheritance_and_actual_child_actions(workflow):
    lines = workflow.preview_summary_lines({'preview_launch': 'automatic', 'preview_pr_approval': 'required'})
    assert 'Local preview · Global — Open/run preview: Automatically' in lines
    assert 'Local preview · Global — Approval before PR: Wait for my approval' in lines
    assert 'Frontend — Open in browser: Inherit global · Automatically' in lines
    assert 'CLI — Run command: Inherit global · Automatically' in lines
    assert 'CLI — Approval before PR: Inherit global · Wait for my approval' in lines
    assert workflow.BEHAVIORS['preview_approval']['label'] == 'Preview approval before PR'


@pytest.mark.parametrize('stages_enabled', [True, False])
def test_pr_review_defaults_to_enabled_without_materializing_storage(workflow, stages_enabled):
    policy = {**copy.deepcopy(workflow.DEFAULT), 'stages_enabled': stages_enabled}
    before = copy.deepcopy(policy)
    assert workflow.pr_review_value(policy) == 'review'
    assert 'Agent review before PR: Enabled' in workflow.summary_lines(policy)
    assert 'Before opening a pull request, review the actual diff and validation results' in workflow._stage_text(policy)
    assert policy == before
    changed = workflow.update_behavior(policy, 'pr_open', 'on_request')
    assert changed['behaviors'] == {'pr_open': 'on_request'}
    assert workflow.BEHAVIORS['pr_review']['choices']['review'][0] == 'Enabled'
    assert workflow.BEHAVIORS['pr_review']['choices']['skip'][0] == 'Disabled'
