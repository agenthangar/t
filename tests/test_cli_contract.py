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
    all_repos = parser.parse_args(["issue", "list", "-a", "--state", "all", "--json"])
    assert (all_repos.repository, all_repos.repo, all_repos.all, all_repos.state,
            all_repos.json) == (None, None, True, "all", True)
    conflict = parser.parse_args(["issue", "list", "api", "--repo", "owner/repo"])
    assert (conflict.repository, conflict.repo) == ("api", "owner/repo")
    status = parser.parse_args(["issue", "status", "123", "--repo", "api", "--json"])
    assert (status.action, status.target, status.repo, status.json) == (
        "status", ["123"], "api", True)
    start = parser.parse_args(["issue", "start", "api", "123", "124", "--codex"])
    assert (start.action, start.target, start.codex) == ("start", ["api", "123", "124"], True)
    assert parser.parse_args(["issue", "start"]).target == []
    opened = parser.parse_args(["issue", "open", "api", "123", "124"])
    assert (opened.action, opened.target, opened.repo) == ("open", ["api", "123", "124"], None)
    opened = parser.parse_args(["issue", "open", "123", "--repo", "owner/repo"])
    assert (opened.target, opened.repo) == (["123"], "owner/repo")
    assert parser.parse_args(["issue", "open"]).target == []


