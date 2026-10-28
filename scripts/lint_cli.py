#!/usr/bin/env python3
"""Check the public CLI's noun/verb contract before release."""

import argparse
import ast
import contextlib
import importlib.util
import io
import json
from importlib.machinery import SourceFileLoader
from pathlib import Path
import re
import sys


ROOT = Path(__file__).resolve().parent.parent
CONTRACT = ROOT / "ui" / "command-contract.json"
# Existing single-word spellings remain available as compatibility shortcuts.
# A new public command must enter a noun/verb family instead of growing this set.
LEGACY_SHORTCUTS = frozenset({
    "help", "open", "app", "ls", "kill", "restart", "push", "pop",
    "resume", "history", "cd", "beam", "read", "plan", "paste", "find", "on",
    "repos", "setup", "new", "checkout", "instructions", "login",
    "install", "integrate", "update", "permissions", "trust", "doctor",
})
# `t share` is the reviewed exception: sending is the root action, while
# targets, inbox, and read remain explicit read-only verbs.
SHARE_COMMAND = "share"
ISSUE_VERBS = {"list", "status", "start", "open"}
# These established families route to compatibility parsers. Freeze their
# accepted verbs so extending one requires an intentional contract review.
LEGACY_GROUP_VERBS = {
    "session": {"open", "list", "close", "restart", "push", "pop", "resume", "history", "cd",
                "move", "read", "view-plan", "paste", "search", "open-app", "goal"},
    "repo": {"list", "cd", "locate", "create", "clone", "add"},
    "cursor": {"list", "resume", "send"},
    "hosts": {"list", "show", "add", "edit", "remove", "default", "run"},
    "agentcore": {"invoke", "stop"},
    "config": {"open", "show", "edit", "setup", "onboard", "workflow"},
    "profile": {"init", "edit", "show", "apply"},
    "policy": {"check", "show", "apply"},
    "agent": {"login", "install", "status", "trust"},
    "system": {"integrate", "update", "diagnose", "advisories"},
}


def _subparsers(parser):
    return next((action for action in parser._actions
                 if isinstance(action, argparse._SubParsersAction)), None)


def command_routes(groups, prefix=()):
    """Flatten resource subgroups without treating them as command leaves."""
    for name, value in groups.items():
        path = (*prefix, name)
        if isinstance(value, dict):
            yield from command_routes(value, path)
        else:
            yield path, value


def check_subgroups(parser, groups, prefix=()):
    """Nested resource registries must own matching native parser children."""
    errors = []
    for name, value in groups.items():
        if not isinstance(value, dict):
            continue
        path = (*prefix, name)
        parent = parser
        for word in path:
            children = _subparsers(parent) if parent else None
            parent = children._name_parser_map.get(word) if children else None
        if prefix:
            children = _subparsers(parent) if parent else None
            if children is None or set(children.choices) != set(value):
                errors.append(f"{' '.join(path)} subgroup must own a matching nested verb parser")
            for leaf_path, route in command_routes(value, path):
                if tuple(route) != leaf_path:
                    errors.append(f"{' '.join(leaf_path)} nested resource must route to its own parser")
        errors.extend(check_subgroups(parser, value, path))
    return errors


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


def check_issue_help(help_text):
    """Keep repository selection visible on the bare issue command."""
    errors = []
    help_text = " ".join(help_text.split())
    for syntax in ("t issue list [repo]", "t issue status [repo] <id>",
                   "t issue start [repo] [id ...]", "t issue open [repo] [id ...]"):
        if syntax not in help_text:
            errors.append("t issue help must show " + syntax)
    for detail in ("owner/repo", "--repo", "current checkout"):
        if detail not in help_text:
            errors.append("t issue help must explain " + detail)
    for detail in ("list --all", "all registered repositories", "Status never prompts"):
        if detail.lower() not in help_text.lower():
            errors.append("t issue help must explain " + detail)
    return errors


