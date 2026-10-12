#!/usr/bin/env python3
"""Check the public CLI's noun/verb contract before release."""

import argparse
import ast
import contextlib
import importlib.util
import io
from importlib.machinery import SourceFileLoader
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parent.parent
# Existing single-word spellings remain available as compatibility shortcuts.
# A new public command must enter a noun/verb family instead of growing this set.
LEGACY_SHORTCUTS = frozenset({
    "help", "open", "app", "ls", "kill", "restart", "push", "pop",
    "resume", "cd", "beam", "read", "plan", "paste", "find", "on",
    "repos", "setup", "new", "checkout", "instructions", "login",
    "install", "integrate", "update", "permissions", "trust", "doctor",
})
ISSUE_VERBS = {"list", "status", "start"}
# These established families route to compatibility parsers. Freeze their
# accepted verbs so extending one requires an intentional contract review.
LEGACY_GROUP_VERBS = {
    "session": {"open", "list", "close", "restart", "push", "pop", "resume", "cd",
                "move", "read", "view-plan", "paste", "search", "open-app"},
    "repo": {"list", "cd", "locate", "create", "clone"},
    "cursor": {"list", "resume", "send"},
    "hosts": {"list", "show", "add", "edit", "remove", "default", "run"},
    "agentcore": {"invoke", "stop"},
    "config": {"open", "show", "edit", "setup"},
    "profile": {"init", "edit", "show", "apply"},
    "policy": {"check", "show", "apply"},
    "agent": {"login", "install", "status", "trust"},
    "system": {"integrate", "update", "diagnose"},
}


def _subparsers(parser):
    return next((action for action in parser._actions
                 if isinstance(action, argparse._SubParsersAction)), None)


def check_issue_picker(source):
    """Check that issue selection uses the shared picker in both entry points."""
    try:
        module = ast.parse(source)
    except SyntaxError:
        return ["issue picker source is not valid Python"]
    owner = next((node for node in module.body if isinstance(node, ast.ClassDef)
                  and node.name == "IssueCommand"), None)
    methods = {node.name: node for node in owner.body if isinstance(node, ast.FunctionDef)} if owner else {}
    errors = []
    picker = methods.get("pick_rows")
    if picker is None:
        errors.append("IssueCommand must provide the shared pick_rows seam")
    else:
        literals = {node.value for node in ast.walk(picker)
                    if isinstance(node, ast.Constant) and isinstance(node.value, str)}
        if not any("_t_fzf_filtered" in value for value in literals) or not any(
                "ui/fzf.sh" in value for value in literals):
            errors.append("IssueCommand.pick_rows must source ui/fzf.sh and use _t_fzf_filtered")
        defaults = dict(zip((arg.arg for arg in picker.args.kwonlyargs), picker.args.kw_defaults))
        multi_default = defaults.get("multi")
        if not isinstance(multi_default, ast.Constant) or multi_default.value is not True:
            errors.append("IssueCommand.pick_rows must default to multi-select")
    for name, single in (("select_issues", False), ("choose", True)):
        method = methods.get(name)
        calls = [node for node in ast.walk(method) if isinstance(node, ast.Call)
                 and isinstance(node.func, ast.Attribute) and node.func.attr == "pick_rows"
                 and isinstance(node.func.value, ast.Name) and node.func.value.id == "self"] if method else []
        if not calls:
            errors.append(f"IssueCommand.{name} must call the shared pick_rows seam")
            continue
        if single and not any(any(keyword.arg == "multi" and isinstance(keyword.value, ast.Constant)
                                  and keyword.value.value is False for keyword in call.keywords)
                              for call in calls):
            errors.append("IssueCommand.choose must select one existing owner")
        if not single and any(any(keyword.arg == "multi" and isinstance(keyword.value, ast.Constant)
                                  and keyword.value.value is False for keyword in call.keywords)
                              for call in calls):
            errors.append("IssueCommand.select_issues must retain multi-select")
    return errors


