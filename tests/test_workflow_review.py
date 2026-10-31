"""Save review compares pending workflow edits with the transaction baseline."""
import copy

import pytest

from test_config import Menu, config_cli  # noqa: F401 -- isolated shell settings
from test_workflow import workflow  # noqa: F401 -- isolated workflow settings


@pytest.fixture
def review(workflow, t_mod, monkeypatch):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    return lambda after, before, **kwargs: t_mod._workflow_review(after, before=before, **kwargs)


def test_workflow_review_stage_toggle_shows_only_changed_workflow(review, workflow):
    before = workflow.action(workflow.defaults(), 'add', 'fast')
    after = copy.deepcopy(before)
    after['workflows']['fast']['stages_enabled'] = False
    assert review(after, before) == ['Codex workflows', '  Workflow fast',
        '    Use multiple models: Planning, Implementation, Review → One model']
    assert before['workflows']['fast'].get('stages_enabled') is None


def test_workflow_review_changed_paths_and_clear_values(review, workflow):
    before = workflow.defaults()
    policy = before['workflows']['standard']
    policy['session_defaults'] = {'codex': {'model': 'old-model', 'effort': 'low'}}
    policy['permissions'] = {'codex': 'ask'}
    policy['behaviors'] = {'pr_open': 'on_request', 'frontend_preview_timing': 'on_request'}
    after = copy.deepcopy(before)
    changed = after['workflows']['standard']
    changed['implementation']['model'] = 'new-model'
    changed['session_defaults']['codex'].pop('effort')
    changed['permissions']['codex'] = 'full'
    changed['behaviors'].pop('frontend_preview_timing')
    changed['behaviors']['pr_open'] = 'automatic'
    text = '\n'.join(review(after, before))
    assert 'Implementation · Model: gpt-6.1-sol → new-model' in text
    assert 'Session defaults · Codex · Effort: Low → Default for this model' in text
    assert 'Permissions · Codex: Ask for approval → Full access' in text
    assert 'Only when I ask → Automatically when ready' in text
    assert '→ Inherit' in text
    assert 'old-model' not in text
    assert workflow.issue_policy() not in text and workflow.PRECEDENCE not in text


def test_workflow_review_text_uses_changed_lines_without_issue_boilerplate(review, workflow):
    before = workflow.defaults()
    before['workflows']['standard']['instructions'] += '\n\nKeep this line.\nOld request.\nAnother unchanged line.'
    after = copy.deepcopy(before)
    after['workflows']['standard']['instructions'] = before['workflows']['standard']['instructions'].replace('Old request.', 'New request.')
    text = '\n'.join(review(after, before))
    assert 'Text instructions:' in text
    assert '-Old request.' in text and '+New request.' in text
    assert 'Keep this line.' not in text and 'Another unchanged line.' not in text
    assert workflow.issue_policy() not in text
    assert before != after


def test_workflow_review_instruction_policy_removal_shows_actual_changed_lines(review, workflow):
    before = workflow.defaults()
    after = copy.deepcopy(before)
    after['workflows']['standard']['instructions'] = ''
    text = '\n'.join(review(after, before))
    assert 'Text instructions:' in text
    for line in workflow.issue_policy().splitlines():
        assert '-' + line in text
    assert 'issue policy changed' not in text


def test_workflow_review_session_default_clears_show_live_inheritance(review, workflow):
    before = workflow.defaults()
    before['workflows']['standard']['session_defaults'] = {
        'codex': {'model': 'chosen-model', 'effort': 'high'}}
    after = copy.deepcopy(before)
    after['workflows']['standard'].pop('session_defaults')
    text = '\n'.join(review(after, before))
    assert 'Session defaults · Codex · Model: chosen-model → Inherit' in text
    assert 'Session defaults · Codex · Effort: High → Inherit' in text
    assert 'Agent default' not in text


def test_workflow_review_review_toggle_missing_value_means_enabled(review, workflow):
    before = workflow.defaults()
    after = copy.deepcopy(before)
    after['workflows']['standard']['behaviors'] = {'pr_review': 'skip'}
    text = '\n'.join(review(after, before))
    assert 'Agent review before PR: Enabled → Disabled' in text
    restored = '\n'.join(review(before, after))
    assert 'Agent review before PR: Disabled → Enabled' in restored
    assert 'Not set' not in text and 'Not set' not in restored


@pytest.mark.parametrize('kind,target,initial', [('frontend', None, 'Enabled'),
    ('cli', None, 'Enabled'), ('frontend', 'cli', 'Disabled'), ('cli', 'frontend', 'Disabled')])
