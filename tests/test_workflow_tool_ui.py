"""Tool-scoped workflow transactions use native choices without changing launch tools."""
import copy
import json
from types import SimpleNamespace

import pytest

from test_config import InterruptMenu, config_cli  # noqa: F401 -- isolated config fixture
from test_workflow import Menu, workflow
from test_tui import make_ui


class ToolMenu(Menu):
    def __init__(self, picks, **kwargs):
        super().__init__(picks, **kwargs)
        self.views, self.messages = [], []

    def pick(self, label, rows, **kwargs):
        self.views.append((label, rows, kwargs))
        return super().pick(label, rows, **kwargs)

    def done(self, label, value):
        self.messages.append((label, value))


def config(workflow):
    document = workflow.defaults()
    return SimpleNamespace(repos={'api': '/repo'}, agents={'api': 'claude'}, agent_default='codex',
        models={}, efforts={}, _workflow_document=document, _workflow_initial=copy.deepcopy(document),
        _workflow_before=None, _workflow_activate=False)


def test_tool_hub_browsing_does_not_materialize_native_scopes_or_change_launch_tool(workflow, t_mod, monkeypatch):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    cfg = config(workflow)
    before = copy.deepcopy(cfg._workflow_document)
    ui = ToolMenu(['claude', 'back', 'cursor', 'customize', 'back', 'back', 'back'])
    assert t_mod._workflow_hub(ui, cfg) is None
    assert ui.views[0][1][:3] == [('codex', 'Codex ›'), ('claude', 'Claude ›'), ('cursor', 'Cursor ›')]
    assert {key: ui.views[0][2]['values'][key] for key in ('codex', 'claude', 'cursor')} == {
        'codex': 'Not configured', 'claude': 'Not configured', 'cursor': 'Not configured'}
    assert any(label == 'Claude workflows' for label, _, _ in ui.views)
    cursor = next(view for view in ui.views if view[0] == 'Workflow · standard')
    assert 'main_model' in dict(cursor[1]) and 'main_effort' not in dict(cursor[1])
    assert 'permissions' not in dict(cursor[1])
    assert cfg._workflow_document == before
    roots = [view for view in ui.views if view[0] == 'Workflows · tools']
    assert roots[1][2]['default'] == 1 and roots[-1][2]['default'] == 2
    assert cfg.agent_default == 'codex' and cfg.agents == {'api': 'claude'}
    assert not hasattr(cfg, '_workflow_tool')
    assert not workflow.config_path().exists()


def test_tool_hub_changes_only_selected_catalogue_and_reviews_only_that_tool(workflow, t_mod, monkeypatch):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    cfg = config(workflow)
    before = copy.deepcopy(cfg._workflow_document)
    ui = ToolMenu(['claude', 'customize', 'behaviors', 'pull_requests', 'pr_review', 'skip',
                   'back', 'back', 'back', 'save'])
    assert t_mod._workflow_hub(ui, cfg, standalone=True) == 'save'
    assert workflow.tool_document(cfg._workflow_document, 'codex') == workflow.tool_document(before, 'codex')
    claude = workflow.tool_document(cfg._workflow_document, 'claude')
    assert claude['workflows']['standard']['behaviors']['pr_review'] == 'skip'
    assert 'cursor' not in cfg._workflow_document.get('tools', {})
    assert cfg._workflow_initial == before and cfg.agent_default == 'codex'
    review = '\n'.join(t_mod._workflow_review(cfg._workflow_document, before))
    assert 'Claude workflows' in review and 'Agent review before PR' in review
    assert 'Codex workflows' not in review and 'Cursor workflows' not in review
    assert not workflow.config_path().exists()


def test_tool_interrupt_merges_completed_nested_edit_into_full_transaction(workflow, t_mod, monkeypatch):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    cfg = config(workflow)
    before = copy.deepcopy(cfg._workflow_document)
    ui = InterruptMenu(['claude', 'customize', 'behaviors', 'pull_requests', 'pr_review', 'skip', KeyboardInterrupt])
    with pytest.raises(KeyboardInterrupt):
        t_mod._workflow_hub(ui, cfg)
    assert workflow.tool_document(cfg._workflow_document, 'claude')['workflows']['standard']['behaviors']['pr_review'] == 'skip'
    assert workflow.tool_document(cfg._workflow_document, 'codex') == workflow.tool_document(before, 'codex')
    assert cfg._workflow_initial == before
    assert not workflow.config_path().exists()


