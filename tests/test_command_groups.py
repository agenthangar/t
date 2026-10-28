"""Public noun/action commands retain the established handlers and protocols."""

import pytest


def test_registry_covers_the_public_groups(t_mod):
    assert tuple(t_mod.COMMAND_GROUPS) == (
        "remote", "issue", "session", "repo", "cursor", "hosts", "agentcore", "config", "profile", "policy", "agent", "system")
    for path, legacy in t_mod._command_routes():
        assert t_mod._canonical_argv([*path, "payload"]) == [*legacy, "payload"]



@pytest.mark.parametrize("argv,legacy", [
    (["session", "list", "-a"], ["ls", "-a"]),
    (["session", "open", "api", "2", "--codex", "--host", "mini"],
     ["open", "api", "2", "--codex", "--host", "mini"]),
    (["session", "open-app", "api", "3"], ["app", "api", "3"]),
    (["repo", "locate", "api"], ["repo", "locate", "api"]),
    (["repo", "create", "name", "--dry-run"], ["new", "name", "--dry-run"]),
    (["profile", "apply", "api"], ["instructions", "--apply", "api"]),
    (["policy", "apply", "--defaults"], ["permissions", "--apply", "--defaults"]),
    (["agent", "status"], ["install", "--status"]),
    (["system", "diagnose"], ["doctor"]),
])
def test_canonical_parser_matches_legacy(t_mod, argv, legacy):
    parser = t_mod.build_parser()
    assert vars(parser.parse_args(argv)) == vars(parser.parse_args(legacy))


@pytest.mark.parametrize("argv", [[], ["help"], ["-h"], ["--help"]])
def test_root_help_is_identical_and_has_only_public_groups(t_mod, monkeypatch, capsys, argv):
    monkeypatch.setattr(t_mod, "Config", lambda: pytest.fail("help loaded config"))
    monkeypatch.setattr(t_mod, "_t_auto_update", lambda args: pytest.fail("help updated"))
    assert t_mod.main(argv) == 0
    output = capsys.readouterr().out
    assert output == t_mod._root_help()
    assert all(section in output for section in ("WORK", "CONFIGURATION", "TOOLS", "FLAGS", "EXAMPLES", "LEARN MORE"))
    assert "t open" not in output and "session-rows" not in output
    assert max(map(len, output.splitlines())) <= 72


def test_root_and_group_help_wrap_at_narrow_width(t_mod, monkeypatch):
    monkeypatch.setattr(t_mod.shutil, "get_terminal_size",
                        lambda fallback: t_mod.os.terminal_size((40, 24)))
    assert max(map(len, t_mod._root_help().splitlines())) <= 40
    assert max(map(len, t_mod._group_help("session").splitlines())) <= 40


@pytest.mark.parametrize("group", ["issue", "session", "repo", "cursor", "hosts", "profile", "policy", "agent", "system"])
def test_bare_group_and_group_help_have_no_side_effects(t_mod, monkeypatch, capsys, group):
    monkeypatch.setattr(t_mod, "Config", lambda: pytest.fail("help loaded config"))
    assert t_mod.main([group]) == 0
    output = capsys.readouterr().out
    assert f"t {group} <verb>" in output
    assert t_mod.main(["help", group]) == 0
    assert capsys.readouterr().out == output


@pytest.mark.parametrize("argv,needle", [
    (["help", "profile", "apply"], "t profile apply <repo>"),
    (["config", "show", "-h"], "t config show"),
    (["policy", "apply", "--help"], "t policy apply [--defaults]"),
    (["help", "session", "search"], "t session search"),
    (["help", "repo", "locate"], "t repo locate"),
])
def test_nested_help_uses_canonical_name(t_mod, monkeypatch, capsys, argv, needle):
    monkeypatch.setattr(t_mod, "Config", lambda: pytest.fail("help loaded config"))
    assert t_mod.main(argv) == 0
    assert needle in capsys.readouterr().out


