"""Single-model preferences are scoped, explicit, and edited transactionally."""
import copy
from types import SimpleNamespace

import pytest

from test_workflow import Menu, workflow


class InspectMenu(Menu):
    def __init__(self, picks):
        super().__init__(picks)
        self.views = []

    def pick(self, label, rows, **kwargs):
        self.views.append((label, rows, kwargs))
        return super().pick(label, rows, **kwargs)


@pytest.mark.parametrize('scope', ['named', 'repository'])
@pytest.mark.parametrize('multiple_models', [False, True])
def test_permission_editor_stages_each_tool_in_both_model_modes(workflow, t_mod, scope, multiple_models):
    document = workflow.defaults()
    policy = workflow.select_stages(document['workflows']['standard'], ['review'] if multiple_models else [])
    document = workflow.update(document, policy, name='standard')
    cfg = SimpleNamespace(repos={'api': '/repo'}, _workflow_document=document)
    initial = copy.deepcopy(document)
    target = {'name': 'standard'} if scope == 'named' else {'repository': '/repo'}
    ui = InspectMenu(['permissions', 'codex', 'auto', 'claude', 'acceptEdits', 'back', 'back'])
    t_mod._workflow_policy_menu(ui, cfg, vars(workflow), **target)
    actual = (cfg._workflow_document['workflows']['standard'] if scope == 'named' else
              workflow.effective(cfg._workflow_document, '/repo'))
    assert actual == dict(policy, permissions={'codex': 'auto', 'claude': 'acceptEdits'})
    assert dict(ui.views[0][1])['permissions'] == 'Permissions ›'
    assert ui.views[0][2]['values']['permissions'] == 'Not set'
    assert ui.views[1][2]['values'] == {'codex': 'Not set', 'claude': 'Not set'}
    assert dict(ui.views[2][1]) == {'': 'Not set', 'ask': 'Ask for approval', 'auto': 'Approve for me',
                                   'full': 'Full access', 'read_only': 'Read only'}
    assert dict(ui.views[4][1]) == {'': 'Not set', 'manual': 'Ask for approval', 'acceptEdits': 'Approve edits',
                                   'auto': 'Auto', 'bypassPermissions': 'Full access', 'plan': 'Plan only'}
    assert ui.views[-1][2]['values']['permissions'] == 'Codex: Approve for me; Claude: Approve edits'
    assert ui.views[5][2]['default'] == 1
    assert document == initial
    if scope == 'repository':
        assert cfg._workflow_document['workflows'] == initial['workflows']
    assert not workflow.config_path().exists()


@pytest.mark.parametrize('scope', ['named', 'repository'])
def test_permission_editor_cancel_noop_and_clear(workflow, t_mod, scope):
    document = workflow.defaults()
    policy = dict(document['workflows']['standard'], permissions={'codex': 'full', 'claude': 'plan'})
    document = workflow.update(document, policy, name='standard')
    cfg = SimpleNamespace(repos={'api': '/repo'}, _workflow_document=document)
    target = {'name': 'standard'} if scope == 'named' else {'repository': '/repo'}
    initial = copy.deepcopy(document)
    ui = InspectMenu(['permissions', 'codex', None, 'claude', 'plan', 'back', 'back'])
    t_mod._workflow_policy_menu(ui, cfg, vars(workflow), **target)
    assert cfg._workflow_document == initial
    assert ui.views[2][2]['default'] == 3
    t_mod._workflow_policy_menu(Menu(['permissions', 'codex', '', 'back', 'back']), cfg, vars(workflow), **target)
    actual = (cfg._workflow_document['workflows']['standard'] if scope == 'named' else
              workflow.effective(cfg._workflow_document, '/repo'))
    assert actual == dict(policy, permissions={'claude': 'plan'})
    ui = InspectMenu(['permissions', 'claude', '', 'back', 'back'])
    t_mod._workflow_policy_menu(ui, cfg, vars(workflow), **target)
    actual = (cfg._workflow_document['workflows']['standard'] if scope == 'named' else
              workflow.effective(cfg._workflow_document, '/repo'))
    assert 'permissions' not in actual
    assert ui.views[-1][2]['values']['permissions'] == 'Not set'
    initial = copy.deepcopy(cfg._workflow_document)
    t_mod._workflow_policy_menu(Menu(['permissions', 'codex', '', None, 'back']), cfg, vars(workflow), **target)
    assert cfg._workflow_document == initial
    assert not workflow.config_path().exists()


def test_permission_editor_discard_keeps_original(workflow, t_mod, monkeypatch, tmp_path):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    monkeypatch.setattr(t_mod, 'CONFIG', str(tmp_path / 'absent'))
    monkeypatch.setattr(t_mod, 'zsh_capture', lambda _: '')
    monkeypatch.setattr(workflow, 'save', lambda *args, **kwargs: pytest.fail('discard must not save'))
    ui = Menu(['workflow', 'customize', 'permissions', 'codex', 'full', 'back',
               'back', 'back', 'cancel', 'discard'])
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    cfg = t_mod.Config()
    t_mod._workflow_session(cfg)
    initial = copy.deepcopy(cfg._workflow_document)
    assert t_mod._config_wizard(cfg) is None
    assert cfg._workflow_document == initial
    assert not t_mod._workflow_pending(cfg)
    assert not workflow.config_path().exists()


def test_permission_list_and_save_review_show_tool_labels(workflow, t_mod, monkeypatch, capsys):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    document = workflow.defaults()
    policy = dict(document['workflows']['standard'], permissions={'codex': 'read_only', 'claude': 'manual'})
    document = workflow.update(document, policy, name='standard')
    t_mod._workflow_print(vars(workflow), {'scope': 'global', 'source': 'global',
        'workflow_name': 'standard', 'excluded': False, 'workflow': policy,
        'propagation': {'runtime': 'pending'}})
    output = capsys.readouterr().out
    assert 'Permissions: New and resumed CLI sessions' in output
    assert 'Codex: Read only' in output and 'Claude: Ask for approval' in output
    review = '\n'.join(t_mod._workflow_review(document))
    assert 'Codex permissions: Read only' in review and 'Claude permissions: Ask for approval' in review
    catalogue = dict(workflow.catalogue(document), propagation={'runtime': 'pending'})
    t_mod._workflow_print(vars(workflow), catalogue, catalogue=True)
    assert 'Codex: Read only; Claude: Ask for approval' in capsys.readouterr().out


@pytest.mark.parametrize('scope', ['named', 'repository'])
def test_clear_text_instructions_is_staged_and_preserves_policy(workflow, t_mod, scope):
    document = workflow.defaults()
    policy = workflow.select_stages(document['workflows']['standard'], ['review'])
    policy.update(instructions='Custom instructions to remove.', behaviors={'pr_merge': 'on_request'},
                  session_defaults={'codex': {'model': 'retained-default'}})
    document = workflow.update(document, policy, name='standard')
    cfg = SimpleNamespace(repos={'api': '/repo'}, _workflow_document=document)
    initial = copy.deepcopy(document)
    target = {'name': 'standard'} if scope == 'named' else {'repository': '/repo'}
    ui = InspectMenu(['clear_instructions', 'back'])
    t_mod._workflow_policy_menu(ui, cfg, vars(workflow), **target)
    actual = (cfg._workflow_document['workflows']['standard'] if scope == 'named' else
              workflow.effective(cfg._workflow_document, '/repo'))
    assert actual == dict(policy, instructions='')
    assert actual['enabled']
    assert dict(ui.views[0][1])['clear_instructions'] == 'Clear text instructions'
    assert 'clear_instructions' not in dict(ui.views[-1][1])
    assert document == initial
    if scope == 'repository':
        assert cfg._workflow_document['workflows'] == initial['workflows']
    before = copy.deepcopy(cfg._workflow_document)
    t_mod._workflow_policy_menu(Menu([None]), cfg, vars(workflow), **target)
    assert cfg._workflow_document == before
    assert not workflow.config_path().exists()


