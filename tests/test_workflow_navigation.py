"""Named editor entry, safe deletion, and retained nested workflow choices."""
import copy
from types import SimpleNamespace

import pytest

from test_workflow import workflow, Menu
from test_workflow_tui import config, HubMenu
from test_tui import make_ui


@pytest.mark.parametrize('prefix', [[], ['config']])
@pytest.mark.parametrize('tool', ['codex', 'claude', 'cursor'])
@pytest.mark.parametrize('save', [False, True])
def test_named_open_transaction(workflow, t_mod, monkeypatch, tmp_path, prefix, tool, save):
    cfg, local = config(workflow, t_mod, monkeypatch, tmp_path)
    cfg.agent_default = 'codex'
    full = workflow.defaults()
    scoped = workflow.action(workflow.tool_document(full, tool), 'add', 'fast')
    full = workflow.with_tool_document(full, tool, scoped)
    workflow.save(full, None)
    original = workflow.load()[0]
    picks = ['enabled', '0', 'back'] + (['save'] if save else ['back', 'back', 'discard'])
    ui = HubMenu(picks)
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    args = t_mod.build_parser().parse_args([*prefix, 'workflow', 'fast', '--tool', tool])
    assert args.workflow_action == 'open'
    assert t_mod._canonical_error([*prefix, 'workflow', 'fast', '--tool', tool]) is None
    assert t_mod.cmd_config(cfg, args) == (0 if save else 130)
    assert ui.views[0][0] == 'Workflow · fast'
    saved = workflow.load()[0]
    assert workflow.tool_document(saved, tool)['workflows']['fast']['enabled'] is not save
    for other in workflow.TOOLS:
        if other != tool:
            assert workflow.tool_document(saved, other) == workflow.tool_document(original, other)
    assert local.read_text() == '# unchanged\n' and ui.restored


def test_named_open_errors_and_collision(workflow, t_mod, monkeypatch, tmp_path, capsys):
    cfg, _ = config(workflow, t_mod, monkeypatch, tmp_path)
    cfg.agent_default = 'codex'
    monkeypatch.setattr(t_mod, '_RailUI', lambda: pytest.fail('must not open UI'))
    parser = t_mod.build_parser()
    assert t_mod.cmd_config(cfg, parser.parse_args(['workflow', 'missing'])) == 1
    assert 'unknown workflow' in capsys.readouterr().err
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: False)
    assert t_mod.cmd_config(cfg, parser.parse_args(['workflow', 'open', 'standard'])) == 1
    assert 'terminal' in capsys.readouterr().err
    assert not workflow.config_path().exists()
    assert parser.parse_args(['workflow', 'open', 'list']).workflow_name == 'list'
    assert parser.parse_args(['workflow', 'list']).workflow_action == 'list'
    assert not t_mod._command_readonly(['workflow', 'standard'])
    assert t_mod._command_readonly(['workflow', 'list'])


@pytest.mark.parametrize('tool', ['codex', 'claude', 'cursor'])
def test_delete_command(workflow, t_mod, monkeypatch, tmp_path, tool, capsys):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    cfg = SimpleNamespace(repos={}, hosts={}, agent_default='codex')
    full = workflow.defaults()
    doc = workflow.action(workflow.tool_document(full, tool), 'add', 'fast')
    doc = workflow.action(doc, 'use', 'fast')
    doc['repositories'] = {str(tmp_path / 'repo'): {'workflow': 'fast', 'excluded': True}}
    workflow.save(workflow.with_tool_document(full, tool, doc), None)
    before = workflow.load()[0]
    parser = t_mod.build_parser()
    def run(*words):
        return t_mod.cmd_config(cfg, parser.parse_args(['workflow', 'delete', *words, '--tool', tool]))
    assert run('fast') == 1
    assert 'replacement' in capsys.readouterr().err
    assert workflow.load()[0] == before
    assert run('fast', '--replacement', 'standard') == 0
    after = workflow.load()[0]
    scoped = workflow.tool_document(after, tool)
    assert scoped['default'] == 'standard' and 'fast' not in scoped['workflows']
    assert scoped['repositories'][str(tmp_path / 'repo')] == {'workflow': 'standard', 'excluded': True}
    for other in workflow.TOOLS:
        if other != tool:
            assert workflow.tool_document(after, other) == workflow.tool_document(before, other)
    assert run('standard') == 1
    assert 'last workflow' in capsys.readouterr().err
    assert workflow.load()[0] == after


def test_delete_validation(workflow):
    original = workflow.action(workflow.defaults(), 'add', 'fast')
    assert workflow.action(original, 'delete', 'fast') == workflow.defaults()
    for name, replacement, message in [('missing', None, 'unknown workflow'), ('fast', 'missing', 'replacement'),
                                       ('fast', 'fast', 'replacement'), ('standard', None, 'in use')]:
        before = copy.deepcopy(original)
        with pytest.raises(ValueError, match=message):
            workflow.action(original, 'delete', name, replacement=replacement)
        assert original == before