def check(parser, command_groups, instructions):
    """Return violations of the explicit issue verbs and public command shape."""
    errors = []
    top = _subparsers(parser)
    if top is None:
        return ["root command parser has no noun/verb dispatch"]
    public = set(top.choices)
    unsupported = public - set(command_groups) - LEGACY_SHORTCUTS
    if unsupported:
        errors.append("new public top-level command lacks a noun/verb group: " +
                      ", ".join(sorted(unsupported)))
    issue_registry = set(command_groups.get("issue", {}))
    if not ISSUE_VERBS <= issue_registry:
        errors.append("issue command registry must expose list, status, and start")
    for group, verbs in LEGACY_GROUP_VERBS.items():
        if set(command_groups.get(group, {})) != verbs:
            errors.append(f"{group} compatibility verb registry changed; review its CLI contract")
    for group, actions in command_groups.items():
        for verb, route in actions.items():
            target = top._name_parser_map.get(route[0]) if route else None
            if target is None:
                errors.append(f"{group} {verb} routes to a missing command parser")
                continue
            for word in route[1:]:
                if word.startswith("-"):
                    if word not in target._option_string_actions:
                        errors.append(f"{group} {verb} routes to an unknown option {word}")
                else:
                    nested_route = _subparsers(target)
                    if nested_route is None or word not in nested_route._name_parser_map:
                        errors.append(f"{group} {verb} routes to an unknown parser verb {word}")
                        break
                    target = nested_route._name_parser_map[word]
        if group == "issue" or group in LEGACY_GROUP_VERBS:
            continue
        noun = top._name_parser_map.get(group)
        nested = _subparsers(noun) if noun else None
        if nested is None or set(nested.choices) != set(actions):
            errors.append(f"new public group {group} must own a matching nested verb parser")
            continue
        positional = [action for action in noun._actions if not action.option_strings
                      and not isinstance(action, argparse._SubParsersAction)]
        if positional or any(tuple(route[:2]) != (group, verb) for verb, route in actions.items()):
            errors.append(f"new public group {group} must require a verb before target arguments")
    issue = top._name_parser_map.get("issue")
    issue_sub = _subparsers(issue) if issue else None
    if issue_sub is None or not ISSUE_VERBS <= set(issue_sub.choices):
        errors.append("issue parser must expose list, status, and start as verbs")
    elif issue_registry != set(issue_sub.choices):
        errors.append("issue parser verbs must match its public command registry")

    def accepts(words):
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                args = parser.parse_args(words)
            except SystemExit:
                return False, None
        return True, args

    for words in (["issue", "list"], ["issue", "list", "repo", "--state", "all", "--json"],
                  ["issue", "status", "123", "--json"],
                  ["issue", "status", "123", "--repo", "repo"],
                  ["issue", "start"], ["issue", "start", "repo", "123", "124", "--codex"]):
        valid, args = accepts(words)
        if not valid or args.verb != "issue" or args.action != words[1]:
            errors.append("issue parser rejected required form: " + " ".join(words))
    for words in (["issue", "123"], ["issue", "list", "--codex"],
                  ["issue", "list", "--allow-incomplete-check"],
                  ["issue", "status", "123", "--codex"],
                  ["issue", "status", "123", "--allow-incomplete-check"],
                  ["issue", "start", "123", "--json"],
                  ["issue", "start", "123", "--codex", "--claude"]):
        valid, _ = accepts(words)
        if valid:
            errors.append("issue parser accepted invalid form: " + " ".join(words))
    for phrase in ("t issue list", "t issue status", "t issue start"):
        if phrase not in instructions:
            errors.append("AGENTS.md must document " + phrase)
    errors.extend(check_issue_picker((ROOT / "libexec/t_issue.py").read_text()))
    return errors


def main():
    loader = SourceFileLoader("t_cli_lint", str(ROOT / "bin/t"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    errors = check(module.build_parser(), module.COMMAND_GROUPS,
                   (ROOT / "AGENTS.md").read_text())
    for error in errors:
        print("CLI contract: " + error, file=sys.stderr)
    if not errors:
        print("CLI noun/verb and issue action contract passed")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
