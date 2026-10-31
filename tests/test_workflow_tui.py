"""Workflow TUI uses the config transaction and never publishes canceled edits."""
import copy
from pathlib import Path

import pytest

from test_config import Menu, InterruptMenu
from test_workflow import workflow, native_paths
from test_tui import make_ui


def config(workflow, t_mod, monkeypatch, tmp_path):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    monkeypatch.setattr(t_mod, 'CONFIG', str(tmp_path / 'absent'))
    monkeypatch.setattr(t_mod, 'zsh_capture', lambda _: '')
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(t_mod.sys.stdout, 'isatty', lambda: True)
    local = tmp_path / 'local.zsh'
    local.write_text('# unchanged\n')
    monkeypatch.setenv('T_LOCAL_RC', str(local))
    return t_mod.Config(), local


@pytest.mark.parametrize('save', [False, True])
@pytest.mark.parametrize('direct', [False, True])
def test_workflow_tui_save_discard(workflow, t_mod, monkeypatch, tmp_path, save, direct):
    cfg, local = config(workflow, t_mod, monkeypatch, tmp_path)
    def editor(argv):
        assert Path(argv[-1]).suffix == '.md'
        Path(argv[-1]).write_text('Try it locally before opening a PR.\n')
        return 0
    monkeypatch.setattr(workflow.subprocess, 'call', editor)
    picks = ([] if direct else ['workflow']) + ['customize', 'instructions', 'back']
    picks += ['save'] if save else ['back', *([] if direct else ['cancel']), 'discard']
    ui = Menu(picks)
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    args = t_mod.build_parser().parse_args(['config', 'workflows'] if direct else ['config'])
    assert t_mod.cmd_config(cfg, args) == (0 if save else 130)
    assert ui.restored and local.read_text() == '# unchanged\n'
    assert workflow.config_path().exists() is save
    if save:
        assert workflow.instructions(workflow.load()[0]) == 'Try it locally before opening a PR.\n'
    else:
        assert not any(path.exists() for path in native_paths())


def test_workflow_tui_route_and_nonterminal(workflow, t_mod, monkeypatch, tmp_path, capsys):
    cfg, local = config(workflow, t_mod, monkeypatch, tmp_path)
    monkeypatch.setattr(t_mod, 'Config', lambda: cfg)
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: False)
    monkeypatch.setattr(t_mod, '_RailUI', lambda: pytest.fail('nonterminal picker'))
    assert t_mod.main(['config', 'workflows']) == 1
    assert 'terminal' in capsys.readouterr().err
    assert not workflow.config_path().exists() and local.read_text() == '# unchanged\n'
    assert t_mod._canonical_argv(['config', 'workflows']) == ['config', '--workflows-ui']
    assert not t_mod._command_readonly(['config', 'workflows'])


@pytest.mark.parametrize('width,height', [(80, 24), (44, 20), (32, 12)])
def test_workflow_tui_narrow(workflow, t_mod, monkeypatch, tmp_path, width, height):
    cfg, _ = config(workflow, t_mod, monkeypatch, tmp_path)
    ui = make_ui(t_mod, ['j'] * 6 + ['k'] * 6 + ['q'], width, height)
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    assert t_mod._config_wizard(cfg, workflows_only=True) is None
    assert ui.restored and not workflow.config_path().exists()
    frames = [frame for frame in ui.frames if any('▸' in line for line in frame)]
    assert frames
    for frame in frames:
        assert sum(t_mod._term_rows(line, width) for line in frame) <= height
        assert all(t_mod._vis_len(line) <= width for line in frame)
        assert any('SAVE CHANGES' in line for line in frame)


def test_workflow_tui_review_back_and_keep(workflow, t_mod, monkeypatch, tmp_path):
    cfg, _ = config(workflow, t_mod, monkeypatch, tmp_path)
    ui = Menu(['customize', 'enabled', '0', 'back', 'save', 'back', 'keep', 'save'], pages=['n', 'y'])
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    changed = t_mod._config_wizard(cfg, workflows_only=True)
    assert not changed._workflow_document['workflows']['standard']['enabled']
    assert not workflow.config_path().exists()  # Wizard stages; cmd_config publishes.


def test_workflow_tui_add_assign_and_repository_scope(workflow, t_mod, monkeypatch, tmp_path):
    cfg, _ = config(workflow, t_mod, monkeypatch, tmp_path)
    cfg.repos = {'api': str(tmp_path / 'api')}
    ui = Menu(['add', 'back', 'choose', 'local', 'repos', 'api', 'exclude', 'back', 'name:standard', 'back', 'save'], inputs=['local'])
    assert t_mod._workflow_hub(ui, cfg, standalone=True) == 'save'
    assert cfg._workflow_document['default'] == 'local'
    assert cfg._workflow_document['repositories'][cfg.repos['api']]['excluded']
    assert not workflow.config_path().exists()


@pytest.mark.parametrize('terminal', [False, True])
def test_workflow_bare_entry(workflow, t_mod, monkeypatch, tmp_path, capsys, terminal):
    cfg, local = config(workflow, t_mod, monkeypatch, tmp_path)
    monkeypatch.setattr(t_mod, 'Config', lambda: cfg)
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: terminal)
    monkeypatch.setattr(t_mod.sys.stdout, 'isatty', lambda: terminal)
    monkeypatch.setattr(t_mod, '_t_auto_update', lambda argv: None)
    ui = Menu(['back'])
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui if terminal else pytest.fail('nonterminal picker'))
    assert t_mod.main(['workflow']) == (130 if terminal else 0)
    assert t_mod._command_readonly(['workflow'], terminal=terminal) is not terminal
    if not terminal:
        assert 'USAGE' in capsys.readouterr().out
    monkeypatch.setattr(t_mod, '_RailUI', lambda: pytest.fail('help opened picker'))
    assert t_mod.main(['workflow', '--help']) == 0
    assert 't workflow' in capsys.readouterr().out
    assert not workflow.config_path().exists() and local.read_text() == '# unchanged\n'