@pytest.mark.parametrize('replacement', [None, 'standard'])
@pytest.mark.parametrize('save', [False, True])
def test_delete_editor_transaction(workflow, t_mod, monkeypatch, tmp_path, replacement, save):
    cfg, _ = config(workflow, t_mod, monkeypatch, tmp_path)
    cfg.agent_default = 'codex'
    document = workflow.action(workflow.defaults(), 'add', 'fast')
    document = workflow.action(document, 'use', 'fast')
    workflow.save(document, None)
    original = workflow.load()[0]
    picks = ['delete', replacement] + (['back'] if replacement is None else [])
    picks += ['save'] if save else ['back', 'back', *(['discard'] if replacement else [])]
    ui = HubMenu(picks)
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    args = t_mod.build_parser().parse_args(['workflow', 'fast'])
    assert t_mod.cmd_config(cfg, args) == (0 if save else 130)
    after = workflow.load()[0]
    assert ('fast' not in after['workflows']) is (save and replacement is not None)
    if not (save and replacement):
        assert after == original
    assert ui.restored


def test_delete_menu_unassigned_and_last(workflow, t_mod, monkeypatch):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    cfg = SimpleNamespace(repos={}, _workflow_document=workflow.action(workflow.defaults(), 'add', 'fast'))
    t_mod._workflow_policy_menu(Menu(['delete']), cfg, vars(workflow), name='fast')
    assert 'fast' not in cfg._workflow_document['workflows']
    original = copy.deepcopy(cfg._workflow_document)
    t_mod._workflow_policy_menu(Menu(['delete', 'back']), cfg, vars(workflow), name='standard')
    assert cfg._workflow_document == original


@pytest.mark.parametrize('back', ['q', 'esc'])
@pytest.mark.parametrize('reset', [False, True])
def test_stage_marks_retained_on_back(workflow, t_mod, monkeypatch, back, reset):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    document = workflow.defaults()
    cfg = SimpleNamespace(repos={}, _workflow_document=document)
    stage_ui = make_ui(t_mod, (['space'] if reset else ['j', 'space']) + [back])
    outer = Menu(['stages_enabled', 'back'])
    pick = outer.pick
    outer.pick = lambda label, rows, **kw: stage_ui.pick(label, rows, **kw) if label == 'Use multiple models' else pick(label, rows, **kw)
    t_mod._workflow_policy_menu(outer, cfg, vars(workflow), name='standard')
    stages = workflow.model_stages(cfg._workflow_document['workflows']['standard'])
    assert stages == ([] if reset else ['planning', 'review'])
    assert not workflow.config_path().exists()


@pytest.mark.parametrize('width', [44, 160])
@pytest.mark.parametrize('selected', [False, True])
@pytest.mark.parametrize('as_value', [False, True])
def test_inherited_value_is_dim_after_clipping(t_mod, width, selected, as_value):
    ui = make_ui(t_mod, [], width)
    inherited = 'Inherit global · Automatically'
    lines = ui.row('Preview timing' if as_value else inherited, inherited if as_value else None, selected=selected)
    rendered = '\n'.join(lines)
    assert 'Inherit global · ' in rendered
    assert ui.st.y + 'Automatically' in rendered
    assert all(t_mod._vis_len(line) <= width for line in lines)
    if selected:
        assert ui.st.focus + ui.st.y + 'Automatically' in rendered
    plain = t_mod._ANSI_RE.sub('', rendered)
    ui.st = t_mod.Style(tty=False)
    ui.rail = '  '
    assert '\n'.join(ui.row('Preview timing' if as_value else inherited, inherited if as_value else None, selected=selected)) == plain


def test_launch_controls_are_per_type_and_preserve_legacy(workflow, t_mod):
    policy = {**copy.deepcopy(workflow.DEFAULT), 'behaviors': {'preview_launch': 'automatic', 'cli_launch': 'on_request'}}
    before = copy.deepcopy(policy)
    ui = HubMenu(['local_preview', 'frontend', 'frontend_browser', '', 'back', 'cli', 'back', 'back', 'back'])
    after = t_mod._workflow_behaviors_menu(ui, vars(workflow), policy)
    assert 'preview_launch' not in dict(ui.views[1][1])
    assert ui.views[2][2]['values']['frontend_browser'] == 'Inherited · Automatically'
    assert ui.views[3][1][0] == ('', 'Use default · Automatically')
    assert ui.views[3][1][ui.views[3][2]['default']][0] == ''
    assert after == before
    assert policy == before
    inherited = {**policy, 'behaviors': {'preview_launch': 'automatic'}}
    changed = workflow.update_behavior(inherited, 'cli_launch', 'on_request')
    assert changed['behaviors'] == {'frontend_browser': 'automatic', 'cli_launch': 'on_request'}
    assert workflow.update_behavior(changed, 'cli_launch', '')['behaviors'] == {'frontend_browser': 'automatic'}
    lines = workflow.preview_summary_lines(inherited['behaviors'])
    assert not any('Open/run preview' in line for line in lines)
    assert 'Frontend — Open in browser: Automatically' in lines
    assert 'CLI — Run command: Automatically' in lines