def check_group_help(group_help, command_groups):
    """Every canonical command must remain discoverable in its group menu."""
    errors = []
    def visit(groups, prefix=()):
        for group, verbs in groups.items():
            if not isinstance(verbs, dict):
                continue
            path = (*prefix, group)
            menu = group_help(" ".join(path))
            for verb in verbs:
                if (not re.search(rf"(?m)^  {re.escape(verb)}\s+\S", menu)
                        and f"t {' '.join(path)} {verb}" not in " ".join(menu.split())):
                    errors.append(f"{' '.join(path)} {verb} is missing from command-group help")
            visit(verbs, path)
    visit(command_groups)
    return errors


def check_issue_list(source):
    """Keep list as a direct, read-only display with explicit scope and rendering."""
    try:
        module = ast.parse(source)
    except SyntaxError:
        return ["issue list source is not valid Python"]
    owner = next((node for node in module.body if isinstance(node, ast.ClassDef)
                  and node.name == "IssueCommand"), None)
    methods = {node.name: node for node in owner.body if isinstance(node, ast.FunctionDef)} if owner else {}
    errors = []
    for name in ("list_issues", "list_repositories", "render_issues"):
        if name not in methods:
            errors.append(f"IssueCommand.{name} must support direct issue listing")
    listing = methods.get("list_issues")
    calls = {node.func.attr for node in ast.walk(listing)
             if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
             and isinstance(node.func.value, ast.Name) and node.func.value.id == "self"} if listing else set()
    for name in ("list_repositories", "render_issues"):
        if name not in calls:
            errors.append(f"IssueCommand.list_issues must call {name}")
    for name in ("list_issues", "list_repositories", "render_issues"):
        method = methods.get(name)
        if method is None:
            continue
        forbidden = {node.func.attr for node in ast.walk(method)
                     if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                     and node.func.attr in ("resolve_repo", "pick_rows", "select_issues")}
        if forbidden:
            errors.append(f"IssueCommand.{name} must not use interactive selection")
    return errors