def test_tool_permissions_offer_only_selected_native_tool(workflow, t_mod, monkeypatch):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    cfg = config(workflow)
    before = copy.deepcopy(cfg._workflow_document)
    ui = ToolMenu(['claude', 'customize', 'permissions', 'claude', 'manual', 'back', 'back', 'save'])
    assert t_mod._workflow_hub(ui, cfg) == 'save'
    permissions = next(view for view in ui.views if view[0] == 'Permissions')
    assert permissions[1] == [('claude', 'Claude'), ('back', 'Back')]
    assert workflow.tool_document(cfg._workflow_document, 'claude')['workflows']['standard']['permissions'] == {'claude': 'manual'}
    assert workflow.tool_document(cfg._workflow_document, 'codex') == workflow.tool_document(before, 'codex')


def test_repository_workflow_tool_chooser_edits_only_selected_tool(workflow, t_mod, monkeypatch):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    cfg = config(workflow)
    before = copy.deepcopy(cfg._workflow_document)
    ui = ToolMenu(['claude', 'customize', 'behaviors', 'pull_requests', 'pr_review', 'skip',
                   'back', 'back', 'back', 'back', 'back'])
    t_mod._config_workflow(ui, cfg, 'api')
    assert ui.views[0][0] == 'Workflow tool · api'
    assert ui.views[-1][0] == 'Workflow tool · api' and ui.views[-1][2]['default'] == 1
    claude = workflow.tool_document(cfg._workflow_document, 'claude')
    assert workflow.effective(claude, '/repo')['behaviors']['pr_review'] == 'skip'
    assert workflow.tool_document(cfg._workflow_document, 'codex') == workflow.tool_document(before, 'codex')
    assert cfg.agents == {'api': 'claude'} and cfg.agent_default == 'codex'


@pytest.mark.parametrize('other_tool_changed', [False, True])
def test_tool_hub_revert_clears_own_pending_but_preserves_other_tool_pending(workflow, t_mod, monkeypatch, other_tool_changed):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    cfg = config(workflow)
    claude = workflow.tool_document(cfg._workflow_document, 'claude')
    claude['workflows']['standard']['instructions'] = 'Existing Claude preferences.'
    cfg._workflow_document = workflow.with_tool_document(cfg._workflow_document, 'claude', claude)
    cfg._workflow_initial = copy.deepcopy(cfg._workflow_document)
    if other_tool_changed:
        cfg._workflow_document['workflows']['standard']['behaviors'] = {'pr_open': 'automatic'}
    expected = copy.deepcopy(cfg._workflow_document)
    ui = ToolMenu(['claude', 'customize', 'behaviors', 'pull_requests', 'pr_open', 'automatic',
        'back', 'back', 'back', 'back', 'claude', 'customize', 'behaviors', 'pull_requests', 'pr_open', '',
        'back', 'back', 'back', 'back', 'back'])
    assert t_mod._workflow_hub(ui, cfg) is None
    tool_views = [view for view in ui.views if view[0] == 'Claude workflows']
    assert tool_views[-1][2]['values']['save'] == ('pending changes' if other_tool_changed else 'no changes')
    assert cfg._workflow_document == expected
    assert bool(t_mod._workflow_pending(cfg)) is other_tool_changed
    assert not workflow.config_path().exists()