@pytest.mark.parametrize("argv", [
    ["profile", "apply"], ["policy", "check", "--apply"],
    ["config", "show", "--edit"], ["session", "unknown"],
    ["repo", "unknown"],
])
def test_invalid_group_modes_are_rejected_before_config(t_mod, monkeypatch, capsys, argv):
    monkeypatch.setattr(t_mod, "Config", lambda: pytest.fail("invalid command loaded config"))
    assert t_mod.main(argv) == 2
    assert "t " in capsys.readouterr().err


def test_host_run_forwards_literal_help_and_option_separator(t_mod, monkeypatch):
    seen = []
    monkeypatch.setattr(t_mod, "Config", lambda: object())
    monkeypatch.setitem(t_mod.IMPLEMENTED, "on", lambda cfg, args: seen.append(args.rest) or 0)
    monkeypatch.setattr(t_mod, "_t_auto_update", lambda argv: None)
    assert t_mod.main(["hosts", "run", "mini", "echo", "--help"]) == 0
    assert t_mod.main(["hosts", "run", "mini", "--", "--help"]) == 0
    assert seen == [["echo", "--help"], ["--", "--help"]]


def test_cursor_list_retains_legacy_forwarding(t_mod, monkeypatch):
    seen = []
    monkeypatch.setattr(t_mod, "Config", lambda: object())
    monkeypatch.setattr(t_mod, "_t_auto_update", lambda argv: None)
    monkeypatch.setattr(t_mod.os, "execvp", lambda exe, args: seen.append(args))
    assert t_mod.main(["cursor", "list", "--host", "mini"]) is None
    assert seen[0][-3:] == ["--ls", "--host", "mini"]


def test_agent_support_and_aliases_live_under_help(t_mod, capsys):
    assert t_mod.main(["help", "agents"]) == 0
    assert "t session open" in capsys.readouterr().out
    assert t_mod.main(["help", "aliases"]) == 0
    aliases = capsys.readouterr().out
    assert "t beam" in aliases
    assert aliases.count("t cursor ls") == 1


def test_agent_trust_status_remains_a_read_only_option(t_mod, monkeypatch):
    seen = []
    monkeypatch.setattr(t_mod, "Config", lambda: object())
    monkeypatch.setattr(t_mod, "_t_auto_update", lambda argv: None)
    monkeypatch.setitem(t_mod.IMPLEMENTED, "trust", lambda cfg, args: seen.append(args) or 0)
    assert t_mod.main(["agent", "trust", "--status", "--all"]) == 0
    assert seen[0].status is True and seen[0].all is True


def test_mode_help_advertises_only_usable_options(t_mod, capsys):
    assert t_mod.main(["help", "agent", "install"]) == 0
    assert "--status" not in capsys.readouterr().out
    assert t_mod.main(["help", "install"]) == 0
    assert "--status" in capsys.readouterr().out
    assert t_mod.main(["help", "policy", "check"]) == 0
    assert "--defaults" in capsys.readouterr().out


def test_explicit_grouped_update_does_not_prompt_for_another_update(t_mod, monkeypatch):
    monkeypatch.setattr(t_mod, "_t_update_interactive", lambda: pytest.fail("explicit update checked again"))
    assert t_mod._t_auto_update(["system", "update"]) is None
    assert t_mod._t_auto_update(["system", "update", "--local", "."]) is None


def test_every_advertised_action_has_side_effect_free_help(t_mod, monkeypatch, capsys):
    monkeypatch.setattr(t_mod, "Config", lambda: pytest.fail("help loaded configuration"))
    monkeypatch.setattr(t_mod, "_t_auto_update", lambda argv: pytest.fail("help checked updates"))
    for path, _ in t_mod._command_routes():
        assert t_mod.main(["help", *path]) == 0
        expected = capsys.readouterr().out
        assert f"usage: t {' '.join(path)}" in expected
        assert t_mod.main([*path, "--help"]) == 0
        concise = capsys.readouterr().out
        if path == ("session", "open"):
            assert "REMOTE SESSIONS" in expected
            assert "REMOTE SESSIONS" not in concise
        else:
            assert concise == expected