def check(parser, command_groups, instructions):
    """Return violations of the explicit issue verbs and public command shape."""
    errors = []
    top = _subparsers(parser)
    if top is None:
        return ["root command parser has no noun/verb dispatch"]
    public = set(top.choices)
    unsupported = public - set(command_groups) - LEGACY_SHORTCUTS - {SHARE_COMMAND}
    if unsupported:
        errors.append("new public top-level command lacks a noun/verb group: " +
                      ", ".join(sorted(unsupported)))
    issue_registry = set(command_groups.get("issue", {}))
    if not ISSUE_VERBS <= issue_registry:
        errors.append("issue command registry must expose list, status, start, and open")
    for group, verbs in LEGACY_GROUP_VERBS.items():
        if set(command_groups.get(group, {})) != verbs:
            errors.append(f"{group} compatibility verb registry changed; review its CLI contract")
    errors.extend(check_subgroups(parser, command_groups))
    for path, route in command_routes(command_groups):
        name = " ".join(path)
        target = top._name_parser_map.get(route[0]) if route else None
        if target is None:
            errors.append(f"{name} routes to a missing command parser")
            continue
        for word in route[1:]:
            if word.startswith("-"):
                if word not in target._option_string_actions:
                    errors.append(f"{name} routes to an unknown option {word}")
            else:
                nested_route = _subparsers(target)
                if nested_route is None or word not in nested_route._name_parser_map:
                    errors.append(f"{name} routes to an unknown parser verb {word}")
                    break
                target = nested_route._name_parser_map[word]
    for group, actions in command_groups.items():
        if group == "issue" or group in LEGACY_GROUP_VERBS:
            continue
        noun = top._name_parser_map.get(group)
        nested = _subparsers(noun) if noun else None
        if nested is None or set(nested.choices) != set(actions):
            errors.append(f"new public group {group} must own a matching nested verb parser")
            continue
        positional = [action for action in noun._actions if not action.option_strings
                      and not isinstance(action, argparse._SubParsersAction)]
        if positional or any(tuple(route) != path for path, route in command_routes(actions, (group,))):
            errors.append(f"new public group {group} must require a verb before target arguments")
    issue = top._name_parser_map.get("issue")
    issue_sub = _subparsers(issue) if issue else None
    if issue_sub is None or not ISSUE_VERBS <= set(issue_sub.choices):
        errors.append("issue parser must expose list, status, start, and open as verbs")
    elif issue_registry != set(issue_sub.choices):
        errors.append("issue parser verbs must match its public command registry")
    if issue_sub is not None:
        for verb in issue_registry:
            leaf = issue_sub._name_parser_map.get(verb)
            if leaf is None:
                continue
            repo_option = leaf._option_string_actions.get("--repo")
            if repo_option is None or repo_option.dest != "repo" or "[repo]" not in leaf.format_usage():
                errors.append(f"issue {verb} must expose positional repo and --repo")
            if verb == "list" and not any(action.dest == "repository" and action.nargs == "?"
                                          for action in leaf._actions):
                errors.append("issue list must keep optional positional repository separate from --repo")
            if verb == "list" and not (leaf._option_string_actions.get("--all") is not None
                                       and leaf._option_string_actions.get("-a") is
                                       leaf._option_string_actions.get("--all")
                                       and leaf._option_string_actions["--all"].dest == "all"):
                errors.append("issue list must expose -a/--all for repository scope")
    share = top._name_parser_map.get(SHARE_COMMAND)
    share_sub = _subparsers(share) if share else None
    if share_sub is None or set(share_sub.choices) != {"targets", "inbox", "read"}:
        errors.append("share parser must expose only targets, inbox, and read verbs")

    def accepts(words):
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                args = parser.parse_args(words)
            except SystemExit:
                return False, None
        return True, args

    workflow_registry = command_groups.get("config", {}).get("workflow", {})
    if not isinstance(workflow_registry, dict) or set(workflow_registry) != {"list", "edit"}:
        errors.append("config workflow registry must expose list and edit")
    config = top._name_parser_map.get("config")
    config_sub = _subparsers(config) if config else None
    workflow = config_sub._name_parser_map.get("workflow") if config_sub else None
    workflow_sub = _subparsers(workflow) if workflow else None
    for verb in ("list", "edit"):
        leaf = workflow_sub._name_parser_map.get(verb) if workflow_sub else None
        option = leaf._option_string_actions.get("--repo") if leaf else None
        if option is None or option.dest != "workflow_repo" or option.metavar != "ALIAS":
            errors.append(f"config workflow {verb} must expose consistent --repo ALIAS")
        for scope in ([], ["--repo", "api"]):
            words = ["config", "workflow", verb, *scope]
            valid, args = accepts(words)
            if not valid or args.verb != "config" or getattr(args, "workflow_action", None) != verb:
                errors.append("workflow parser rejected or misrouted required form: " + " ".join(words))
    valid, args = accepts(["config", "workflow", "list", "--json"])
    if not valid or not getattr(args, "json", False):
        errors.append("config workflow list must expose --json")
    for phrase in ("nested resources before verbs", "flags select scope or format", "t config workflow list", "t config workflow edit"):
        if phrase not in instructions:
            errors.append("AGENTS.md must document " + phrase)
    for words in (["issue", "list"], ["issue", "list", "--all", "--json"],
                  ["issue", "list", "-a"], ["issue", "list", "repo", "--state", "all", "--json"],
                  ["issue", "list", "--repo", "repo"],
                  ["issue", "status", "123", "--json"],
                  ["issue", "status", "123", "--repo", "repo"],
                  ["issue", "start"], ["issue", "start", "repo", "123", "124", "--codex"],
                  ["issue", "open"], ["issue", "open", "repo", "123", "124"],
                  ["issue", "open", "--repo", "owner/repo", "123"],
                  ["issue", "open", "https://github.com/owner/repo/issues/123"]):
        valid, args = accepts(words)
        if not valid or args.verb != "issue" or args.action != words[1]:
            errors.append("issue parser rejected required form: " + " ".join(words))
    for words in (["issue", "123"], ["issue", "list", "--codex"],
                  ["issue", "list", "--allow-incomplete-check"],
                  ["issue", "status", "123", "--codex"],
                  ["issue", "status", "123", "--allow-incomplete-check"],
                  ["issue", "start", "123", "--json"],
                  ["issue", "open", "123", "--json"],
                  ["issue", "open", "123", "--codex"],
                  ["issue", "start", "123", "--codex", "--claude"]):
        valid, _ = accepts(words)
        if valid:
            errors.append("issue parser accepted invalid form: " + " ".join(words))
    for phrase in ("t issue list", "t issue status", "t issue start", "t issue open"):
        if phrase not in instructions:
            errors.append("AGENTS.md must document " + phrase)
    if "t share" not in instructions:
        errors.append("AGENTS.md must document the explicit t share exception")
    for words in (["share", "--to", "codex:123", "--file", "-"],
                  ["share", "--retry", "123"], ["share", "targets"],
                  ["share", "inbox"], ["share", "read", "123"]):
        valid, args = accepts(words)
        if not valid or args.verb != "share":
            errors.append("share parser rejected required form: " + " ".join(words))
    for words in (["share", "send"], ["share", "targets", "--file", "x"],
                  ["share", "inbox", "--to", "codex:123"],
                  ["share", "read", "--retry", "123"]):
        valid, _ = accepts(words)
        if valid:
            errors.append("share parser accepted invalid form: " + " ".join(words))
    errors.extend(check_issue_picker((ROOT / "libexec/t_issue.py").read_text()))
    errors.extend(check_issue_list((ROOT / "libexec/t_issue.py").read_text()))
    return errors