@pytest.mark.parametrize('scope', ['named', 'repository'])
@pytest.mark.parametrize('cancelled', [False, True])
def test_text_editor_cancel_or_unchanged_keeps_instructions(workflow, t_mod, monkeypatch, scope, cancelled):
    document = workflow.defaults()
    cfg = SimpleNamespace(repos={'api': '/repo'}, _workflow_document=document)
    initial = copy.deepcopy(document)
    monkeypatch.setattr(workflow, 'edit_instructions', lambda *args: (130 if cancelled else 0, document))
    target = {'name': 'standard'} if scope == 'named' else {'repository': '/repo'}
    t_mod._workflow_policy_menu(Menu(['instructions', 'back']), cfg, vars(workflow), **target)
    assert cfg._workflow_document == initial
    assert not workflow.config_path().exists()


def test_legacy_preview_policy_displays_separate_approval_without_rewriting(workflow, t_mod):
    policy = dict(copy.deepcopy(workflow.DEFAULT), behaviors={'local_preview': 'before_pr'})
    initial = copy.deepcopy(policy)
    ui = InspectMenu(['local_preview', 'local_preview', None, 'implement_before_approval', None, 'back', 'back'])
    assert t_mod._workflow_behaviors_menu(ui, vars(workflow), policy) == initial
    assert policy == initial
    assert ui.views[1][2]['values']['local_preview'] == 'Automatically when ready'
    assert ui.views[1][2]['values']['implement_before_approval'] == 'No'
    assert 'before_pr' not in dict(ui.views[2][1])


def test_pr_open_choices_distinguish_timing_and_explicit_request(workflow, t_mod):
    ui = InspectMenu(['pull_requests', 'pr_open', 'on_request', 'back', 'back'])
    policy = t_mod._workflow_behaviors_menu(ui, vars(workflow), copy.deepcopy(workflow.DEFAULT))
    assert dict(ui.views[1][1])['pr_open'] == 'When to open PR'
    assert ui.views[2][0] == 'When to open PR'
    assert dict(ui.views[2][1]) == {'': 'Not set', 'automatic': 'Automatically when ready',
                                   'on_request': 'Only when I ask'}
    assert policy['behaviors']['pr_open'] == 'on_request'
    assert t_mod._workflow_choice(vars(workflow), 'local_preview', 'on_request') == 'Only when I ask'
    assert t_mod._workflow_choice(vars(workflow), 'pr_merge', 'on_request') == 'When I ask'


@pytest.mark.parametrize('scope', ['named', 'repository'])
@pytest.mark.parametrize('choice', ['review', 'skip'])
def test_pr_review_preference_stages_and_retains_selected_review_model(workflow, t_mod, scope, choice):
    document = workflow.defaults()
    cfg = SimpleNamespace(repos={'api': '/repo'}, _workflow_document=document)
    initial = copy.deepcopy(document)
    target = {'name': 'standard'} if scope == 'named' else {'repository': '/repo'}
    ui = InspectMenu(['behaviors', 'pull_requests', 'pr_review', choice, 'back', 'back', 'back'])
    t_mod._workflow_policy_menu(ui, cfg, vars(workflow), **target)
    actual = cfg._workflow_document['workflows']['standard'] if scope == 'named' else workflow.effective(cfg._workflow_document, '/repo')
    pull_requests = next(view for view in ui.views if view[0] == 'Pull requests')
    assert pull_requests[1][0] == ('pr_review', 'Review before PR')
    picker = next(view for view in ui.views if view[0] == 'Review before PR')
    assert picker[1] == [('', 'Not set'), ('review', 'Review changes'), ('skip', 'Skip review')]
    assert actual['behaviors']['pr_review'] == choice
    assert actual['review'] == initial['workflows']['standard']['review']
    assert 'review' in workflow.model_stages(actual)
    assert 'review' in dict(ui.views[-1][1])
    assert (ui.views[-1][2]['values']['review'] == 'Skipped before PR') is (choice == 'skip')
    review_editor = InspectMenu(['review', None, 'back'])
    t_mod._workflow_policy_menu(review_editor, cfg, vars(workflow), **target)
    assert review_editor.views[1][0] == 'Review'
    assert review_editor.views[1][2]['values']['model'] == actual['review']['model']
    before = copy.deepcopy(cfg._workflow_document)
    t_mod._workflow_policy_menu(Menu(['behaviors', 'pull_requests', 'pr_review', choice, 'back', 'back', 'back']),
                               cfg, vars(workflow), **target)
    assert cfg._workflow_document == before
    assert document == initial
    assert not workflow.config_path().exists()


@pytest.mark.parametrize('choice', [None, ''])
def test_pr_review_unset_or_cancel_does_not_materialize_preference(workflow, t_mod, choice):
    original = copy.deepcopy(workflow.DEFAULT)
    assert t_mod._workflow_behaviors_menu(Menu(['pull_requests', 'pr_review', choice, 'back', 'back']),
                                          vars(workflow), original) == original
    configured = dict(original, behaviors={'pr_review': 'skip', 'pr_merge': 'on_request'})
    after = t_mod._workflow_behaviors_menu(Menu(['pull_requests', 'pr_review', choice, 'back', 'back']),
                                         vars(workflow), configured)
    assert after == (configured if choice is None else dict(original, behaviors={'pr_merge': 'on_request'}))
    assert configured['behaviors']['pr_review'] == 'skip'


def test_pr_review_skip_is_clear_in_list_and_review_without_losing_settings(workflow, t_mod, monkeypatch, capsys):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    document = workflow.defaults()
    policy = document['workflows']['standard']
    policy['behaviors'] = {'pr_review': 'skip'}
    policy['review'] = {'model': 'retained-review-model', 'effort': 'high'}
    before = copy.deepcopy(policy)
    assert t_mod._workflow_model_rows(vars(workflow), policy)[2] == ('Review', '—', '—', 'Skipped before PR')
    t_mod._workflow_print(vars(workflow), {'scope': 'global', 'source': 'global', 'workflow_name': 'standard',
        'excluded': False, 'workflow': policy, 'propagation': {'runtime': 'pending'}})
    output = capsys.readouterr().out
    review = '\n'.join(t_mod._workflow_review(document))
    for text in (output, review):
        assert 'Review before PR: Skip review' in text and 'Skipped before PR' in text
        assert 'retained-review-model' not in text
    assert policy == before
    restored = workflow.update_behavior(policy, 'pr_review', '')
    assert t_mod._workflow_model_rows(vars(workflow), restored)[2] == ('Review', 'retained-review-model', 'High', 'Worker')
    assert policy['behaviors']['pr_review'] == 'skip'


