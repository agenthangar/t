"""Existing repository names resolve at typed command boundaries, never payloads."""
import argparse
import copy
import json
from pathlib import Path
import runpy
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
SID = "12345678-1234-1234-1234-123456789abc"


@pytest.fixture
def cfg(t_mod, tmp_path, monkeypatch):
    monkeypatch.setattr(t_mod, "CONFIG", str(tmp_path / "missing-config"))
    value = t_mod.Config()
    value.repos = {"app": str(tmp_path / "application")}
    value.worktree_root = str(tmp_path / "worktrees")
    return value


@pytest.mark.parametrize("selector", ["app", "application"])
def test_alias_and_directory_name_use_registered_key_without_config_changes(cfg, selector):
    before = copy.deepcopy(vars(cfg))
    assert cfg.resolve_repo(selector) == "app"
    assert vars(cfg) == before


def test_resolver_exact_alias_precedence_and_real_checkout_ambiguity(cfg, tmp_path):
    cfg.repos.update(other=str(tmp_path / "elsewhere" / "application"))
    with pytest.raises(ValueError, match="ambiguous repository.*app, other"):
        cfg.resolve_repo("application")
    cfg.repos["application"] = str(tmp_path / "third")
    assert cfg.resolve_repo("application") == "application"
    assert cfg.resolve_repo("app") == "app"


def test_resolver_normalizes_configured_paths_and_uses_stable_alias_order(cfg, tmp_path):
    cfg.repos = {"zz": str(tmp_path / "name with spaces") + "/", "aa": str(tmp_path / "name with spaces")}
    assert cfg.resolve_repo("name with spaces") == "aa"
    assert cfg.worktree_path("aa", "2").endswith("/name with spaces/name with spaces-2")
    assert cfg.worktree_path("zz", "2") == cfg.worktree_path("aa", "2")
    (tmp_path / "name with spaces").mkdir()
    (tmp_path / "link").symlink_to(tmp_path / "name with spaces", target_is_directory=True)
    cfg.repos["z"] = str(tmp_path / "link" / ".." / "name with spaces")
    assert cfg.resolve_repo("name with spaces") == "z"
    cfg.repos["name with spaces"] = str(tmp_path / "other")
    assert cfg.resolve_repo("name with spaces") == "name with spaces"
    assert cfg.resolve_repo("missing") is None
    assert cfg.resolve_repo(None) is None
    with pytest.raises(ValueError, match="unknown repository.*missing"):
        cfg.resolve_repo("missing", required=True)
    cfg.repos = {}
    with pytest.raises(ValueError, match="none registered"):
        cfg.resolve_repo("missing", required=True)


def test_real_checkout_grouping_preserves_symlink_before_parent_semantics(cfg, tmp_path):
    (tmp_path / "root" / "application").mkdir(parents=True)
    (tmp_path / "target" / "nested").mkdir(parents=True)
    (tmp_path / "target" / "application").mkdir()
    (tmp_path / "root" / "link").symlink_to(tmp_path / "target" / "nested", target_is_directory=True)
    cfg.repos = {"a": str(tmp_path / "root" / "link" / ".." / "application"),
                 "b": str(tmp_path / "target" / "application")}
    assert cfg.resolve_repo("application") == "a"
    cfg.repos["c"] = str(tmp_path / "root" / "application")
    with pytest.raises(ValueError, match="ambiguous repository"):
        cfg.resolve_repo("application")


@pytest.mark.parametrize("selector", ["app", "application"])
def test_readonly_list_locate_and_mcp_scope_match(t_mod, cfg, selector, capsys):
    directory = cfg.repos["app"]
    assert t_mod._ls_scope(cfg, selector, False, "/outside") == (
        directory, str(Path(cfg.worktree_root) / "application"))
    assert t_mod.cmd_repo(cfg, SimpleNamespace(action="locate", repo=selector)) == 0
    assert capsys.readouterr().out.strip() == directory
    assert t_mod._repo_path(cfg, selector) == directory
    ctx = dict(recs=[], live={}, idle=[], now=1, cache_root="/unused", this_sid=None)
    assert t_mod._tool_list(cfg, ctx, {"repo": selector})["count"] == 0
    result = t_mod._find_sessions([], ["application"], {}, cfg, repo=selector)
    assert result["query_terms"] == ["application"] and result["matched"] == 0