def check_command_patterns(parser, command_groups, contract, *, root=ROOT):
    """Validate the development inventory against the parser and test suite."""
    errors = []
    allowed = {"list", "detail", "single", "multi", "filtered", "wizard",
               "review", "action", "passthrough", "machine"}
    if contract.get("schema_version") != 1:
        errors.append("command inventory schema version must be 1")
    patterns = contract.get("patterns", {})
    if set(patterns) != allowed:
        errors.append("command patterns must define the ten approved behavior types")
    registry_routes = {" ".join(path): route for path, route in command_routes(command_groups)}
    routes = (set(registry_routes)
              | {SHARE_COMMAND, "share targets", "share inbox", "share read"})
    top = _subparsers(parser)
    commands = contract.get("commands", {})
    if set(commands) != routes:
        missing = routes - set(commands)
        stale = set(commands) - routes
        errors.append("command inventory differs from COMMAND_GROUPS: missing " +
                      ", ".join(sorted(missing)) + "; stale " + ", ".join(sorted(stale)))

    def accepts(argv):
        with contextlib.redirect_stderr(io.StringIO()):
            try:
                parser.parse_args(argv)
            except SystemExit:
                return False
        return True

    for name in sorted(routes & set(commands)):
        entry = commands[name]
        words = name.split()
        route = registry_routes.get(name, tuple(words))
        leaf = top._name_parser_map.get(route[0]) if top else None
        for word in route[1:]:
            if word.startswith("-"):
                continue
            nested = _subparsers(leaf) if leaf else None
            leaf = nested._name_parser_map.get(word) if nested else None
        modes = entry.get("modes", {})
        if not modes or "default" not in modes:
            errors.append(f"{name} needs a default mode")
        if (entry.get("renderer") not in {"plain", "fzf", "rail", "external", "mixed"}
                or not entry.get("scope")
                or entry.get("confirmation") not in {"none", "enter", "review", "per_target", "when_interrupting", "external"}):
            errors.append(f"{name} needs renderer, scope, and confirmation policy")
        declared = set(entry.get("patterns", []))
        actual = set()
        if not declared <= allowed or not declared:
            errors.append(f"{name} names an unknown or empty command pattern")
        for mode, spec in modes.items():
            mode_patterns = set(spec.get("patterns", []))
            actual |= mode_patterns
            if not mode_patterns or not mode_patterns <= allowed:
                errors.append(f"{name} {mode} names an unknown or empty mode pattern")
            if not isinstance(spec.get("read_only"), bool):
                errors.append(f"{name} {mode} needs a read_only policy")
            if "terminal" in spec and not isinstance(spec["terminal"], bool):
                errors.append(f"{name} {mode} terminal context must be boolean")
            argv = spec.get("argv", [])
            if not isinstance(argv, list) or argv[:len(words)] != words or not accepts(argv):
                errors.append(f"{name} {mode} example does not parse as its canonical command")
            if words[-1] == "list" and (not spec.get("read_only") or "list" not in mode_patterns and "machine" not in mode_patterns
                                             or mode_patterns & {"single", "multi", "filtered", "wizard", "review", "action"}):
                errors.append(f"{name} {mode} list must be read-only list or machine output without selection")
            for reference in spec.get("tests", []):
                errors.extend(_check_test_reference(reference, root))
            if not spec.get("tests"):
                errors.append(f"{name} {mode} needs a test reference")
        if actual != declared:
            errors.append(f"{name} command patterns disagree with its modes")
        if leaf is not None:
            conditional = {"--json", "--dry-run", "--status", "--check", "--dump", "--quiet"}
            available = conditional & set(leaf._option_string_actions)
            # These flags change the presentation or safety contract. A route
            # whose canonical verb already selects the mode need not repeat it.
            implicit = {word for word in route if word.startswith("-")}
            if name == "agent install":
                implicit.add("--status")  # Public action is `agent status`.
            for flag in sorted(available - implicit):
                if not any(flag in spec.get("argv", []) for spec in modes.values()):
                    errors.append(f"{name} lacks a conditional mode for {flag}")
        for reference in entry.get("tests", []):
            errors.extend(_check_test_reference(reference, root))
        if not entry.get("tests"):
            errors.append(f"{name} needs a command test reference")
    errors.extend(check_display_contracts(contract, root=root))
    return errors