def test_preview_methods_and_approval_choices_keep_context(workflow, t_mod):
    original = dict(copy.deepcopy(workflow.DEFAULT), behaviors={'pr_open': 'on_request'})
    ui = InspectMenu(['local_preview', 'cli', 'cli_preview', 'run', 'back', 'implement_before_approval', 'yes',
                      'frontend', 'frontend_preview', 'rebuild', 'back', 'back', 'back'])
    policy = t_mod._workflow_behaviors_menu(ui, vars(workflow), original)
    assert ui.views[2][0] == 'CLI'
    assert ui.views[3][0] == 'Preview method'
    assert dict(ui.views[3][1]) == {'': 'Not set', 'install': 'Install locally', 'run': 'Run from the repo'}
    assert ui.views[9][0] == 'Preview method'
    assert dict(ui.views[9][1]) == {'': 'Not set', 'fast_reload': 'Fast reload', 'rebuild': 'Build'}
    assert ui.views[6][0] == 'Implement before approval'
    assert dict(ui.views[6][1]) == {'': 'Not set', 'yes': 'Yes', 'no': 'No'}
    assert ui.views[8][0] == 'Frontend'
    assert dict(ui.views[8][1]) == {'frontend_enabled': 'Preview', 'frontend_preview_timing': 'When to preview',
                                   'frontend_implement_before_approval': 'Implement before approval', 'frontend_design': 'Design approach',
                                   'frontend_preview': 'Preview method', 'frontend_browser': 'Open in browser', 'back': 'Back'}
    assert ui.views[5][2]['default'] == 3
    assert ui.views[11][2]['default'] == 2
    assert policy['behaviors'] == {'pr_open': 'on_request', 'cli_preview': 'run',
                                   'frontend_preview': 'rebuild', 'implement_before_approval': 'yes'}
    assert original['behaviors'] == {'pr_open': 'on_request'}


@pytest.mark.parametrize('kind', ['frontend', 'cli'])
@pytest.mark.parametrize('defaults', [{}, {'local_preview': 'when_ready', 'implement_before_approval': 'no'}])
def test_preview_child_fields_show_live_inheritance_without_materializing_choices(workflow, t_mod, kind, defaults):
    original = dict(copy.deepcopy(workflow.DEFAULT), behaviors=dict(defaults))
    timing = kind + '_preview_timing'
    implementation = kind + '_implement_before_approval'
    ui = InspectMenu(['local_preview', kind, timing, None, implementation, None, 'back', 'back', 'back'])
    assert t_mod._workflow_behaviors_menu(ui, vars(workflow), original) == original
    timing_label = 'Automatically when ready' if defaults else 'Not set'
    implementation_label = 'No' if defaults else 'Not set'
    assert ui.views[2][2]['values'][timing] == 'Inherit · ' + timing_label
    assert ui.views[2][2]['values'][implementation] == 'Inherit · ' + implementation_label
    assert ui.views[3][1] == [('', 'Inherit · ' + timing_label), ('when_ready', 'Automatically when ready'), ('on_request', 'Only when I ask')]
    assert ui.views[5][1] == [('', 'Inherit · ' + implementation_label), ('yes', 'Yes'), ('no', 'No')]
    assert ui.views[3][2]['default'] == ui.views[5][2]['default'] == 0
    assert timing not in original['behaviors'] and implementation not in original['behaviors']
    assert not workflow.config_path().exists()


@pytest.mark.parametrize('kind', ['frontend', 'cli'])
def test_preview_overrides_survive_parent_edits_and_clear_to_live_defaults(workflow, t_mod, kind):
    original = dict(copy.deepcopy(workflow.DEFAULT), behaviors={'local_preview': 'when_ready',
        'implement_before_approval': 'no', 'pr_open': 'on_request'})
    before = copy.deepcopy(original)
    timing = kind + '_preview_timing'
    implementation = kind + '_implement_before_approval'
    ui = InspectMenu(['local_preview', kind, timing, 'when_ready', implementation, 'no', 'back',
        'local_preview', 'on_request', 'implement_before_approval', 'yes', kind, timing, None,
        implementation, None, 'back', 'back', 'back'])
    overridden = t_mod._workflow_behaviors_menu(ui, vars(workflow), original)
    assert ui.views[6][2]['values'][timing] == 'Override · Automatically when ready'
    assert ui.views[6][2]['values'][implementation] == 'Override · No'
    assert ui.views[12][2]['values'][timing] == 'Override · Automatically when ready'
    assert ui.views[12][2]['values'][implementation] == 'Override · No'
    assert ui.views[13][1][0] == ('', 'Inherit · Only when I ask')
    assert ui.views[15][1][0] == ('', 'Inherit · Yes')
    assert overridden['behaviors'][timing] == 'when_ready' and overridden['behaviors'][implementation] == 'no'
    assert overridden['behaviors']['local_preview'] == 'on_request'
    assert overridden['behaviors']['implement_before_approval'] == 'yes'
    cleared = t_mod._workflow_behaviors_menu(Menu(['local_preview', kind, timing, '', implementation, '',
        'back', 'back', 'back']), vars(workflow), overridden)
    assert timing not in cleared['behaviors'] and implementation not in cleared['behaviors']
    reopened = InspectMenu(['local_preview', kind, None, 'back', 'back'])
    assert t_mod._workflow_behaviors_menu(reopened, vars(workflow), cleared) == cleared
    assert reopened.views[2][2]['values'][timing] == 'Inherit · Only when I ask'
    assert reopened.views[2][2]['values'][implementation] == 'Inherit · Yes'
    assert cleared['behaviors']['pr_open'] == 'on_request'
    assert original == before


def test_preview_inheritance_sources_are_clear_in_list_review_and_catalogue(workflow, t_mod, monkeypatch, capsys):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    policy = dict(copy.deepcopy(workflow.DEFAULT), behaviors={'local_preview': 'on_request',
        'implement_before_approval': 'no', 'frontend_preview_timing': 'on_request',
        'frontend_implement_before_approval': 'yes'})
    before = copy.deepcopy(policy)
    t_mod._workflow_print(vars(workflow), {'scope': 'global', 'source': 'global', 'workflow_name': 'standard',
        'excluded': False, 'workflow': policy, 'propagation': {'runtime': 'pending'}})
    output = capsys.readouterr().out
    document = workflow.update(workflow.defaults(), policy, name='standard')
    review = '\n'.join(t_mod._workflow_review(document))
    catalogue = dict(workflow.catalogue(document), propagation={'runtime': 'pending'})
    t_mod._workflow_print(vars(workflow), catalogue, catalogue=True)
    catalogue_text = capsys.readouterr().out
    for text in (output, review, catalogue_text):
        assert 'When to preview: Override · Only when I ask' in text
        assert 'When to preview: Inherit · Only when I ask' in text
        assert 'Implement before approval: Override · Yes' in text
        assert 'Implement before approval: Inherit · No' in text
        assert 'Preview approval before PR' not in text and 'Design approval' not in text
    assert policy == before
    assert not workflow.config_path().exists()