@pytest.mark.parametrize("words", [["open"], ["session", "open"]])
@pytest.mark.parametrize("width", [40, 80])
def test_open_help_is_concise_with_discoverable_details(t_mod, monkeypatch, capsys, words, width):
    monkeypatch.setattr(t_mod, "Config", lambda: pytest.fail("help loaded configuration"))
    monkeypatch.setattr(t_mod, "_t_auto_update", lambda argv: pytest.fail("help checked updates"))
    monkeypatch.setattr(t_mod.shutil, "get_terminal_size",
                        lambda fallback=(80, 24): t_mod.os.terminal_size((width, 24)))
    monkeypatch.setenv("COLUMNS", str(width))
    assert t_mod.main([*words, "--help"]) == 0
    concise = capsys.readouterr().out
    assert len(concise.split()) < 200
    assert max(map(len, concise.splitlines())) <= width
    assert "agent (fresh slots):" in concise and "location:" in concise
    assert "--codex --new" in concise and "t help open" in concise
    normalized = " ".join(concise.split())
    assert "Follow its release instructions if the desktop conversation is still loaded" in normalized
    assert "t app pull" not in normalized
    assert "resumes the same conversation and attaches automatically" in normalized
    assert "t " + " ".join(words) + " api 2 --cli" in normalized
    for flag in ("--new", "--fg", "--cli", "--app", "--codex", "--claude",
                 "--local", "--here", "--remote", "--host", "--help"):
        assert flag in concise
    assert t_mod.main(["help", *words]) == 0
    detailed = capsys.readouterr().out
    assert len(detailed) > len(concise)
    assert all(section in detailed for section in (
        "SESSION TARGETS", "OPENING DEFAULTS", "DESKTOP TO CLI", "REMOTE SESSIONS"))
    assert "DEV_AGENT_DEFAULT" in detailed and "<repo>:p<pid>" in detailed
    assert max(map(len, detailed.splitlines())) <= width


def test_leaf_help_does_not_turn_supplied_arguments_into_usage(t_mod, capsys):
    assert t_mod.main(["session", "open", "api", "3", "--help"]) == 0
    usage = capsys.readouterr().out.splitlines()[0]
    assert "usage: t session open " in usage
    assert "api" not in usage


def test_literal_help_argument_stays_a_repository_name(t_mod, monkeypatch):
    seen = []
    monkeypatch.setattr(t_mod, "Config", lambda: object())
    monkeypatch.setattr(t_mod, "_t_auto_update", lambda argv: None)
    monkeypatch.setitem(t_mod.IMPLEMENTED, "repo", lambda cfg, args: seen.append(args.repo) or 0)
    assert t_mod.main(["repo", "locate", "--", "--help"]) == 0
    assert seen == ["--help"]


@pytest.mark.parametrize("argv", [
    ["config", "workflow"], ["config", "workflow", "--help"],
    ["help", "config", "workflow"],
])
def test_nested_resource_help_does_not_load_config(t_mod, monkeypatch, capsys, argv):
    monkeypatch.setattr(t_mod, "Config", lambda: pytest.fail("subgroup help loaded config"))
    monkeypatch.setattr(t_mod, "_t_auto_update", lambda argv: pytest.fail("subgroup help updated"))
    assert t_mod.main(argv) == 0
    output = capsys.readouterr().out
    assert "t config workflow <verb>" in output
    assert "--repo ALIAS" in output and "never inferred" in output


@pytest.mark.parametrize("argv", [
    ["config", "workflow", "unknown"], ["config", "workflow", "list", "--edit"],
    ["config", "workflow", "edit", "--workflow"],
])
def test_nested_invalid_verbs_and_modes_fail_before_config(t_mod, monkeypatch, capsys, argv):
    monkeypatch.setattr(t_mod, "Config", lambda: pytest.fail("invalid nested command loaded config"))
    assert t_mod.main(argv) == 2
    assert "t config workflow" in capsys.readouterr().err


