"""Runner controls use the same CLI-tool catalogue as their workflow."""
import copy
import json
from types import SimpleNamespace

import pytest

from test_config import Menu
from test_workflow import workflow  # noqa: F401


@pytest.mark.parametrize('tool', ['codex', 'claude', 'cursor'])
def test_runner_list_selects_tool_without_writes(workflow, t_mod, monkeypatch, capsys, tool):
    document = workflow.defaults()
    for index, selected in enumerate(workflow.TOOLS):
        scoped = workflow.tool_document(document, selected)
        scoped['workflows']['standard']['runners'] = copy.deepcopy(workflow.RUNNER_DEFAULT)
        scoped['workflows']['standard']['runners']['timeout_seconds'] = 100 + index
        document = workflow.with_tool_document(document, selected, scoped)
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    monkeypatch.setattr(workflow, 'load', lambda: (copy.deepcopy(document), None))
    monkeypatch.setattr(workflow, 'save', lambda *a, **kw: pytest.fail('read wrote configuration'))
    args = t_mod.build_parser().parse_args(['runners', 'list', '--tool', tool, '--json'])
    assert t_mod._cmd_workflow_runners(SimpleNamespace(repos={}, agent_default='codex'), args) == 0
    result = json.loads(capsys.readouterr().out)
    assert result['tool'] == tool
    assert result['runners']['timeout_seconds'] == 100 + workflow.TOOLS.index(tool)


def test_config_runner_tool_picker_keeps_edits_in_selected_tool(workflow, t_mod, monkeypatch):
    monkeypatch.setattr(t_mod, '_workflow', lambda: vars(workflow))
    cfg = SimpleNamespace(repos={}, agent_default='codex')
    def edit(document, repository, name):
        policy = copy.deepcopy(document['workflows'][name])
        policy['runners'] = copy.deepcopy(workflow.RUNNER_DEFAULT)
        policy['runners']['timeout_seconds'] = 321
        return 0, workflow.update(document, policy, name=name)
    monkeypatch.setattr(workflow, 'edit_runners', edit)
    ui = Menu(['claude', 'global', 'edit', 'back', 'back', 'back'])
    t_mod._config_runners(ui, cfg)
    assert 'runners' not in cfg._workflow_document['workflows']['standard']
    claude = workflow.tool_document(cfg._workflow_document, 'claude')
    assert claude['workflows']['standard']['runners']['timeout_seconds'] == 321
    assert cfg.agent_default == 'codex' and t_mod._workflow_pending(cfg)
    assert not workflow.config_path().exists()


def test_runner_start_tool_flag_and_repository_default(t_mod):
    args = t_mod.build_parser().parse_args(['runners', 'runs', 'start', '--tool', 'cursor', '--', 'check'])
    cfg = SimpleNamespace(repos={'api': '/api'}, agents={'api': 'claude'}, agent_default='codex')
    assert t_mod._runner_tool(cfg, args, '/api') == 'cursor'
    args.workflow_tool = None
    assert t_mod._runner_tool(cfg, args, '/api') == 'claude'
    assert t_mod._runner_tool(cfg, args) == 'codex'