@pytest.mark.parametrize("selector", ["app", "application"])
def test_slot_controls_refs_read_and_goal_use_actual_alias(t_mod, cfg, selector, tmp_path, monkeypatch, capsys):
    cwd = cfg.worktree_path("app", "2")
    row = dict(host="local", slot="app-2", sid=SID, cwd=cwd, agent="codex", context="tmux")
    assert t_mod._slot_select(cfg, [row], selector, "2", "/outside", "restart") is row
    record = dict(sid=SID, cwd=cwd)
    assert t_mod._session_ref_resolve(selector + "-2", cfg, [record], {SID: row}) is record
    monkeypatch.setattr(t_mod, "HOME", str(tmp_path))
    logs = tmp_path / ".tmux-logs"
    logs.mkdir()
    (logs / "dev-app-2.log").write_text("application and app remain literal\n")
    args = t_mod.build_parser().parse_args(["read", selector, "2", "--dump"])
    assert t_mod.cmd_read(cfg, args) == 0
    assert capsys.readouterr().out.strip() == "application and app remain literal"
    seen = []
    monkeypatch.setattr(t_mod, "zsh_capture", lambda snippet: "ignored")
    monkeypatch.setattr(t_mod, "_parse_rows", lambda raw: [row])
    monkeypatch.setattr(t_mod, "_mcp_controls", lambda: {"call": lambda *args: seen.append(args[2]) or {}})
    goal = "app application ls list"
    assert t_mod.cmd_session_goal(cfg, SimpleNamespace(repo=selector, slot="2", goal=goal, json=True)) == 0
    assert seen == [{"repo": "app", "slot": 2, "session_id": SID, "goal": goal}]


@pytest.mark.parametrize("argv", [
    ["config", "workflow", "list", "--repo", "application", "--json"],
    ["config", "--show", "--workflow", "--repo", "application"],
])
def test_workflow_scope_resolves_name_without_saving(t_mod, cfg, argv, monkeypatch, capsys):
    helper = runpy.run_path(str(ROOT / "libexec/t_workflow.py"))
    helper["load"] = lambda: ({"global": helper["DEFAULT"], "repositories": {}, "version": 1}, None)
    helper["save"] = lambda *args: pytest.fail("read-only workflow saved")
    monkeypatch.setattr(t_mod, "_workflow", lambda: helper)
    args = t_mod.build_parser().parse_args(argv)
    assert t_mod.cmd_config(cfg, args) == 0
    assert json.loads(capsys.readouterr().out)["scope"] == cfg.repos["app"]


@pytest.mark.parametrize("selector", ["app", "application"])
def test_profile_apply_and_issue_and_history_boundaries(t_mod, cfg, selector, monkeypatch, tmp_path):
    Path(cfg.repos["app"]).mkdir()
    calls = []
    monkeypatch.setattr(t_mod, "_instructions_offer", lambda *args: calls.append(args) or 0)
    args = t_mod.build_parser().parse_args(["instructions", selector, "--apply"])
    assert t_mod.cmd_instructions(cfg, args) == 0
    assert calls[0][0] == [cfg.repos["app"]]
    issue = runpy.run_path(str(ROOT / "libexec/t_issue.py"))
    issue["repository"].__globals__["command"] = lambda words: "git@github.com:org/application.git"
    for value in (selector, "org/application"):
        result = issue["repository"](value, cfg)
        assert result["alias"] == "app" and result["repo"] == "org/application"
    assert issue["target"]([selector, "12"], cfg)["directory"] == cfg.repos["app"]
    assert issue["target"](["https://github.com/org/application/issues/12"], cfg)["number"] == 12
    history = runpy.run_path(str(ROOT / "libexec/t_history.py"))
    app = history["HistoryCommand"](cfg, {"_t_source_root": lambda: str(ROOT)}, store=object())
    assert app.scope([selector, "2"]) == ("app", "2")