@pytest.mark.parametrize('legacy', ['required', 'continue'])
@pytest.mark.parametrize('explicit', [None, 'yes', 'no'])
def test_retired_design_approval_uses_single_preview_gate_without_rewriting(workflow, t_mod, monkeypatch, capsys, legacy, explicit):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    behaviors = {'design_approval': legacy}
    if explicit is not None:
        behaviors['implement_before_approval'] = explicit
    original = dict(copy.deepcopy(workflow.DEFAULT), behaviors=behaviors)
    before = copy.deepcopy(original)
    ui = InspectMenu(['local_preview', 'frontend', None, 'back', 'back'])
    assert t_mod._workflow_behaviors_menu(ui, vars(workflow), original) == before
    expected = explicit or ('no' if legacy == 'required' else 'yes')
    label = 'No' if expected == 'no' else 'Yes'
    assert ui.views[1][2]['values']['implement_before_approval'] == label
    assert 'design_approval' not in dict(ui.views[2][1])
    assert dict(ui.views[2][1]) == {'frontend_enabled': 'Preview', 'frontend_preview_timing': 'When to preview',
                                   'frontend_implement_before_approval': 'Implement before approval', 'frontend_design': 'Design approach',
                                   'frontend_preview': 'Preview method', 'frontend_browser': 'Open in browser', 'back': 'Back'}
    t_mod._workflow_print(vars(workflow), {'scope': 'global', 'source': 'global', 'workflow_name': 'standard',
        'excluded': False, 'workflow': original, 'propagation': {'runtime': 'pending'}})
    output = capsys.readouterr().out
    document = workflow.update(workflow.defaults(), original, name='standard')
    review = '\n'.join(t_mod._workflow_review(document))
    for text in (output, review):
        assert 'Design approval' not in text
        assert 'Implement before approval: ' + label in text
        assert 'Preview approval before PR' not in text
    catalogue = dict(workflow.catalogue(document), propagation={'runtime': 'pending'})
    t_mod._workflow_print(vars(workflow), catalogue, catalogue=True)
    assert 'Design approval' not in capsys.readouterr().out
    assert original == before
    assert not workflow.config_path().exists()


def test_nested_preview_menus_stage_changes_and_restore_parent_focus(workflow, t_mod):
    original = dict(copy.deepcopy(workflow.DEFAULT), behaviors={'preview_target': 'frontend', 'cli_preview': 'install'})
    ui = InspectMenu(['local_preview', 'frontend', 'frontend_design', 'in_app',
                      'frontend_preview', 'rebuild', 'frontend_browser', 'automatic', None, None, 'back'])
    policy = t_mod._workflow_behaviors_menu(ui, vars(workflow), original)
    assert dict(ui.views[0][1]) == {'pull_requests': 'Pull requests ›', 'local_preview': 'Local preview ›',
                                   'tests': 'Tests ›', 'back': 'Back'}
    assert dict(ui.views[1][1]) == {'local_preview': 'When to preview',
        'implement_before_approval': 'Implement before approval', 'frontend': 'Frontend ›', 'cli': 'CLI ›', 'back': 'Back'}
    assert ui.views[2][0] == 'Frontend'
    assert ui.views[9][0] == 'Local preview' and ui.views[9][2]['default'] == 2
    assert ui.views[10][0] == 'PRs, previews & tests' and ui.views[10][2]['default'] == 1
    assert policy['behaviors'] == {'preview_target': 'frontend', 'cli_preview': 'install', 'frontend_design': 'in_app',
        'frontend_preview': 'rebuild', 'frontend_browser': 'automatic'}
    summary = ui.views[10][2]['values']['local_preview']
    assert 'Frontend: Enabled' in summary and 'Design approach: In the app' in summary
    assert 'Preview method: Build' in summary and 'Open in browser: Automatically' in summary
    assert 'CLI: Disabled' in summary and 'Install locally' not in summary
    assert original['behaviors'] == {'preview_target': 'frontend', 'cli_preview': 'install'}
    assert not workflow.config_path().exists()


@pytest.mark.parametrize('scope', ['named', 'repository'])
def test_tests_runner_edits_keep_prior_and_later_behavior_changes(workflow, t_mod, monkeypatch, scope):
    document = workflow.defaults()
    policy = dict(document['workflows']['standard'], behaviors={'pr_open': 'on_request'})
    document = workflow.update(document, policy, name='standard')
    cfg = SimpleNamespace(repos={'api': '/repo'}, _workflow_document=document)
    target = {'name': 'standard'} if scope == 'named' else {'repository': '/repo'}
    def edit_runners(current_document, repository=None, name=None):
        current = copy.deepcopy(current_document['workflows'][name]) if name else workflow.resolved(current_document, repository)
        assert current['behaviors'] == {'pr_open': 'automatic', 'test_timing': 'before_preview'}
        current['runners'] = dict(copy.deepcopy(workflow.RUNNER_DEFAULT), timeout_seconds=123)
        return 0, workflow.update(current_document, current, repository, name)
    monkeypatch.setattr(workflow, 'edit_runners', edit_runners)
    ui = InspectMenu(['behaviors', 'pull_requests', 'pr_open', 'automatic', 'back', 'tests', 'test_timing',
        'before_preview', 'runners', 'edit', 'back', 'test_timing', 'pr_ci', 'back', 'pull_requests', 'pr_open',
        'on_request', 'back', 'back', 'back'])
    t_mod._workflow_policy_menu(ui, cfg, vars(workflow), **target)
    actual = cfg._workflow_document['workflows']['standard'] if scope == 'named' else workflow.effective(cfg._workflow_document, '/repo')
    assert actual['behaviors'] == {'pr_open': 'on_request', 'test_timing': 'pr_ci'}
    assert actual['runners']['timeout_seconds'] == 123
    assert 'runners' not in dict(ui.views[0][1])
    test_views = [view for view in ui.views if view[0] == 'Tests']
    assert dict(test_views[0][1]) == {'test_timing': 'Run tests', 'runners': 'Test runners ›', 'back': 'Back'}
    assert any(view[2]['default'] == 1 for view in test_views[1:])
    assert not workflow.config_path().exists()


@pytest.mark.parametrize('scope', ['named', 'repository'])
def test_tests_runner_browsing_is_noop(workflow, t_mod, scope):
    cfg = SimpleNamespace(repos={'api': '/repo'}, _workflow_document=workflow.defaults())
    initial = copy.deepcopy(cfg._workflow_document)
    target = {'name': 'standard'} if scope == 'named' else {'repository': '/repo'}
    t_mod._workflow_policy_menu(Menu(['behaviors', 'tests', 'runners', 'back', 'back', 'back', 'back']),
                               cfg, vars(workflow), **target)
    assert cfg._workflow_document == initial
    assert not workflow.config_path().exists()


def test_behavior_revert_after_runner_visit_does_not_keep_intermediate_snapshot(workflow, t_mod):
    document = workflow.defaults()
    document['workflows']['standard']['behaviors'] = {'test_timing': 'pr_ci'}
    cfg = SimpleNamespace(repos={}, _workflow_document=document)
    initial = copy.deepcopy(document)
    t_mod._workflow_policy_menu(Menu(['behaviors', 'tests', 'test_timing', 'before_preview', 'runners', 'back',
        'test_timing', 'pr_ci', 'back', 'back', 'back']), cfg, vars(workflow), name='standard')
    assert cfg._workflow_document == initial
    assert not workflow.config_path().exists()