@pytest.mark.parametrize('other_tool_changed', [False, True])
def test_reverting_new_native_scope_restores_absence_and_preserves_other_edits(workflow, t_mod, monkeypatch, other_tool_changed):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    cfg = config(workflow)
    cfg._workflow_document = workflow.WorkflowDocument(cfg._workflow_document)
    cfg._workflow_document.legacy_snapshots = {'/native/settings': ('original bytes', 0o600)}
    cfg._workflow_initial = copy.deepcopy(cfg._workflow_document)
    if other_tool_changed:
        cfg._workflow_document['workflows']['standard']['behaviors'] = {'pr_open': 'automatic'}
    expected = copy.deepcopy(cfg._workflow_document)
    ui = ToolMenu(['claude', 'customize', 'behaviors', 'pull_requests', 'pr_open', 'automatic',
        'back', 'back', 'back', 'back', 'claude', 'customize', 'behaviors', 'pull_requests', 'pr_open', '',
        'back', 'back', 'back', 'back', 'back'])
    assert t_mod._workflow_hub(ui, cfg) is None
    assert cfg._workflow_document == expected
    assert 'tools' not in cfg._workflow_document
    assert cfg._workflow_document.legacy_snapshots == expected.legacy_snapshots
    assert bool(t_mod._workflow_pending(cfg)) is other_tool_changed
    assert not workflow.config_path().exists()


@pytest.mark.parametrize('screen', ['tools', 'catalogue', 'config'])
def test_save_footer_shortcut_works_without_duplicate_menu_row(workflow, t_mod, monkeypatch, config_cli, screen):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    cfg, local = config_cli
    cfg.agent_default = 'codex'
    original_text = local.read_text()
    keys = ['enter', 'j', 'enter', 's', 'enter'] if screen == 'config' else ['s']
    ui = make_ui(t_mod, keys)
    calls, pick = [], ui.pick
    def capture(label, rows, **kwargs):
        calls.append((label, rows, kwargs))
        return pick(label, rows, **kwargs)
    ui.pick = capture
    if screen == 'config':
        monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
        edited = t_mod._config_wizard(cfg)
        assert edited.agent_default != cfg.agent_default
        assert any('Review local settings' in line for frame in ui.frames for line in frame)
    elif screen == 'catalogue':
        result = t_mod._workflow_tool_scope(ui, cfg, 'claude', lambda scoped: t_mod._workflow_tool_hub(ui, scoped))
        assert result == 'save'
    else:
        assert t_mod._workflow_hub(ui, cfg) == 'save'
    assert calls and all('save' not in dict(rows) for _, rows, _ in calls)
    assert any(options.get('shortcuts') == {'s': 'save'} and options.get('primary', ())[:2] == ('s', 'SAVE CHANGES')
               for _, _, options in calls)
    assert all(sum('SAVE CHANGES' in line for line in frame) <= 1 for frame in ui.frames)
    assert local.read_text() == original_text and not workflow.config_path().exists()


def test_claude_stage_choices_come_from_claude_metadata(workflow, t_mod, monkeypatch):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    calls = []
    def models(tool):
        calls.append(tool)
        return [{'value': 'claude-native', 'supportedEffortLevels': ['low', 'high']}]
    monkeypatch.setattr(t_mod, '_config_live_models', models)
    cfg = config(workflow)
    ui = ToolMenu(['claude', 'customize', 'stages_enabled', ['implementation'], 'implementation', 'model',
        'claude-native', 'implementation', 'effort', 'low', 'back', 'back', 'back'])
    assert t_mod._workflow_hub(ui, cfg) is None
    assert calls == ['claude']
    picker = next(view for view in ui.views if view[0] == 'Implementation model')
    assert 'gpt-6-astra' not in dict(picker[1]) and 'gpt-6.1-sol' not in dict(picker[1])
    effort = next(view for view in ui.views if view[0] == 'Implementation reasoning effort')
    assert dict(effort[1]) == {'auto': 'Agent selects by task', 'low': 'Low', 'high': 'High'}
    policy = workflow.tool_document(cfg._workflow_document, 'claude')['workflows']['standard']
    assert policy['implementation'] == {'model': 'claude-native', 'effort': 'low'}
    assert cfg.agent_default == 'codex'


def test_cursor_offline_stage_model_supports_native_parameter_ids_without_effort(workflow, t_mod, monkeypatch):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    monkeypatch.setattr(t_mod, '_config_live_models', lambda tool: (_ for _ in ()).throw(ValueError('authentication unavailable')))
    cfg = config(workflow)
    native_id = 'composer[fast](mode=auto,max=true)'
    ui = ToolMenu(['cursor', 'customize', 'stages_enabled', ['implementation'], 'implementation', 'model',
                   '__custom__', 'back', 'back', 'back'], inputs=[native_id])
    assert t_mod._workflow_hub(ui, cfg) is None
    stage = next(view for view in ui.views if view[0] == 'Implementation · worker')
    assert stage[1] == [('model', 'Model'), ('back', 'Back')]
    picker = next(view for view in ui.views if view[0] == 'Implementation model')
    assert dict(picker[1]) == {'': 'Inherit · Tool default', '__custom__': 'Enter a custom model ID…', '__refresh__': 'Refresh model list'}
    assert any(label == 'unavailable' for label, _ in ui.messages)
    policy = workflow.tool_document(cfg._workflow_document, 'cursor')['workflows']['standard']
    assert policy['implementation']['model'] == native_id
    assert not workflow.config_path().exists()