@pytest.mark.parametrize('menu,field,value', [
    (['behaviors', 'pull_requests', 'pr_open', 'automatic'], 'behaviors', {'pr_open': 'automatic'}),
    (['permissions', 'codex', 'full'], 'permissions', {'codex': 'full'}),
    (['implementation', 'effort', 'high'], 'implementation', {'model': 'gpt-6.1-sol', 'effort': 'high'}),
])
def test_workflow_ctrl_c_preserves_completed_nested_selections(workflow, t_mod, monkeypatch, tmp_path, menu, field, value):
    cfg, local = config(workflow, t_mod, monkeypatch, tmp_path)
    ui = InterruptMenu(['customize', *menu, KeyboardInterrupt, 'save'], pages=['enter'])
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    changed = t_mod._config_wizard(cfg, workflows_only=True)
    assert changed._workflow_document['workflows']['standard'][field] == value
    assert any('unsaved' in label for label in ui.labels)
    assert ui.restored and not workflow.config_path().exists()
    assert local.read_text() == '# unchanged\n'


def test_workflow_clean_ctrl_c_exits_without_unsaved_prompt(workflow, t_mod, monkeypatch, tmp_path):
    cfg, local = config(workflow, t_mod, monkeypatch, tmp_path)
    ui = InterruptMenu([KeyboardInterrupt])
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    assert t_mod.cmd_config(cfg, t_mod.build_parser().parse_args(['config', '--workflows-ui'])) == 130
    assert not any('unsaved' in label for label in ui.labels)
    assert ui.restored and not workflow.config_path().exists()
    assert local.read_text() == '# unchanged\n'


def test_config_real_abort_key_offers_review_without_publishing(workflow, t_mod, monkeypatch, tmp_path):
    cfg, local = config(workflow, t_mod, monkeypatch, tmp_path)
    cfg.open_mode_default = 'cli'
    ui = make_ui(t_mod, ['j', 'enter', 'j', 'enter', 'abort', 'j', 'enter', 'enter'])
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    changed = t_mod._config_wizard(cfg)
    assert changed.open_mode_default == 'app'
    assert any('unsaved' in line for frame in ui.frames for line in frame)
    assert any('Review local settings' in line for frame in ui.frames for line in frame)
    assert ui.restored and not workflow.config_path().exists()
    assert local.read_text() == '# unchanged\n'


def test_workflow_editor_interrupt_keeps_prior_staged_edits(workflow, t_mod, monkeypatch, tmp_path):
    cfg, local = config(workflow, t_mod, monkeypatch, tmp_path)
    def interrupted_editor(*args):
        raise KeyboardInterrupt
    monkeypatch.setattr(workflow, 'edit_instructions', interrupted_editor)
    ui = InterruptMenu(['customize', 'enabled', '0', 'instructions', 'save'], pages=['enter'])
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    changed = t_mod._config_wizard(cfg, workflows_only=True)
    assert not changed._workflow_document['workflows']['standard']['enabled']
    assert any('unsaved' in label for label in ui.labels)
    assert ui.restored and not workflow.config_path().exists()
    assert local.read_text() == '# unchanged\n'


def test_workflow_runner_interrupt_keeps_runner_and_behavior_edits(workflow, t_mod, monkeypatch, tmp_path):
    cfg, local = config(workflow, t_mod, monkeypatch, tmp_path)
    def interrupted_runners(ui, staged, helper, repository, name):
        policy = copy.deepcopy(staged._workflow_document['workflows'][name])
        policy['runners'] = copy.deepcopy(workflow.RUNNER_DEFAULT)
        policy['runners']['timeout_seconds'] = 1234
        staged._workflow_document = workflow.update(staged._workflow_document, policy, repository, name)
        raise KeyboardInterrupt
    monkeypatch.setattr(t_mod, '_workflow_runners_menu', interrupted_runners)
    ui = InterruptMenu(['customize', 'behaviors', 'pull_requests', 'pr_open', 'on_request',
                        'back', 'tests', 'runners', 'save'], pages=['enter'])
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    changed = t_mod._config_wizard(cfg, workflows_only=True)
    policy = changed._workflow_document['workflows']['standard']
    assert policy['behaviors']['pr_open'] == 'on_request'
    assert policy['runners']['timeout_seconds'] == 1234
    assert ui.restored and not workflow.config_path().exists()
    assert local.read_text() == '# unchanged\n'


@pytest.mark.parametrize('submenu', ['behaviors', 'permissions'])
def test_pristine_repository_submenu_interrupt_does_not_create_override(workflow, t_mod, monkeypatch, tmp_path, submenu):
    cfg, local = config(workflow, t_mod, monkeypatch, tmp_path)
    cfg.repos = {'api': str(tmp_path / 'api')}
    ui = InterruptMenu(['repos', 'api', 'customize', submenu, KeyboardInterrupt])
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    with pytest.raises(KeyboardInterrupt):
        t_mod._config_wizard(cfg, workflows_only=True)
    assert not any('unsaved' in label for label in ui.labels)
    assert cfg._workflow_document['repositories'] == {}
    assert ui.restored and not workflow.config_path().exists()
    assert local.read_text() == '# unchanged\n'
