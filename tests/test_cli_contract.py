"""The public issue commands keep explicit verbs and safe option boundaries."""

import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


def lint_module():
    spec = importlib.util.spec_from_file_location("cli_lint_test", ROOT / "scripts/lint_cli.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_issue_parser_accepts_verbs_and_separates_options(t_mod):
    parser = t_mod.build_parser()
    listed = parser.parse_args(["issue", "list", "api", "--state", "closed", "--json"])
    assert (listed.verb, listed.action, listed.repository, listed.repo, listed.state, listed.json) == (
        "issue", "list", "api", None, "closed", True)
    chosen = parser.parse_args(["issue", "list", "--repo", "owner/repo"])
    assert (chosen.repository, chosen.repo) == (None, "owner/repo")
    conflict = parser.parse_args(["issue", "list", "api", "--repo", "owner/repo"])
    assert (conflict.repository, conflict.repo) == ("api", "owner/repo")
    status = parser.parse_args(["issue", "status", "123", "--repo", "api", "--json"])
    assert (status.action, status.target, status.repo, status.json) == (
        "status", ["123"], "api", True)
    start = parser.parse_args(["issue", "start", "api", "123", "124", "--codex"])
    assert (start.action, start.target, start.codex) == ("start", ["api", "123", "124"], True)
    assert parser.parse_args(["issue", "start"]).target == []


@pytest.mark.parametrize("argv", [
    ["issue", "123"], ["issue", "list", "--codex"],
    ["issue", "status", "123", "--allow-incomplete-check"],
    ["issue", "start", "123", "--json"],
    ["issue", "start", "123", "--codex", "--claude"],
])
def test_issue_parser_rejects_bare_target_and_cross_action_flags(t_mod, argv):
    with pytest.raises(SystemExit) as exc:
        t_mod.build_parser().parse_args(argv)
    assert exc.value.code == 2


def test_issue_help_is_a_side_effect_free_verb_menu(t_mod, monkeypatch, capsys):
    monkeypatch.setattr(t_mod, "Config", lambda: pytest.fail("help loaded configuration"))
    monkeypatch.setattr(t_mod, "_t_auto_update", lambda argv: pytest.fail("help checked updates"))
    assert t_mod.main(["issue"]) == 0
    menu = capsys.readouterr().out
    assert all(f"t issue {verb}" in menu or verb in menu
               for verb in ("list", "status", "start"))
    assert t_mod.main(["issue", "--help"]) == 0
    assert capsys.readouterr().out == menu
    assert t_mod.main(["help", "issue", "status"]) == 0
    assert "usage: t issue status" in capsys.readouterr().out
    assert t_mod.main(["help", "issue", "start"]) == 0
    assert "usage: t issue start" in capsys.readouterr().out


def test_issue_help_shows_repository_resolution_on_every_verb(t_mod, capsys):
    menu = t_mod._group_help("issue")
    for syntax in ("t issue list [repo]", "t issue status [repo] <id>",
                   "t issue start [repo] [id ...]"):
        assert syntax in menu
    assert "owner/repo" in menu and "--repo" in menu
    lint = lint_module()
    assert lint.check_issue_help(menu) == []
    assert any("status [repo]" in error for error in lint.check_issue_help(
        menu.replace("t issue status [repo] <id>", "t issue status <id>")))
    for verb in ("list", "status", "start"):
        assert t_mod.main(["help", "issue", verb]) == 0
        help_text = capsys.readouterr().out
        assert "[repo]" in help_text and "--repo" in help_text


def test_cli_lint_passes_and_detects_contract_regressions(t_mod):
    lint = lint_module()
    parser = t_mod.build_parser()
    instructions = (ROOT / "AGENTS.md").read_text()
    assert lint.check(parser, t_mod.COMMAND_GROUPS, instructions) == []

    groups = {name: dict(actions) for name, actions in t_mod.COMMAND_GROUPS.items()}
    del groups["issue"]["status"]
    assert any("registry" in error for error in lint.check(parser, groups, instructions))

    top = lint._subparsers(parser)
    top.add_parser("surprise", help="unreviewed public shortcut")
    assert any("top-level" in error for error in lint.check(parser, t_mod.COMMAND_GROUPS, instructions))

    groups["surprise"] = {"start": ("surprise",)}
    assert any("matching nested verb parser" in error
               for error in lint.check(parser, groups, instructions))

    groups = {name: dict(actions) for name, actions in t_mod.COMMAND_GROUPS.items()}
    groups["session"]["surprise"] = ("open",)
    assert any("compatibility verb registry changed" in error
               for error in lint.check(t_mod.build_parser(), groups, instructions))

    groups = {name: dict(actions) for name, actions in t_mod.COMMAND_GROUPS.items()}
    groups["repo"]["list"] = ("repos", "missing")
    assert any("unknown parser verb" in error
               for error in lint.check(t_mod.build_parser(), groups, instructions))

    extended = t_mod.build_parser()
    issue = lint._subparsers(extended)._name_parser_map["issue"]
    lint._subparsers(issue).add_parser("triage")
    groups = {name: dict(actions) for name, actions in t_mod.COMMAND_GROUPS.items()}
    groups["issue"]["triage"] = ("issue", "triage")
    assert lint.check(extended, groups, instructions) == []

    assert any("AGENTS.md" in error for error in lint.check(t_mod.build_parser(),
                                                            t_mod.COMMAND_GROUPS, ""))


def test_cli_lint_detects_shared_picker_regressions():
    lint = lint_module()
    source = (ROOT / "libexec/t_issue.py").read_text()
    assert lint.check_issue_picker(source) == []

    bespoke = source.replace("_t_fzf_filtered", "_t_issue_private_picker", 1)
    assert any("_t_fzf_filtered" in error for error in lint.check_issue_picker(bespoke))

    single_default = source.replace("multi=True", "multi=False", 1)
    assert any("default to multi-select" in error
               for error in lint.check_issue_picker(single_default))

    no_multi = source.replace("return [targets[row] for row in self.pick_rows(",
                              "return [targets[row] for row in self.private_picker(", 1)
    assert any("select_issues" in error for error in lint.check_issue_picker(no_multi))

    many_owners = source.replace("multi=False)[0]", "multi=True)[0]", 1)
    assert any("choose" in error for error in lint.check_issue_picker(many_owners))