DISPLAY_FIELDS = frozenset({"state", "context", "interface", "agent", "model", "host", "slot", "summary", "status"})
DISPLAY_COLUMNS = ("STATUS", "UI", "MODEL", "HOST", "SESSION", "WORKING ON")
HISTORY_ENVELOPE = frozenset({"version", "repo", "slot", "page", "limit", "total", "conversations"})
HISTORY_RECORD = frozenset({"repo", "agent", "sid", "title", "updated", "associations", "sources", "prs", "mentions", "owners"})


def check_display_contracts(contract, *, root=ROOT):
    """Keep the shared ls/history information contract executable and complete."""
    errors = []
    schemas = contract.get("display_schemas", {})
    common, history = schemas.get("session_information"), schemas.get("history_information")
    if not isinstance(common, dict) or not isinstance(history, dict):
        return ["command inventory must define session_information and history_information display schemas"]
    for command, name in (("session list", "session_information"), ("session history", "history_information")):
        if contract.get("commands", {}).get(command, {}).get("display_schema") != name:
            errors.append(f"{command} must bind the {name} display schema")
    fields = common.get("fields", {})
    if set(fields) != DISPLAY_FIELDS or any(not isinstance(spec, dict) or spec.get("type") != "string"
                                          or not spec.get("meaning") or "unknown" not in spec
                                          for spec in fields.values()):
        errors.append("session_information must define all typed session detail fields and unknown meanings")
    columns = common.get("columns", [])
    if (not isinstance(columns, list) or tuple(column.get("label") for column in columns if isinstance(column, dict)) != DISPLAY_COLUMNS
            or {field for column in columns if isinstance(column, dict) for field in column.get("fields", [])} != DISPLAY_FIELDS):
        errors.append("session_information must preserve STATUS/UI/MODEL/HOST/SESSION/WORKING ON columns and field coverage")
    groups = {"STATUS": ["state", "context", "status"], "UI": ["interface"], "MODEL": ["agent", "model"],
              "HOST": ["host"], "SESSION": ["slot"], "WORKING ON": ["summary"]}
    if any(column.get("fields") != groups.get(column.get("label")) for column in columns if isinstance(column, dict)):
        errors.append("session_information columns must bind their actual session detail fields")
    if common.get("status_marks") != {"attached": "●", "detached": "○", "active_context": "✓", "still_in_progress": "↻"}:
        errors.append("session_information must preserve the session status mark meanings")
    table = common.get("layouts", {}).get("table", {})
    for key, value in (("wide", "aligned_columns"), ("narrow", "labeled_wrapped_cells"),
                       ("switch", "when_columns_do_not_fit"), ("preserve_values", True), ("redirected", "untruncated"),
                       ("terminal_sizes", [[80, 24], [44, 20], [32, 12]])):
        if table.get(key) != value:
            errors.append(f"session_information table must define {key} as {value!r}")
    for key in ("infer_interface_from_agent", "infer_model_from_agent", "infer_active_context_without_owner"):
        if common.get("unknown_policy", {}).get(key) is not False:
            errors.append(f"session_information unknown policy must prohibit {key}")
    if history.get("extends") != "session_information" or history.get("fields", {}).get("prs", {}).get("type") != "array":
        errors.append("history_information must extend session_information and define structured PRs")
    historical = {"updated", "associations", "sid", "model_source", "summary_source", "mentions", "sources"}
    if not historical <= set(history.get("historical_fields", {})):
        errors.append("history_information must define historical detail identity, metadata provenance, and evidence")
    if any(not isinstance(spec, dict) or spec.get("type") not in {"string", "number", "array"}
           or not spec.get("meaning") for spec in history.get("historical_fields", {}).values()):
        errors.append("history_information historical metadata needs typed meanings")
    layouts = history.get("layouts", {})
    printed = layouts.get("table", {}).get("columns", [])
    if printed[:len(DISPLAY_COLUMNS)] != list(DISPLAY_COLUMNS) or not {"DATE", "BRANCH", "PRS", "ID"} <= set(printed):
        errors.append("history_information table must preserve session list column order and retained DATE/BRANCH/PRS/ID details")
    if layouts.get("table", {}).get("extensions") != {"DATE": "updated", "BRANCH": "associations[].branch", "PRS": "prs", "ID": "sid"}:
        errors.append("history_information table extensions must bind retained historical metadata")
    picker = layouts.get("picker", {})
    for key, value in (("profile", "_t_fzf_paginated"), ("physical_row", "single_line"),
                       ("narrow", "status_and_working_on_first"), ("max_prs_per_row", 3),
                       ("terminal_sizes", [[80, 24], [44, 20], [32, 12]])):
        if picker.get(key) != value:
            errors.append(f"history_information picker must define {key} as {value!r}")
    preview = history.get("preview", {})
    required = historical | {"repo", "title", "prs"} | {"display." + field for field in DISPLAY_FIELDS}
    if not required <= set(preview.get("required_fields", [])) or preview.get("full_values") is not True:
        errors.append("history_information preview must retain full identity, session information, and historical evidence")
    if (preview.get("default") != "when_terminal_fits" or preview.get("toggle") != "ctrl-/"
            or preview.get("minimum_columns") != 70 or preview.get("minimum_lines") != 22
            or preview.get("placement") != "bottom" or preview.get("height_percent") != 35):
        errors.append("history_information preview must use responsive defaults and Ctrl-/ details")
    selection = history.get("selection", {})
    for key, value in (("count", 1), ("identity_fields", ["repo", "agent", "sid"]),
                       ("accept", "enter"), ("cancel", ["esc", "ctrl-c"]), ("page_size", 50),
                       ("keys", {"next": "ctrl-f", "previous": "ctrl-b", "refresh": "ctrl-r"}),
                       ("auto_accept_single_result", False), ("search", "global_before_pagination"),
                       ("validate_returned_query_page_and_snapshot", True), ("network", "explicit_refresh_only"),
                       ("non_tty", "read_only_table"), ("json", "read_only_machine_output")):
        if selection.get(key) != value:
            errors.append(f"history_information selection must define {key} as {value!r}")
    machine = history.get("json", {})
    if (machine.get("version") != 1 or set(machine.get("envelope_required", [])) != HISTORY_ENVELOPE
            or set(machine.get("conversation_required", [])) != HISTORY_RECORD
            or machine.get("compatibility") != "additive_only" or machine.get("ansi") is not False or machine.get("prompts") is not False):
        errors.append("history_information JSON must preserve the version 1 envelope and existing conversation fields without chrome or prompts")
    wire_types = {"version": "integer", "repo": "string", "slot": "string", "page": "integer",
                  "limit": "integer", "total": "integer", "conversations": "array"}
    record_types = {"repo": "string", "agent": "string", "sid": "string", "title": "string", "updated": "number",
                    "associations": "array", "sources": "array", "prs": "array", "mentions": "array", "owners": "array",
                    "model": "string", "model_source": "string", "summary": "string", "summary_source": "string",
                    "interface": "string", "display": "object"}
    if (machine.get("envelope_types") != wire_types or machine.get("conversation_types") != record_types
            or machine.get("display_schema") != "history_information"):
        errors.append("history_information JSON must type its original fields and additive display metadata")
    for schema in (common, history):
        if not schema.get("behavior_tests"):
            errors.append("display schemas need behavior tests exercising actual renderers")
        for reference in schema.get("behavior_tests", []):
            errors.extend(_check_test_reference(reference, root))
    return errors