@pytest.mark.parametrize('tool', ['codex', 'claude', 'cursor'])
def test_workflow_tool_flag_parser_and_read_scope(workflow, t_mod, monkeypatch, capsys, tool):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    monkeypatch.setattr(t_mod, '_t_auto_update', lambda argv: None)
    cfg = config(workflow)
    monkeypatch.setattr(t_mod, 'Config', lambda: cfg)
    assert t_mod.main(['workflow', 'list', '--tool', tool, '--json']) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['tool'] == tool
    assert result['workflow'] == workflow.effective(workflow.tool_defaults(tool))
    assert t_mod.build_parser().parse_args(['config', 'workflow', 'instructions', 'show', '--tool', tool]).workflow_tool == tool
    assert not workflow.config_path().exists()


def test_workflow_read_default_uses_explicit_repository_tool(workflow, t_mod, monkeypatch, capsys):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    cfg = config(workflow)
    args = SimpleNamespace(workflow=True, workflow_action='list', workflow_repo='api', json=True)
    assert t_mod._cmd_workflow(cfg, args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['tool'] == 'claude'
    assert result['source'] == 'recommended defaults (not saved)'
    assert result['workflow']['implementation']['model'] == ''


def test_workflow_tool_write_merges_only_selected_catalogue(workflow, t_mod, monkeypatch):
    full = workflow.defaults()
    claude = workflow.action(workflow.tool_document(full, 'claude'), 'add', 'fast')
    full = workflow.with_tool_document(full, 'claude', claude)
    before = copy.deepcopy(full)
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    monkeypatch.setattr(workflow, 'load', lambda: (copy.deepcopy(full), None))
    written = []
    monkeypatch.setattr(workflow, 'save', lambda document, *args, **kwargs: written.append(copy.deepcopy(document)))
    cfg = config(workflow)
    args = SimpleNamespace(workflow=True, workflow_action='use', workflow_name='fast', workflow_tool='claude')
    assert t_mod._cmd_workflow(cfg, args) == 0
    assert len(written) == 1
    assert workflow.tool_document(written[0], 'claude')['default'] == 'fast'
    assert workflow.tool_document(written[0], 'codex') == workflow.tool_document(before, 'codex')
    assert 'cursor' not in written[0].get('tools', {})
    assert full == before and not workflow.config_path().exists()


def test_tool_diff_formats_retained_other_tool_permissions_without_key_error(workflow, t_mod, monkeypatch):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    full = workflow.defaults()
    claude = workflow.tool_document(full, 'claude')
    claude['workflows']['standard']['permissions'] = {'codex': 'ask', 'claude': 'manual'}
    full = workflow.with_tool_document(full, 'claude', claude)
    changed = copy.deepcopy(full)
    changed['tools']['claude']['workflows']['standard']['permissions']['codex'] = 'full'
    review = '\n'.join(t_mod._workflow_review(changed, full))
    assert 'Claude workflows' in review and 'Full access' in review
    assert 'Codex workflows' not in review


def test_cursor_catalog_discovery_is_bounded_and_auth_failure_does_not_invent_models(t_mod, monkeypatch):
    monkeypatch.setattr(t_mod.shutil, 'which', lambda name: '/native/cursor-agent')
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(returncode=1, stdout='', stderr='Authentication required')
    monkeypatch.setattr(t_mod.subprocess, 'run', run)
    with pytest.raises(ValueError, match='authentication'):
        t_mod._config_live_models('cursor')
    assert calls == [(['/native/cursor-agent', '--list-models'], {'capture_output': True, 'text': True, 'timeout': 8})]