@pytest.mark.parametrize('target', ['', 'frontend', 'cli', 'both'])
def test_preview_submenus_replace_legacy_target_with_enable_controls(workflow, t_mod, target):
    behaviors = {'cli_preview': 'install', 'frontend_preview': 'fast_reload', 'frontend_design': 'standalone_html'}
    if target:
        behaviors['preview_target'] = target
    original = dict(copy.deepcopy(workflow.DEFAULT), behaviors=behaviors)
    ui = InspectMenu(['local_preview', 'frontend', 'frontend_enabled', None, 'back',
                      'cli', 'cli_enabled', None, 'back', 'back', 'back'])
    policy = t_mod._workflow_behaviors_menu(ui, vars(workflow), original)
    assert dict(ui.views[1][1]) == {'local_preview': 'When to preview', 'implement_before_approval': 'Implement before approval',
                                   'frontend': 'Frontend ›', 'cli': 'CLI ›', 'back': 'Back'}
    assert ui.views[3][0] == 'Frontend preview' and ui.views[7][0] == 'CLI preview'
    for view, kind in [(ui.views[3], 'frontend'), (ui.views[7], 'cli')]:
        enabled = workflow.preview_enabled(behaviors, kind)
        assert view[1] == [('enabled', 'Enabled'), ('disabled', 'Disabled')]
        assert view[2]['default'] == (0 if enabled else 1)
    summary = ui.views[-1][2]['values']['local_preview']
    assert ('Preview method: Install locally' in summary) is workflow.preview_enabled(behaviors, 'cli')
    assert ('Preview method: Fast reload' in summary) is workflow.preview_enabled(behaviors, 'frontend')
    assert 'Applies to' not in summary
    assert policy == original


@pytest.mark.parametrize('kind, method, value', [('frontend', 'frontend_preview', 'rebuild'), ('cli', 'cli_preview', 'run')])
def test_preview_toggles_preserve_disabled_choices_and_reenable(workflow, t_mod, kind, method, value):
    original = dict(copy.deepcopy(workflow.DEFAULT), behaviors={'local_preview': 'when_ready'})
    ui = InspectMenu(['local_preview', kind, kind + '_enabled', 'disabled', method, value, 'back', 'back', 'back'])
    disabled = t_mod._workflow_behaviors_menu(ui, vars(workflow), original)
    assert disabled['behaviors'][kind + '_enabled'] == 'disabled'
    assert disabled['behaviors'][method] == value
    assert ui.views[4][2]['values'][kind + '_enabled'] == 'Disabled'
    label = 'CLI' if kind == 'cli' else 'Frontend'
    assert label + ': Disabled' in ui.views[-1][2]['values']['local_preview']
    before = copy.deepcopy(disabled)
    assert t_mod._workflow_behaviors_menu(Menu(['local_preview', kind, kind + '_enabled', None, 'back', 'back', 'back']),
                                          vars(workflow), disabled) == before
    enabled = t_mod._workflow_behaviors_menu(Menu(['local_preview', kind, kind + '_enabled', 'enabled', 'back', 'back', 'back']),
                                            vars(workflow), disabled)
    assert enabled['behaviors'][kind + '_enabled'] == 'enabled'
    assert enabled['behaviors'][method] == value
    assert original['behaviors'] == {'local_preview': 'when_ready'}


@pytest.mark.parametrize('browser, label', [('automatic', 'Automatically'), ('on_request', 'Only when I ask')])
def test_frontend_browser_setting_is_independent_and_clearable(workflow, t_mod, browser, label):
    original = dict(copy.deepcopy(workflow.DEFAULT), behaviors={'frontend_design': 'in_app',
        'implement_before_approval': 'no', 'frontend_preview': 'rebuild', 'preview_target': 'cli'})
    ui = InspectMenu(['local_preview', 'frontend', 'frontend_browser', browser, 'back', 'back', 'back'])
    policy = t_mod._workflow_behaviors_menu(ui, vars(workflow), original)
    assert ui.views[2][0] == 'Frontend'
    assert dict(ui.views[2][1]) == {'frontend_enabled': 'Preview', 'frontend_preview_timing': 'When to preview',
                                  'frontend_implement_before_approval': 'Implement before approval', 'frontend_design': 'Design approach',
                                  'frontend_preview': 'Preview method', 'frontend_browser': 'Open in browser', 'back': 'Back'}
    assert ui.views[3][0] == 'Open in browser'
    assert dict(ui.views[3][1]) == {'': 'Not set', 'automatic': 'Automatically', 'on_request': 'Only when I ask'}
    assert policy['behaviors'] == dict(original['behaviors'], frontend_browser=browser)
    assert ui.views[4][2]['values']['frontend_browser'] == label
    assert 'Frontend: Disabled' in ui.views[-1][2]['values']['local_preview']
    assert 'Open in browser:' not in ui.views[-1][2]['values']['local_preview']
    for picked in (None, browser):
        assert t_mod._workflow_behaviors_menu(Menu(['local_preview', 'frontend', 'frontend_browser', picked, 'back', 'back', 'back']),
                                              vars(workflow), policy) == policy
    assert t_mod._workflow_behaviors_menu(Menu(['local_preview', 'frontend', 'frontend_browser', '', 'back', 'back', 'back']),
                                          vars(workflow), policy) == original
    assert 'frontend_browser' not in original['behaviors']


@pytest.mark.parametrize('scope', ['named', 'repository'])
@pytest.mark.parametrize('launch, label', [('automatic', 'Automatically'), ('on_request', 'Only when I ask')])
def test_cli_launch_stages_clears_and_preserves_preview_preferences(workflow, t_mod, scope, launch, label):
    document = workflow.defaults()
    policy = dict(document['workflows']['standard'], behaviors={'local_preview': 'when_ready',
        'implement_before_approval': 'no', 'cli_preview_timing': 'on_request', 'cli_preview': 'run'})
    document = workflow.update(document, policy, name='standard')
    initial = copy.deepcopy(document)
    cfg = SimpleNamespace(repos={'api': '/repo'}, _workflow_document=document)
    target = {'name': 'standard'} if scope == 'named' else {'repository': '/repo'}
    ui = InspectMenu(['behaviors', 'local_preview', 'cli', 'cli_launch', launch, 'back', 'back', 'back', 'back'])
    t_mod._workflow_policy_menu(ui, cfg, vars(workflow), **target)
    actual = cfg._workflow_document['workflows']['standard'] if scope == 'named' else workflow.effective(cfg._workflow_document, '/repo')
    assert actual == dict(policy, behaviors=dict(policy['behaviors'], cli_launch=launch))
    cli = next(view for view in ui.views if view[0] == 'CLI')
    assert cli[1][-3:] == [('cli_preview', 'Preview method'), ('cli_launch', 'Run command'), ('back', 'Back')]
    picker = next(view for view in ui.views if view[0] == 'Run command')
    assert picker[1] == [('', 'Not set'), ('automatic', 'Automatically'), ('on_request', 'Only when I ask')]
    assert picker[2]['default'] == 0
    assert [view for view in ui.views if view[0] == 'CLI'][-1][2]['values']['cli_launch'] == label
    for picked in (None, launch):
        assert t_mod._workflow_behaviors_menu(Menu(['local_preview', 'cli', 'cli_launch', picked, 'back', 'back', 'back']),
                                              vars(workflow), actual) == actual
    assert t_mod._workflow_behaviors_menu(Menu(['local_preview', 'cli', 'cli_launch', '', 'back', 'back', 'back']),
                                          vars(workflow), actual) == policy
    assert document == initial
    assert not workflow.config_path().exists()