def test_typed_mcp_repo_resolves_without_rewriting_instruction_payload(cfg, monkeypatch):
    controls = runpy.run_path(str(ROOT / "libexec/t_mcp_controls.py"))
    args = {"repo": "application", "slot": 2, "session_id": SID,
            "instructions": "app application ls list"}
    assert controls["validate"](cfg, "send_instructions", args) == ("app", "2")
    assert args["repo"] == "application" and args["instructions"] == "app application ls list"
    calls = []
    controls["call"].__globals__["shell_run"] = lambda words: calls.append(words) or "opened"
    assert controls["call"](cfg, "open_session", {"repo": "application", "slot": 2})["repo"] == "app"
    assert calls == [["_t_open", "app", "2", "--cli"]]


def test_ambiguous_name_stops_python_actions_before_discovery_or_mutation(t_mod, cfg, tmp_path, monkeypatch, capsys):
    cfg.repos["other"] = str(tmp_path / "other" / "application")
    monkeypatch.setattr(t_mod, "zsh_capture", lambda *args: pytest.fail("ambiguous target scanned"))
    assert t_mod.cmd_read(cfg, t_mod.build_parser().parse_args(["read", "application", "2"])) == 1
    assert "ambiguous repository" in capsys.readouterr().err
    assert t_mod.cmd_repo(cfg, SimpleNamespace(action="locate", repo="application")) == 1
    assert "app, other" in capsys.readouterr().err
    with pytest.raises(ValueError, match="ambiguous repository"):
        t_mod._slot_select(cfg, [], "application", "2", "/outside", "restart")
    monkeypatch.setattr(t_mod, "_workflow", lambda: pytest.fail("ambiguous workflow loaded"))
    args = t_mod.build_parser().parse_args(["config", "workflow", "edit", "--repo", "application"])
    assert t_mod.cmd_config(cfg, args) == 1
    assert "ambiguous repository" in capsys.readouterr().err
    monkeypatch.setattr(t_mod, "_instructions_offer", lambda *args: pytest.fail("ambiguous profile applied"))
    args = t_mod.build_parser().parse_args(["instructions", "application", "--apply"])
    assert t_mod.cmd_instructions(cfg, args) == 1
    assert "ambiguous repository" in capsys.readouterr().err
    issue = runpy.run_path(str(ROOT / "libexec/t_issue.py"))
    with pytest.raises(issue["IssueError"], match="ambiguous repository"):
        issue["repository"]("application", cfg)
    history = runpy.run_path(str(ROOT / "libexec/t_history.py"))
    app = history["HistoryCommand"](cfg, {"_t_source_root": lambda: str(ROOT)}, store=object())
    with pytest.raises(history["HistoryError"], match="ambiguous repository"):
        app.scope(["application", "2"])


def test_repo_selector_route_inventory_tracks_every_public_parser_boundary(t_mod):
    """New repo-taking routes need an explicit resolution seam in the contract."""
    contract = json.loads((ROOT / "ui/command-contract.json").read_text())
    tracked = set(contract["repository_selectors"])
    found = set()
    for path, route in t_mod._command_routes():
        parser = t_mod.build_parser()
        for word in route:
            if word.startswith("-"):
                break
            children = next((a for a in parser._actions if isinstance(a, argparse._SubParsersAction)), None)
            if not children:
                break
            parser = children._name_parser_map[word]
        fields = {a.dest for a in parser._actions}
        if fields & {"repo", "workflow_repo"} or path == ("session", "history"):
            found.add(" ".join(path))
    assert found == tracked
    assert all(contract["repository_selectors"][path] for path in tracked)