def test_workflow_review_preview_toggle_missing_values_resolve_legacy_defaults(review, workflow, kind, target, initial):
    before = workflow.defaults()
    before['workflows']['standard']['behaviors'] = {'preview_target': target} if target else {}
    after = copy.deepcopy(before)
    after['workflows']['standard']['behaviors'][kind + '_enabled'] = 'disabled' if initial == 'Enabled' else 'enabled'
    other = 'Disabled' if initial == 'Enabled' else 'Enabled'
    label = workflow.BEHAVIORS[kind + '_enabled']['label']
    text = '\n'.join(review(after, before))
    assert label + ': ' + initial + ' → ' + other in text
    restored = '\n'.join(review(before, after))
    assert label + ': ' + other + ' → ' + initial in restored
    assert 'Not set' not in text and 'Not set' not in restored


def test_workflow_review_runners_shows_only_changed_target_fields(review, workflow):
    before = workflow.defaults()
    before['workflows']['standard']['runners'] = copy.deepcopy(workflow.RUNNER_DEFAULT)
    before['workflows']['standard']['runners']['targets'] = {
        'linux': {'provider': 'ssh', 'host': 'old-host', 'os': 'linux', 'arch': 'x64', 'capabilities': ['test']}}
    after = copy.deepcopy(before)
    after['workflows']['standard']['runners']['timeout_seconds'] = 1200
    after['workflows']['standard']['runners']['targets']['linux']['host'] = 'new-host'
    text = '\n'.join(review(after, before))
    assert 'Runners · Timeout seconds: 900 → 1200' in text
    assert 'Runners · Targets · Linux · Host: old-host → new-host' in text
    assert 'Capabilities' not in text and 'Queue timeout' not in text


def test_workflow_review_add_remove_default_assignments_and_exclusion(review, workflow, tmp_path):
    before = workflow.action(workflow.defaults(), 'add', 'obsolete')
    repository = str(tmp_path.resolve() / 'repo')
    before['repositories'][repository] = {'workflow': 'obsolete'}
    after = workflow.action(before, 'add', 'fast')
    after['workflows'].pop('obsolete')
    after['default'] = 'fast'
    after['repositories'][repository] = {'workflow': 'fast', 'excluded': True}
    text = '\n'.join(review(after, before))
    assert 'Global default: standard → fast' in text
    assert 'Workflow fast: added' in text and 'Workflow obsolete: removed' in text
    assert 'Named workflow obsolete → Named workflow fast' in text
    assert 'Excluded: No → Yes' in text
    restored = copy.deepcopy(after)
    restored['repositories'].pop(repository)
    restored_text = '\n'.join(review(restored, after))
    assert 'Named workflow fast → Global default fast' in restored_text
    assert 'Excluded: Yes → No' in restored_text


def test_workflow_review_repository_copy_shows_only_customized_fields(review, workflow, tmp_path):
    before = workflow.defaults()
    repository = str(tmp_path.resolve() / 'repo')
    policy = workflow.resolved(before, repository)
    policy['permissions'] = {'claude': 'plan'}
    after = workflow.update(before, policy, repository)
    text = '\n'.join(review(after, before))
    assert 'Source: Global default standard → Custom copy' in text
    assert 'Permissions · Claude: Tool default → Plan only' in text
    assert 'Planning · Model' not in text and 'Text instructions:' not in text


def test_workflow_review_noop_and_activation_are_distinct(review, workflow):
    document = workflow.defaults()
    before = copy.deepcopy(document)
    assert review(document, before) == []
    assert review(document, before, activate=True) == ['Activate recommended workflow settings']
    assert document == before


def test_config_save_review_passes_baseline_without_dumping_workflows(review, workflow, t_mod, config_cli, monkeypatch):
    cfg, local = config_cli
    ui = Menu(['workflow', 'codex', 'customize', 'enabled', '0', 'back', 'save'])
    pages = []
    ui.page = lambda label, body, *args, **kwargs: pages.append((label, body)) or 'enter'
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    changed = t_mod._config_wizard(cfg)
    text = '\n'.join(pages[0][1])
    assert 'Workflow changes:' in text
    assert 'Enabled: Enabled → Disabled' in text
    assert 'Use multiple models' not in text
    assert workflow.issue_policy() not in text and workflow.PRECEDENCE not in text
    assert changed._workflow_initial['workflows']['standard']['enabled']
    assert not changed._workflow_document['workflows']['standard']['enabled']
    assert local.read_text() == '# keep me\n' and not workflow.config_path().exists()
