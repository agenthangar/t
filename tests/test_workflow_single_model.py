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
    ui = InspectMenu(['local_preview', 'local_preview', None, 'preview_approval', None, 'back', 'back'])
    assert t_mod._workflow_behaviors_menu(ui, vars(workflow), policy) == initial
    assert policy == initial
    assert ui.views[1][2]['values']['local_preview'] == 'Automatically'
    assert ui.views[1][2]['values']['preview_approval'] == 'Required'
    assert 'before_pr' not in dict(ui.views[2][1])


def test_pr_open_choices_distinguish_timing_and_explicit_request(workflow, t_mod):
    ui = InspectMenu(['pull_requests', 'pr_open', 'on_request', 'back', 'back'])
    policy = t_mod._workflow_behaviors_menu(ui, vars(workflow), copy.deepcopy(workflow.DEFAULT))
    assert dict(ui.views[1][1])['pr_open'] == 'When to open PR'
    assert ui.views[2][0] == 'When to open PR'
    assert dict(ui.views[2][1]) == {'': 'Not set', 'automatic': 'Automatically when ready',
                                   'on_request': 'Only when I ask'}
    assert policy['behaviors']['pr_open'] == 'on_request'
    assert t_mod._workflow_choice(vars(workflow), 'local_preview', 'on_request') == 'When I ask'


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
    assert picker[1] == [('planning', 'Planning'), ('implementation', 'Main work'), ('review', 'Review')]
    assert picker[2] == {'multi': True, 'marked': {0, 1, 2}}
    rows = dict(ui.views[-1][1])
    values = ui.views[-1][2]['values']
    assert ('implementation' in rows) is ('implementation' in selected)
    assert ('main_model' in rows) is ('implementation' not in selected)
    assert ('main_effort' in rows) is ('implementation' not in selected)
    assert ('planning' in rows) is ('planning' in selected)
    assert ('review' in rows) is ('review' in selected)
    assert values['stages_enabled'] == (', '.join('Main work' if stage == 'implementation' else stage.title()
                                                for stage in selected) if selected else 'No · one model')
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
    assert rows[1] == ('Main work', expected_model, expected_effort, 'Main model')
    assert rows[2] == ('Review', expected_model, expected_effort, 'Main model')
    assert rows[0][3] == 'Selected stage'
    t_mod._workflow_print(vars(workflow), {'scope': 'global', 'source': 'global',
        'workflow_name': 'standard', 'excluded': False, 'workflow': policy,
        'propagation': {'runtime': 'pending'}})
    output = capsys.readouterr().out
    review = '\n'.join(t_mod._workflow_review(document))
    for text in (output, review):
        assert 'inactive-implementation' not in text and 'Ultra' not in text
        assert expected_model in text and expected_effort in text
    assert t_mod._workflow_effort('auto', 'planning') == 'Agent selects by task'
    assert t_mod._workflow_effort('auto', 'implementation') == 'Agent selects by task · starts Medium'


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
    assert 'Main model' in output and 'Agent selects by task · starts Medium' in output
    for scope in [document, workflow.update(workflow.defaults(), policy, '/repo')]:
        review = '\n'.join(t_mod._workflow_review(scope))
        if scope is not document:
            review = review.split('Repository /repo:', 1)[1]
        assert inherited + '-private' not in review
        assert inherited.title() + ': main-active · Agent selects by task · starts Medium · Main model' in review


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
    assert 'Planning:' not in review and 'Main work:' not in review and 'Review:' not in review
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
                              'preview_approval', 'required', 'back',
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
                                   'preview_approval': 'required', 'test_timing': 'pr_ci'}
    assert 'behaviors' not in policy
    first = ui.views[0]
    assert first[0] == 'PRs, previews & tests'
    assert set(first[2]['values'].values()) == {'Not set'}
    assert dict(first[1]) == {'pull_requests': 'Pull requests ›', 'local_preview': 'Local preview ›',
                              'frontend_design': 'Frontend design ›', 'tests': 'Tests ›', 'back': 'Back'}
    assert dict(ui.views[2][1])['draft'] == 'Draft'
    assert ui.views[3][2]['default'] == 1
    assert dict(ui.views[5][1]) == {'local_preview': 'Preview', 'preview_approval': 'Approval before PR',
                                   'cli_preview': 'CLI preview', 'frontend_preview': 'Update preview', 'back': 'Back'}
    assert dict(ui.views[6][1]) == {'': 'Not set', 'when_ready': 'Automatically', 'on_request': 'When I ask'}
    assert dict(ui.views[8][1]) == {'': 'Not set', 'required': 'Required', 'not_required': 'Not required'}
    assert ui.views[10][2]['default'] == 1
    assert ui.views[-1][2]['default'] == 3
    assert ui.context == []