def check_display_projection(contract, name, projection):
    """Validate an actual renderer projection, allowing additive JSON metadata."""
    schemas = contract["display_schemas"]
    schema = schemas[name]
    fields = dict(schemas[schema["extends"]]["fields"]) if "extends" in schema else {}
    fields.update(schema["fields"])
    errors = []
    for field, spec in fields.items():
        value = projection.get(field)
        expected = str if spec["type"] == "string" else list
        if not isinstance(value, expected):
            errors.append(f"{name} projection missing or invalid {field}: expected {spec['type']}")
    return errors


def check_history_json(contract, payload):
    """Check actual history machine output without requiring optional additions."""
    machine = contract["display_schemas"]["history_information"]["json"]
    kinds = {"string": str, "integer": int, "number": (int, float), "array": list, "object": dict}
    errors = []

    def fields(value, required, declared, label):
        if not isinstance(value, dict):
            errors.append(f"{label} must be an object")
            return False
        for field, kind in declared.items():
            if field not in required and field not in value:
                continue
            item = value.get(field)
            if not isinstance(item, kinds[kind]) or isinstance(item, bool) and kind in ("integer", "number"):
                errors.append(f"{label} missing or invalid {field}: expected {kind}")
        return True

    if not fields(payload, machine["envelope_required"], machine["envelope_types"], "history JSON"):
        return errors
    if payload.get("version") != machine["version"]:
        errors.append("history JSON changed its wire version")
    for record in payload.get("conversations", []) if isinstance(payload.get("conversations"), list) else []:
        if fields(record, machine["conversation_required"], machine["conversation_types"], "history conversation") and isinstance(record.get("display"), dict):
            errors.extend(check_display_projection(contract, machine["display_schema"], record["display"]))
    return errors