@pytest.mark.parametrize('enabled', [False, True])
def test_cli_launch_display_respects_enabled_state_without_losing_setting(workflow, t_mod, monkeypatch, capsys, enabled):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    policy = dict(copy.deepcopy(workflow.DEFAULT), behaviors={'cli_enabled': 'enabled' if enabled else 'disabled',
        'cli_preview': 'run', 'cli_launch': 'on_request'})
    before = copy.deepcopy(policy)
    t_mod._workflow_print(vars(workflow), {'scope': 'global', 'source': 'global', 'workflow_name': 'standard',
        'excluded': False, 'workflow': policy, 'propagation': {'runtime': 'pending'}})
    output = capsys.readouterr().out
    document = workflow.update(workflow.defaults(), policy, name='standard')
    review = '\n'.join(t_mod._workflow_review(document))
    catalogue = dict(workflow.catalogue(document), propagation={'runtime': 'pending'})
    t_mod._workflow_print(vars(workflow), catalogue, catalogue=True)
    catalogue_text = capsys.readouterr().out
    for text in (output, review, catalogue_text):
        assert ('Run command: Only when I ask' in text) is enabled
    ui = InspectMenu(['local_preview', 'cli', None, 'back', 'back'])
    assert t_mod._workflow_behaviors_menu(ui, vars(workflow), policy) == before
    assert ui.views[2][2]['values']['cli_launch'] == 'Only when I ask'
    assert ui.views[2][2]['values']['cli_enabled'] == ('Enabled' if enabled else 'Disabled')
    assert policy == before


@pytest.mark.parametrize('target', ['frontend', 'cli', 'both'])
def test_printed_preview_sections_match_target_and_frontend_grouping(workflow, t_mod, capsys, target):
    policy = dict(copy.deepcopy(workflow.DEFAULT), behaviors={'preview_target': target,
        'cli_preview': 'install', 'frontend_preview': 'rebuild', 'frontend_design': 'in_app'})
    t_mod._workflow_print(vars(workflow), {'scope': 'global', 'source': 'global',
        'workflow_name': 'standard', 'excluded': False, 'workflow': policy,
        'propagation': {'runtime': 'pending'}})
    output = capsys.readouterr().out
    local = output.split('  Local preview\n', 1)[1].split('  Tests\n', 1)[0]
    shared = local.split('    Frontend · ', 1)[0]
    frontend = local.split('    Frontend · ', 1)[1].split('    CLI · ', 1)[0]
    assert 'Applies to:' not in local
    assert ('      Preview method: Install locally' in local) is (target != 'frontend')
    assert '    CLI · ' + ('Disabled' if target == 'frontend' else 'Enabled') in local
    assert '    Frontend · ' + ('Disabled' if target == 'cli' else 'Enabled') in local
    assert 'Preview method:' not in shared
    for expected in ('Design approach: In the app', 'Preview method: Build', 'Open in browser: Not set'):
        assert (expected in frontend) is (target != 'cli')
    assert 'Design approval' not in output
    assert '\n  Frontend' not in output and '\n  CLI\n' not in output


def test_disabled_preview_display_omits_retained_settings_and_legacy_target(workflow, t_mod, capsys):
    policy = dict(copy.deepcopy(workflow.DEFAULT), behaviors={'preview_target': 'both',
        'frontend_enabled': 'disabled', 'cli_enabled': 'disabled', 'local_preview': 'when_ready',
        'implement_before_approval': 'no', 'frontend_design': 'in_app', 'frontend_preview': 'rebuild',
        'frontend_browser': 'automatic', 'cli_preview': 'install', 'pr_open': 'on_request'})
    before = copy.deepcopy(policy)
    result = {'scope': 'global', 'source': 'global', 'workflow_name': 'standard', 'excluded': False,
              'workflow': policy, 'propagation': {'runtime': 'pending'}}
    t_mod._workflow_print(vars(workflow), result)
    output = capsys.readouterr().out
    assert 'Frontend · Disabled' in output and 'CLI · Disabled' in output
    for inactive in ('Applies to', 'When to preview:', 'Preview method:', 'Design approach:', 'Open in browser:'):
        assert inactive not in output
    assert 'When to open PR: Only when I ask' in output
    document = workflow.update(workflow.defaults(), policy, name='standard')
    catalogue = dict(workflow.catalogue(document), propagation={'runtime': 'pending'})
    t_mod._workflow_print(vars(workflow), catalogue, catalogue=True)
    output = capsys.readouterr().out
    assert 'applies to' not in output.lower() and 'Install locally' not in output and 'Build' not in output
    assert policy == before


@pytest.mark.parametrize('stage', ['planning', 'implementation', 'review'])
def test_each_stage_can_let_agent_choose_worker_effort(workflow, t_mod, stage):
    document = workflow.defaults()
    document['workflows']['standard'][stage]['effort'] = 'high'
    cfg = SimpleNamespace(repos={}, _workflow_document=document)
    ui = InspectMenu([stage, 'effort', 'auto', 'back'])
    t_mod._workflow_policy_menu(ui, cfg, vars(workflow), name='standard')
    policy = cfg._workflow_document['workflows']['standard']
    assert policy[stage]['effort'] == 'auto'
    choices = dict(ui.views[2][1])
    assert choices['auto'] == 'Agent selects by task'
    assert ui.views[2][2]['sections'] == {'auto': 'Automatic', 'minimal': 'Fixed effort'}
    assert ui.views[2][1][ui.views[2][2]['default']][0] == 'high'
    assert 'Automatic' not in choices and 'Fixed effort' not in choices
    assert 'one-level-lower' not in choices
    assert all('starts' not in label for label in choices.values())
    row = next(row for row in t_mod._workflow_model_rows(vars(workflow), policy)
               if row[0] == stage.title())
    assert row[2:] == ('Agent selects by task', 'Worker')
    assert not workflow.config_path().exists()


