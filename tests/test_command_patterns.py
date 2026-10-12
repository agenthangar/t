"""The public command inventory is executable contract data for CLI lint."""

import contextlib
import io
import json
from pathlib import Path

import pytest

from test_cli_contract import lint_module


ROOT = Path(__file__).resolve().parent.parent
CONTRACT = json.loads((ROOT / "ui/command-contract.json").read_text())


def _cases():
    for command, entry in CONTRACT["commands"].items():
        for mode, spec in entry["modes"].items():
            yield pytest.param(command, mode, spec, id=f"{command.replace(' ', '-')}-{mode}")


def test_declared_commands(t_mod):
    lint = lint_module()
    assert lint.check_command_patterns(t_mod.build_parser(), t_mod.COMMAND_GROUPS, CONTRACT) == []
    assert set(CONTRACT["commands"]) == {
        f"{group} {verb}" for group, verbs in t_mod.COMMAND_GROUPS.items() for verb in verbs}


@pytest.mark.parametrize("command,mode,spec", list(_cases()))
def test_declared_modes(t_mod, command, mode, spec):
    parser = t_mod.build_parser()
    assert spec["argv"][:2] == command.split()
    assert t_mod._canonical_error(spec["argv"]) is None
    with contextlib.redirect_stderr(io.StringIO()):
        parser.parse_args(spec["argv"])
    assert spec["patterns"] and set(spec["patterns"]) <= set(CONTRACT["patterns"])
    assert isinstance(spec["read_only"], bool)
    assert t_mod._command_readonly(spec["argv"], terminal=spec.get("terminal", True)) == spec["read_only"]


def test_inventory_lint_rejects_missing_routes_modes_and_test_references(t_mod):
    lint = lint_module()
    parser = t_mod.build_parser()
    changed = json.loads(json.dumps(CONTRACT))
    del changed["commands"]["issue list"]
    assert any("missing issue list" in error for error in
               lint.check_command_patterns(parser, t_mod.COMMAND_GROUPS, changed))

    changed = json.loads(json.dumps(CONTRACT))
    changed["commands"]["issue list"]["modes"]["json"]["patterns"] = ["surprise"]
    assert any("unknown" in error for error in
               lint.check_command_patterns(parser, t_mod.COMMAND_GROUPS, changed))

    changed = json.loads(json.dumps(CONTRACT))
    changed["commands"]["issue list"]["modes"]["json"]["argv"] = ["issue", "list", "--codex"]
    assert any("does not parse" in error for error in
               lint.check_command_patterns(parser, t_mod.COMMAND_GROUPS, changed))

    changed = json.loads(json.dumps(CONTRACT))
    changed["commands"]["issue list"]["tests"] = ["tests/test_issue_list.py::test_absent"]
    assert any("missing command contract test" in error for error in
               lint.check_command_patterns(parser, t_mod.COMMAND_GROUPS, changed))

    changed = json.loads(json.dumps(CONTRACT))
    changed["commands"]["issue list"]["modes"].pop("json")
    assert any("lacks a conditional mode for --json" in error for error in
               lint.check_command_patterns(parser, t_mod.COMMAND_GROUPS, changed))

    changed = json.loads(json.dumps(CONTRACT))
    changed["commands"]["issue list"]["tests"] = [
        "tests/test_command_patterns.py::test_declared_commands"]
    assert any("inventory-only test" in error for error in
               lint.check_command_patterns(parser, t_mod.COMMAND_GROUPS, changed))


def test_picker_boundary_lint_rejects_bypasses_and_control_overrides(tmp_path):
    lint = lint_module()
    (tmp_path / "zsh").mkdir()
    (tmp_path / "libexec").mkdir()
    (tmp_path / "bin").mkdir()
    (tmp_path / "zsh" / "good.zsh").write_text("rows | _t_fzf_multi open legend --prompt='pick> '\n")
    (tmp_path / "libexec" / "t_issue.py").write_text("# no picker here\n")
    (tmp_path / "bin" / "t").write_text("# fzf is optional\n")
    assert lint.check_picker_boundaries(root=tmp_path) == []
    (tmp_path / "zsh" / "bad.zsh").write_text(
        "rows | command fzf --multi\nrows | _t_fzf --prompt=x\n"
        "rows | _t_fzf_multi open legend --bind=x\n"
        "rows | _t_fzf_single open legend --no-header\n")
    errors = lint.check_picker_boundaries(root=tmp_path)
    assert any("launches fzf" in error for error in errors)
    assert any("bypasses a picker profile" in error for error in errors)
    assert any("reserved picker controls" in error for error in errors)


def test_picker_boundary_lint_scans_python_entrypoints_without_prose_false_positives(tmp_path):
    lint = lint_module()
    (tmp_path / "bin").mkdir()
    (tmp_path / "libexec").mkdir()
    (tmp_path / "bin" / "t").write_text(
        "import subprocess\n"
        "description = 'fzf nor curses is required'\n"
        "subprocess.run(['fzf', '--version'])\n"
        "subprocess.run(['fzf', '--multi'])\n")
    (tmp_path / "libexec" / "t_other.py").write_text(
        "import subprocess\n"
        "subprocess.Popen(['fzf', '--height=40%'])\n"
        "subprocess.run(['sh', '-c', 'rows | fzf --multi'])\n")
    errors = lint.check_picker_boundaries(root=tmp_path)
    assert len(errors) == 3
    assert any("bin/t:4 launches fzf" in error for error in errors)
    assert any("libexec/t_other.py:2 launches fzf" in error for error in errors)
    assert any("libexec/t_other.py:3 launches fzf" in error for error in errors)


def test_group_help_lint_rejects_missing_action(t_mod):
    lint = lint_module()
    assert lint.check_group_help(t_mod._group_help, t_mod.COMMAND_GROUPS) == []
    def missing_list(group):
        menu = t_mod._group_help(group)
        return menu.replace("  list       List live sessions\n", "") if group == "session" else menu
    assert any("session list" in error for error in
               lint.check_group_help(missing_list, t_mod.COMMAND_GROUPS))