@pytest.mark.parametrize("argv", [
    ["issue", "123"], ["issue", "list", "--codex"],
    ["issue", "status", "123", "--allow-incomplete-check"],
    ["issue", "start", "123", "--json"],
    ["issue", "start", "123", "--codex", "--claude"],
    ["issue", "open", "123", "--json"],
    ["issue", "open", "123", "--codex"],
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
               for verb in ("list", "status", "start", "open"))
    assert t_mod.main(["issue", "--help"]) == 0
    assert capsys.readouterr().out == menu
    assert t_mod.main(["help", "issue", "status"]) == 0
    assert "usage: t issue status" in capsys.readouterr().out
    assert t_mod.main(["help", "issue", "start"]) == 0
    assert "usage: t issue start" in capsys.readouterr().out
    assert t_mod.main(["help", "issue", "open"]) == 0
    assert "usage: t issue open" in capsys.readouterr().out


def test_issue_help_shows_repository_resolution_on_every_verb(t_mod, capsys):
    menu = t_mod._group_help("issue")
    for syntax in ("t issue list [repo]", "t issue status [repo] <id>",
                   "t issue start [repo] [id ...]", "t issue open [repo] [id ...]"):
        assert syntax in menu
    assert "owner/repo" in menu and "--repo" in menu
    assert "list --all" in menu.lower() and "all registered repositories" in menu.lower()
    lint = lint_module()
    assert lint.check_issue_help(menu) == []
    assert any("status [repo]" in error for error in lint.check_issue_help(
        menu.replace("t issue status [repo] <id>", "t issue status <id>")))
    for verb in ("list", "status", "start", "open"):
        assert t_mod.main(["help", "issue", verb]) == 0
        help_text = capsys.readouterr().out
        assert "[repo]" in help_text and "--repo" in help_text
        if verb == "list":
            assert "--all" in help_text


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
    triage = lint._subparsers(issue).add_parser("triage", usage="%(prog)s [repo]")
    groups = {name: dict(actions) for name, actions in t_mod.COMMAND_GROUPS.items()}
    groups["issue"]["triage"] = ("issue", "triage")
    assert any("issue triage" in error for error in lint.check(extended, groups, instructions))
    triage.add_argument("repo", nargs="?")
    triage.add_argument("--repo", dest="repo")
    assert lint.check(extended, groups, instructions) == []

    missing_scope = t_mod.build_parser()
    list_parser = lint._subparsers(lint._subparsers(missing_scope)
                                   ._name_parser_map["issue"])._name_parser_map["list"]
    list_parser._option_string_actions.pop("--all")
    assert any("issue list must expose -a/--all" in error
               for error in lint.check(missing_scope, t_mod.COMMAND_GROUPS, instructions))

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


def test_cli_lint_keeps_issue_list_noninteractive():
    lint = lint_module()
    source = (ROOT / "libexec/t_issue.py").read_text()
    assert lint.check_issue_list(source) == []

    no_scope = source.replace("self.list_repositories(", "self.resolve_repo(", 1)
    assert any("list_repositories" in error for error in lint.check_issue_list(no_scope))

    picker = source.replace("self.list_repositories(", "self.pick_rows(", 1)
    assert any("interactive selection" in error for error in lint.check_issue_list(picker))


def test_cli_lint_recurses_into_resource_subgroups(t_mod):
    import copy
    import json

    lint = lint_module()
    instructions = (ROOT / 'AGENTS.md').read_text()
    contract = json.loads((ROOT / 'ui/command-contract.json').read_text())
    assert lint.check_group_help(t_mod._group_help, t_mod.COMMAND_GROUPS) == []
    assert lint.check_command_patterns(t_mod.build_parser(), t_mod.COMMAND_GROUPS, contract) == []

    groups = copy.deepcopy(t_mod.COMMAND_GROUPS)
    del groups['config']['workflow']['edit']
    errors = lint.check(t_mod.build_parser(), groups, instructions)
    assert any('config workflow registry' in error for error in errors)
    assert any('matching nested verb parser' in error for error in errors)

    groups = copy.deepcopy(t_mod.COMMAND_GROUPS)
    groups['config']['workflow']['list'] = ('config', '--show', '--workflow')
    assert any('nested resource must route to its own parser' in error
               for error in lint.check(t_mod.build_parser(), groups, instructions))

    parser = t_mod.build_parser()
    config = lint._subparsers(parser)._name_parser_map['config']
    workflow = lint._subparsers(config)._name_parser_map['workflow']
    children = lint._subparsers(workflow)
    children._name_parser_map.pop('edit')
    assert any('config workflow edit routes to an unknown parser verb' in error
               for error in lint.check(parser, t_mod.COMMAND_GROUPS, instructions))

    parser = t_mod.build_parser()
    config = lint._subparsers(parser)._name_parser_map['config']
    workflow = lint._subparsers(config)._name_parser_map['workflow']
    listing = lint._subparsers(workflow)._name_parser_map['list']
    listing._option_string_actions['--repo'].dest = 'wrong_repository'
    listing.set_defaults(workflow_action='edit')
    errors = lint.check(parser, t_mod.COMMAND_GROUPS, instructions)
    assert any('consistent --repo ALIAS' in error for error in errors)
    assert any('misrouted required form' in error for error in errors)

    missing = copy.deepcopy(contract)
    del missing['commands']['config workflow edit']
    assert any('missing config workflow edit' in error for error in
               lint.check_command_patterns(t_mod.build_parser(), t_mod.COMMAND_GROUPS, missing))
    wrong = copy.deepcopy(contract)
    wrong['commands']['config workflow list']['modes']['default']['read_only'] = False
    assert any('list must be read-only' in error for error in
               lint.check_command_patterns(t_mod.build_parser(), t_mod.COMMAND_GROUPS, wrong))
    wrong['commands']['config workflow list']['modes']['default']['patterns'] = ['single']
    assert any('without selection' in error for error in
               lint.check_command_patterns(t_mod.build_parser(), t_mod.COMMAND_GROUPS, wrong))
    assert any('config workflow edit is missing' in error for error in
               lint.check_group_help(lambda group: 'list  List workflow' if group == 'config workflow'
                                     else t_mod._group_help(group), t_mod.COMMAND_GROUPS))
    assert any('nested resources before verbs' in error for error in
               lint.check(t_mod.build_parser(), t_mod.COMMAND_GROUPS,
                          instructions.replace('nested resources before verbs', 'flat commands')))



def test_cli_lint_supports_new_groups_with_nested_resources(t_mod):
    import copy
    lint = lint_module()
    parser = t_mod.build_parser()
    instructions = (ROOT / 'AGENTS.md').read_text()
    family = lint._subparsers(parser).add_parser('preferences')
    resource = family.add_subparsers(dest='resource').add_parser('workflow')
    resource.add_subparsers(dest='operation').add_parser('list')
    groups = copy.deepcopy(t_mod.COMMAND_GROUPS)
    groups['preferences'] = {'workflow': {'list': ('preferences', 'workflow', 'list')}}
    assert lint.check(parser, groups, instructions) == []
    groups['preferences']['workflow']['list'] = ('config', '--show')
    errors = lint.check(parser, groups, instructions)
    assert any('nested resource must route to its own parser' in error for error in errors)
    assert any('require a verb before target arguments' in error for error in errors)