@pytest.mark.parametrize('stage', ['implementation', 'review'])
@pytest.mark.parametrize('scope', ['named', 'repository'])
@pytest.mark.parametrize('picked', [None, 'auto'])
def test_retired_effort_displays_as_automatic_without_rewriting(workflow, t_mod, stage, scope, picked):
    document = workflow.defaults()
    document['workflows']['standard'][stage]['effort'] = 'one-level-lower'
    initial = copy.deepcopy(document)
    cfg = SimpleNamespace(repos={'api': '/repo'}, _workflow_document=document)
    target = {'name': 'standard'} if scope == 'named' else {'repository': '/repo'}
    ui = InspectMenu([stage, 'effort', picked, 'back'])
    t_mod._workflow_policy_menu(ui, cfg, vars(workflow), **target)
    policy = (cfg._workflow_document['workflows']['standard'] if scope == 'named' else
              workflow.effective(cfg._workflow_document, '/repo'))
    assert policy[stage]['effort'] == 'one-level-lower'
    choices = ui.views[2][1]
    assert choices[0] == ('auto', 'Agent selects by task')
    assert 'one-level-lower' not in dict(choices)
    assert ui.views[2][2]['sections'] == {'auto': 'Automatic', 'minimal': 'Fixed effort'}
    row = next(row for row in t_mod._workflow_model_rows(vars(workflow), policy) if row[0] == stage.title())
    assert row[2:] == ('Agent selects by task', 'Worker')
    reopened = InspectMenu([stage, 'effort', None, 'back'])
    before = copy.deepcopy(cfg._workflow_document)
    t_mod._workflow_policy_menu(reopened, cfg, vars(workflow), **target)
    picker = reopened.views[2]
    assert picker[1][picker[2]['default']][0] == 'auto'
    assert cfg._workflow_document == before
    assert document == initial
    assert not workflow.config_path().exists()
    t_mod._workflow_policy_menu(Menu([stage, 'effort', 'high', 'back']), cfg, vars(workflow), **target)
    edited = (cfg._workflow_document['workflows']['standard'] if scope == 'named' else
              workflow.effective(cfg._workflow_document, '/repo'))
    assert edited[stage]['effort'] == 'high'
    assert document == initial


@pytest.mark.parametrize('effort, label', [('auto', 'Agent selects by task'),
    ('one-level-lower', 'Agent selects by task')])
def test_implementation_worker_and_main_session_display_separate_efforts(workflow, t_mod, monkeypatch, capsys, effort, label):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    document = workflow.defaults()
    policy = workflow.select_model_stages(document['workflows']['standard'], ['implementation'])
    policy['implementation'] = {'model': 'implementation-model', 'effort': effort}
    document = workflow.update(document, policy, name='standard')
    rows = t_mod._workflow_model_rows(vars(workflow), policy)
    assert rows == [('Planning', 'implementation-model', 'Medium', 'Main model'),
                    ('Implementation', 'implementation-model', label, 'Worker'),
                    ('Review', 'implementation-model', 'Medium', 'Main model')]
    t_mod._workflow_print(vars(workflow), {'scope': 'global', 'source': 'global',
        'workflow_name': 'standard', 'excluded': False, 'workflow': policy,
        'propagation': {'runtime': 'pending'}})
    output = capsys.readouterr().out
    review = '\n'.join(t_mod._workflow_review(document))
    for text in (output, review):
        assert 'Main session: implementation-model · Medium' in text
        assert label in text
        assert 'starts' not in text
    policy['implementation']['effort'] = 'high'
    fixed_rows = t_mod._workflow_model_rows(vars(workflow), policy)
    assert fixed_rows[1] == ('Implementation', 'implementation-model', 'High', 'Main session')


@pytest.mark.parametrize('scope', ['named', 'repository'])
@pytest.mark.parametrize('selected', [[], ['planning'], ['implementation'], ['review'],
    ['planning', 'implementation'], ['planning', 'review'], ['implementation', 'review'],
    ['planning', 'implementation', 'review']])
def test_stage_checkboxes_select_independent_helpers(workflow, t_mod, scope, selected):
    cfg = SimpleNamespace(repos={'api': '/repo'}, _workflow_document=workflow.defaults())
    original = copy.deepcopy(cfg._workflow_document)
    ui = InspectMenu(['stages_enabled', selected, 'back'])
    target = {'name': 'standard'} if scope == 'named' else {'repository': '/repo'}
    t_mod._workflow_policy_menu(ui, cfg, vars(workflow), **target)
    policy = (cfg._workflow_document['workflows']['standard'] if scope == 'named' else
              workflow.effective(cfg._workflow_document, '/repo'))
    assert workflow.model_stages(policy) == selected
    picker = ui.views[1]
    assert picker[0] == 'Model settings for'
    assert picker[1] == [('planning', 'Planning'), ('implementation', 'Implementation'), ('review', 'Review')]
    assert picker[2] == {'multi': True, 'marked': {0, 1, 2}}
    rows = dict(ui.views[-1][1])
    values = ui.views[-1][2]['values']
    assert ('implementation' in rows) is ('implementation' in selected)
    assert ('main_model' in rows) is ('implementation' not in selected)
    assert ('main_effort' in rows) is ('implementation' not in selected)
    assert ('planning' in rows) is ('planning' in selected)
    assert ('review' in rows) is ('review' in selected)
    assert values['stages_enabled'] == (', '.join(stage.title() for stage in selected) if selected else 'No · one model')
    for stage in ('planning', 'implementation', 'review'):
        assert policy[stage] == original['workflows']['standard'][stage]
    if scope == 'repository':
        assert cfg._workflow_document['workflows'] == original['workflows']
    assert not workflow.config_path().exists()


@pytest.mark.parametrize('selected', [None, ['planning', 'implementation', 'review']])
@pytest.mark.parametrize('scope', ['named', 'repository'])
def test_stage_checkboxes_cancel_and_unchanged_do_not_create_overrides(workflow, t_mod, selected, scope):
    cfg = SimpleNamespace(repos={'api': '/repo'}, _workflow_document=workflow.defaults())
    initial = copy.deepcopy(cfg._workflow_document)
    target = {'name': 'standard'} if scope == 'named' else {'repository': '/repo'}
    t_mod._workflow_policy_menu(Menu(['stages_enabled', selected, 'back']), cfg, vars(workflow), **target)
    assert cfg._workflow_document == initial
    assert not workflow.config_path().exists()


def test_stage_checkboxes_retain_choices_and_keep_main_work_editable(workflow, t_mod):
    document = workflow.defaults()
    policy = document['workflows']['standard']
    policy['planning'] = {'model': 'planning-only', 'effort': 'high'}
    policy['review'] = {'model': 'review-only', 'effort': 'low'}
    policy = workflow.select_model_stages(policy, ['implementation', 'review'])
    document = workflow.update(document, policy, name='standard')
    cfg = SimpleNamespace(repos={}, _workflow_document=document)
    ui = InspectMenu(['implementation', 'effort', 'high', 'stages_enabled', [],
                      'stages_enabled', ['planning'], 'back'])
    t_mod._workflow_policy_menu(ui, cfg, vars(workflow), name='standard')
    policy = cfg._workflow_document['workflows']['standard']
    assert policy['implementation']['effort'] == 'high'
    assert policy['planning'] == {'model': 'planning-only', 'effort': 'high'}
    assert policy['review'] == {'model': 'review-only', 'effort': 'low'}
    assert workflow.model_stages(policy) == ['planning']
    pickers = [view for view in ui.views if view[0] == 'Model settings for']
    assert pickers[0][2]['marked'] == {1, 2}
    assert pickers[1][2]['marked'] == set()
    reopened = InspectMenu(['stages_enabled', None, 'back'])
    t_mod._workflow_policy_menu(reopened, cfg, vars(workflow), name='standard')
    assert reopened.views[1][2]['marked'] == {0}


