"""Permissions are explicit per-tool workflow launch defaults."""
import copy

import pytest

from test_workflow import workflow  # noqa: F401 -- isolated account settings


@pytest.mark.parametrize('permissions', [None, [], {'unknown': 'full'}, {'codex': None},
                                        {'codex': 'invalid'}, {'claude': 'full'}, {'codex': []}])
def test_invalid_permission_schema(workflow, permissions):
    policy = {**copy.deepcopy(workflow.DEFAULT), 'permissions': permissions}
    with pytest.raises(ValueError, match='permissions'):
        workflow.validate_policy(policy)


def test_permission_updates_preserve_other_settings_and_unset_defaults(workflow):
    original = workflow.select_stages(workflow.DEFAULT, ['review'])
    assert workflow.update_permission(original, 'codex', '') == original
    assert workflow.update_permission(original, 'claude', None) == original
    selected = workflow.update_permission(original, 'codex', 'auto')
    assert workflow.update_permission(selected, 'codex', 'auto') == selected
    both = workflow.update_permission(selected, 'claude', 'acceptEdits')
    assert both['permissions'] == {'codex': 'auto', 'claude': 'acceptEdits'}
    assert workflow.update_permission(both, 'codex', None)['permissions'] == {'claude': 'acceptEdits'}
    assert workflow.update_permission(selected, 'codex', '') == original
    assert 'permissions' not in original
    assert selected['separate_stages'] == ['review']
    for agent, value in [('other', 'auto'), ('codex', False), ('claude', 'full')]:
        with pytest.raises(ValueError, match='permission preset'):
            workflow.update_permission(original, agent, value)


@pytest.mark.parametrize('stages_enabled', [False, True])
def test_each_permission_preset_is_independent_of_model_stages(workflow, stages_enabled):
    for agent, choices in workflow.PERMISSIONS.items():
        for value, spec in choices.items():
            policy = workflow.update_permission(workflow.DEFAULT, agent, value)
            policy['stages_enabled'] = stages_enabled
            before = copy.deepcopy(policy)
            args = workflow.permission_args(policy, agent)
            assert args == spec['args'] and len(args) % 2 == 0
            args.append('mutated copy')
            assert workflow.permission_args(policy, agent) == spec['args']
            assert agent.title() + ' permissions: ' + spec['label'] in workflow.summary_lines(policy)
            assert workflow.policy_summary(policy)['permissions'] == {agent: value}
            assert policy == before
            policy['enabled'] = False
            assert workflow.permission_args(policy, agent) == []


def test_permission_choices_are_exact_native_pairs(workflow):
    codex = workflow.PERMISSIONS['codex']
    assert codex['ask']['args'] == ['--sandbox', 'workspace-write', '--ask-for-approval',
                                   'on-request', '-c', 'approvals_reviewer="user"']
    assert codex['auto']['args'][-1] == 'approvals_reviewer="auto_review"'
    assert codex['full']['args'] == ['--sandbox', 'danger-full-access', '--ask-for-approval', 'never']
    assert codex['read_only']['args'] == ['--sandbox', 'read-only', '--ask-for-approval', 'never']
    for preset, spec in workflow.PERMISSIONS['claude'].items():
        assert spec['args'] == ['--permission-mode', preset]


def test_unset_permissions_are_read_only_and_never_model_prose(workflow):
    document = workflow.defaults()
    before = copy.deepcopy(document)
    policy = workflow.effective(document)
    assert workflow.permission_args(policy, 'codex') == []
    assert workflow.permission_args(policy, 'claude') == []
    assert workflow.permission_args(None, 'codex') == []
    assert 'Codex permissions: Not set' in workflow.summary_lines(policy)
    selected = workflow.update_permission(policy, 'codex', 'full')
    assert workflow.main_session_settings(selected) == workflow.main_session_settings(policy)
    assert workflow._stage_text(selected) == workflow._stage_text(policy)
    assert workflow.catalogue(document)['workflows']['standard']['permissions'] == {}
    assert document == before and 'permissions' not in policy
    assert not workflow.config_path().exists()