def _check_test_reference(reference, root):
    if not isinstance(reference, str) or "::" not in reference:
        return [f"invalid command contract test reference: {reference!r}"]
    relpath, test_name = reference.split("::", 1)
    if not relpath.startswith("tests/") or not test_name.startswith("test_"):
        return [f"invalid command contract test reference: {reference!r}"]
    if relpath in ("tests/test_command_patterns.py", "tests/test_cli_contract.py"):
        return [f"command contract needs a behavior test, not inventory-only test: {reference}"]
    path = root / relpath
    if not path.is_file():
        return [f"missing command contract test file: {relpath}"]
    try:
        tree = ast.parse(path.read_text())
    except SyntaxError:
        return [f"invalid command contract test file: {relpath}"]
    if not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and
               node.name == test_name for node in tree.body):
        return [f"missing command contract test: {reference}"]
    return []


_RESERVED_PICKER_OPTION = re.compile(
    r"(?:--(?:bind|header|no-header|multi|no-multi|marker|pointer|color|height|layout|reverse|border|margin|expect)(?:\b|=)|(?<!\S)-m(?:\s|$))")
_SHELL_FZF_LAUNCH = re.compile(
    r"(?:^|[|;]\s*|&&\s*|\$\(\s*|\bthen\s+)(?:command\s+)?fzf(?:\s|$)")
_LAUNCH_CALLS = {"run", "Popen", "call", "check_call", "check_output",
                 "_run", "command", "system", "execvp", "execvpe", "spawnvp", "spawnvpe"}