@pytest.mark.parametrize('scope', ['named', 'repository'])
def test_helpers_without_main_stage_edit_session_defaults(workflow, t_mod, scope):
    document = workflow.defaults()
    policy = workflow.select_model_stages(document['workflows']['standard'], ['planning', 'review'])
    document = workflow.update(document, policy, name='standard')
    cfg = SimpleNamespace(repos={'api': '/repo'}, agent_default='codex', _workflow_document=document,
                          models={'codex': 'account-model'}, efforts={'codex': 'low'})
    cfg._workflow_session_catalogs = {'codex': [dict(id='session-main', resolved='session-main',
        label='session-main', efforts=['low', 'high'], fast=False, ultrafast=False)]}
    target = {'name': 'standard'} if scope == 'named' else {'repository': '/repo'}
    ui = InspectMenu(['main_model', 'session-main', 'main_effort', 'high', 'back'])
    t_mod._workflow_policy_menu(ui, cfg, vars(workflow), **target)
    actual = (cfg._workflow_document['workflows']['standard'] if scope == 'named' else
              workflow.effective(cfg._workflow_document, '/repo'))
    assert actual['session_defaults'] == {'codex': {'model': 'session-main', 'effort': 'high'}}
    assert actual['implementation'] == policy['implementation']
    rows = dict(ui.views[0][1])
    assert 'planning' in rows and 'review' in rows and 'implementation' not in rows
    assert 'main_model' in rows and 'main_effort' in rows
    assert workflow.model_stages(actual) == ['planning', 'review']
    assert cfg.models == {'codex': 'account-model'} and cfg.efforts == {'codex': 'low'}
    assert not workflow.config_path().exists()


@pytest.mark.parametrize('defaults', [{}, {'codex': {'model': 'session-main', 'effort': 'high'}}])
def test_deselected_main_stage_never_displays_retained_main_model(workflow, t_mod, monkeypatch, capsys, defaults):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    document = workflow.defaults()
    policy = workflow.select_model_stages(document['workflows']['standard'], ['planning'])
    policy['implementation'] = {'model': 'inactive-implementation', 'effort': 'ultra'}
    if defaults:
        policy['session_defaults'] = defaults
    document = workflow.update(document, policy, name='standard')
    rows = t_mod._workflow_model_rows(vars(workflow), policy)
    expected_model = 'session-main' if defaults else 'Agent default'
    expected_effort = 'High' if defaults else 'Session default'
    assert rows[1] == ('Implementation', expected_model, expected_effort, 'Main model')
    assert rows[2] == ('Review', expected_model, expected_effort, 'Main model')
    assert rows[0][3] == 'Worker'
    t_mod._workflow_print(vars(workflow), {'scope': 'global', 'source': 'global',
        'workflow_name': 'standard', 'excluded': False, 'workflow': policy,
        'propagation': {'runtime': 'pending'}})
    output = capsys.readouterr().out
    review = '\n'.join(t_mod._workflow_review(document))
    for text in (output, review):
        assert 'inactive-implementation' not in text and 'Ultra' not in text
        assert expected_model in text and expected_effort in text
    assert t_mod._workflow_effort('auto', 'planning') == 'Agent selects by task'
    assert t_mod._workflow_effort('auto', 'implementation') == 'Agent selects by task'


@pytest.mark.parametrize('selected, inherited', [(['planning'], 'review'), (['review'], 'planning')])
def test_partial_stage_list_and_review_use_main_values(workflow, t_mod, monkeypatch, capsys, selected, inherited):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    document = workflow.defaults()
    policy = document['workflows']['standard']
    policy['planning'] = {'model': 'planning-private', 'effort': 'high'}
    policy['review'] = {'model': 'review-private', 'effort': 'low'}
    policy['implementation'] = {'model': 'main-active', 'effort': 'auto'}
    policy = workflow.select_stages(policy, selected)
    document = workflow.update(document, policy, name='standard')
    t_mod._workflow_print(vars(workflow), {'scope': 'global', 'source': 'global',
        'workflow_name': 'standard', 'excluded': False, 'workflow': policy,
        'propagation': {'runtime': 'pending'}})
    output = capsys.readouterr().out
    assert inherited + '-private' not in output
    assert selected[0] + '-private' in output
    assert 'Main model' in output and 'Main session: main-active · Medium' in output
    assert 'starts Medium' not in output
    for scope in [document, workflow.update(workflow.defaults(), policy, '/repo')]:
        review = '\n'.join(t_mod._workflow_review(scope))
        if scope is not document:
            review = review.split('Repository /repo:', 1)[1]
        assert inherited + '-private' not in review
        assert inherited.title() + ': main-active · Medium · Main model' in review
        assert 'Main session: main-active · Medium' in review
        assert 'starts Medium' not in review


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
    menu = Menu(['stages_enabled', [], 'main_model', 'repo-model', 'main_effort', 'high', 'back'])
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
    t_mod._workflow_policy_menu(Menu(['stages_enabled', ['planning', 'implementation', 'review'], 'back']), cfg, vars(workflow), repository='/repo')
    assert workflow.effective(cfg._workflow_document, '/repo')['session_defaults'] == policy['session_defaults']
    t_mod._workflow_policy_menu(Menu(['stages_enabled', [], 'main_model', '', 'back']), cfg, vars(workflow), repository='/repo')
    assert 'session_defaults' not in workflow.effective(cfg._workflow_document, '/repo')


def test_development_flow_keeps_section_focus_without_chatter(workflow, t_mod):
    class Inspect(Menu):
        def __init__(self):
            super().__init__(['pull_requests', 'pr_state', 'draft', 'back',
                              'local_preview', 'local_preview', 'when_ready',
                              'implement_before_approval', 'no', 'back',
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
    assert result['behaviors'] == {'pr_state': 'draft', 'local_preview': 'when_ready',
                                   'implement_before_approval': 'no', 'test_timing': 'pr_ci'}
    assert 'behaviors' not in policy
    first = ui.views[0]
    assert first[0] == 'PRs, previews & tests'
    assert first[2]['values']['pull_requests'] == first[2]['values']['tests'] == 'Not set'
    assert 'Frontend: Enabled' in first[2]['values']['local_preview'] and 'CLI: Enabled' in first[2]['values']['local_preview']
    assert 'Inherit · Not set' in first[2]['values']['local_preview']
    assert dict(first[1]) == {'pull_requests': 'Pull requests ›', 'local_preview': 'Local preview ›',
                              'tests': 'Tests ›', 'back': 'Back'}
    assert dict(ui.views[2][1])['draft'] == 'Draft'
    assert ui.views[3][2]['default'] == 2
    assert dict(ui.views[5][1]) == {'local_preview': 'When to preview',
                                   'implement_before_approval': 'Implement before approval', 'frontend': 'Frontend ›', 'cli': 'CLI ›', 'back': 'Back'}
    assert dict(ui.views[6][1]) == {'': 'Not set', 'when_ready': 'Automatically when ready', 'on_request': 'Only when I ask'}
    assert dict(ui.views[8][1]) == {'': 'Not set', 'yes': 'Yes', 'no': 'No'}
    assert ui.views[10][2]['default'] == 1
    assert ui.views[-1][2]['default'] == 2
    assert ui.context == []