@pytest.mark.parametrize("flag", ["--show", "--edit", "--workflow", "--onboard"])
def test_legacy_mode_before_workflow_subgroup_is_rejected(t_mod, monkeypatch, flag):
    monkeypatch.setattr(t_mod, "Config", lambda: pytest.fail("conflicting command loaded config"))
    with pytest.raises(SystemExit) as exc:
        t_mod.main(["config", flag, "workflow", "list"])
    assert exc.value.code == 2


LISTING_CASES = [
    (("session",), ["api", "--remote"]),
    (("repo",), []),
    (("cursor",), ["--host", "mini"]),
    (("hosts",), ["--json"]),
    (("issue",), ["--repo", "ls", "--state", "all", "--json"]),
    (("config", "workflow"), ["--repo", "list", "--json"]),
    ((), ["help", "--remote"]),
]


@pytest.mark.parametrize("prefix,payload", LISTING_CASES)
def test_listing_aliases_share_parser_handler_and_readonly(t_mod, monkeypatch, prefix, payload):
    parser = t_mod.build_parser()
    commands = [[*prefix, verb, *payload] for verb in ("ls", "list")]
    assert t_mod._canonical_argv(commands[0]) == t_mod._canonical_argv(commands[1])
    assert vars(parser.parse_args(commands[0])) == vars(parser.parse_args(commands[1]))
    seen = []
    monkeypatch.setattr(t_mod, "Config", lambda: object())
    monkeypatch.setattr(t_mod, "_t_update_interactive", lambda: pytest.fail("listing checked for updates"))
    def handler(cfg, args):
        seen.append(vars(args))
        return 0
    for name in t_mod.IMPLEMENTED:
        monkeypatch.setitem(t_mod.IMPLEMENTED, name, handler)
    monkeypatch.setattr(t_mod, "cmd_cursor", handler)
    for argv in commands:
        assert t_mod._command_readonly(argv, terminal=True)
        assert t_mod.main(argv) == 0
    assert seen[0] == seen[1]


@pytest.mark.parametrize("prefix,payload", LISTING_CASES)
@pytest.mark.parametrize("verb", ["ls", "list"])
def test_listing_alias_help_keeps_requested_spelling(t_mod, monkeypatch, capsys, prefix, payload, verb):
    monkeypatch.setattr(t_mod, "Config", lambda: pytest.fail("alias help loaded config"))
    words = [*prefix, verb]
    for argv in (["help", *words], [*words, "--help"]):
        assert t_mod.main(argv) == 0
        assert f"usage: t {' '.join(words)}" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [
    ["repo", "add", "ls", "--alias", "list"],
    ["repo", "locate", "help"], ["session", "open", "list"],
    ["config", "workflow", "edit", "--repo", "ls"],
    ["hosts", "run", "ls", "t", "config", "workflow", "ls", "--repo", "help"],
])
def test_alias_resolution_preserves_payload_words(t_mod, argv):
    path, route = t_mod._command_path(argv)
    assert t_mod._canonical_argv(argv) == [*route, *argv[len(path):]]


@pytest.mark.parametrize("tail", [[], ["ls"], ["list"], ["cd", "api"],
                                  ["add", "."], ["path", "api"], ["--help"]])
def test_removed_repos_family_is_rejected_before_side_effects(t_mod, monkeypatch, capsys, tail):
    monkeypatch.setattr(t_mod, "Config", lambda: pytest.fail("removed family loaded config"))
    monkeypatch.setattr(t_mod, "_t_auto_update", lambda argv: pytest.fail("removed family checked updates"))
    assert t_mod.main(["repos", *tail]) == 2
    assert "unknown command" in capsys.readouterr().err
    with pytest.raises(SystemExit) as exc:
        t_mod.build_parser().parse_args(["repos", *tail])
    assert exc.value.code == 2
    capsys.readouterr()
    assert t_mod.main(["help", "repos", *tail]) == 2
    assert "unknown command" in capsys.readouterr().err