def _python_fzf_launches(source, relpath):
    """Find literal Python process launches without treating prose as a launch."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [f"{relpath} is not valid Python"]
    errors = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        call_name = node.func.attr if isinstance(node.func, ast.Attribute) else (
            node.func.id if isinstance(node.func, ast.Name) else "")
        if call_name not in _LAUNCH_CALLS:
            continue
        argv = node.args[0]
        if isinstance(argv, (ast.List, ast.Tuple)) and argv.elts:
            first = argv.elts[0]
            if isinstance(first, ast.Constant) and first.value == "fzf":
                # A version/capability probe is not a picker implementation.
                second = argv.elts[1] if len(argv.elts) > 1 else None
                if not (isinstance(second, ast.Constant) and second.value in ("--version", "--help")):
                    errors.append(f"{relpath}:{node.lineno} launches fzf outside ui/fzf.sh")
            elif (isinstance(first, ast.Constant) and first.value in ("sh", "bash", "zsh")
                  and len(argv.elts) >= 3):
                shell_body = argv.elts[2]
                if isinstance(shell_body, ast.Constant) and isinstance(shell_body.value, str) and \
                        _SHELL_FZF_LAUNCH.search(shell_body.value):
                    errors.append(f"{relpath}:{node.lineno} launches fzf outside ui/fzf.sh")
        elif isinstance(argv, ast.Constant) and isinstance(argv.value, str) and \
                _SHELL_FZF_LAUNCH.search(argv.value):
            errors.append(f"{relpath}:{node.lineno} launches fzf outside ui/fzf.sh")
    return errors


def check_picker_boundaries(*, root=ROOT):
    """Keep launches and controls in the shared picker implementation."""
    errors = []
    shell_paths = [*sorted((root / "zsh").glob("*.zsh")), root / "t.plugin.zsh"]
    if (root / "bin").exists():
        shell_paths.extend(sorted((root / "bin").iterdir()))
    for path in shell_paths:
        if not path.is_file():
            continue
        if path.suffix != ".zsh" and not path.read_bytes().startswith(
                (b"#!/bin/sh", b"#!/bin/bash", b"#!/usr/bin/env bash", b"#!/usr/bin/env zsh")):
            continue
        relpath = path.relative_to(root)
        for number, line in enumerate(path.read_text().splitlines(), 1):
            code = line.split("#", 1)[0]
            if _SHELL_FZF_LAUNCH.search(code):
                errors.append(f"{relpath}:{number} launches fzf outside ui/fzf.sh")
            if re.search(r"\b_t_fzf(?:\s|\()", code) and not re.search(r"\b_t_fzf_(?:single|multi|filtered)\b", code):
                errors.append(f"{relpath}:{number} bypasses a picker profile")
            if "_t_fzf_" in code and _RESERVED_PICKER_OPTION.search(code):
                errors.append(f"{relpath}:{number} overrides reserved picker controls")
    python_paths = [root / "bin/t", *sorted((root / "libexec").glob("*.py"))]
    for path in python_paths:
        if path.is_file():
            errors.extend(_python_fzf_launches(path.read_text(), path.relative_to(root)))
    return errors


def main():
    loader = SourceFileLoader("t_cli_lint", str(ROOT / "bin/t"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    errors = check(module.build_parser(), module.COMMAND_GROUPS,
                   (ROOT / "AGENTS.md").read_text())
    errors.extend(check_issue_help(module._group_help("issue")))
    errors.extend(check_group_help(module._group_help, module.COMMAND_GROUPS))
    try:
        contract = json.loads(CONTRACT.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"cannot read command inventory: {exc}")
    else:
        errors.extend(check_command_patterns(module.build_parser(), module.COMMAND_GROUPS, contract))
    errors.extend(check_picker_boundaries())
    for path in (ROOT / "AGENTS.md", ROOT / "CONTRIBUTING.md"):
        if "ui/command-contract.json" not in path.read_text():
            errors.append(f"{path.name} must link ui/command-contract.json")
    for error in errors:
        print("CLI contract: " + error, file=sys.stderr)
    if not errors:
        print("CLI noun/verb and issue action contract passed")
    return int(bool(errors))


if __name__ == "__main__":
    raise SystemExit(main())
