#!/usr/bin/env python3
"""Generate TEST ONLY protocol goldens through the actual Core and bridge handler.
Mutations are replaced by a fixture return to avoid creating sessions or touching HOME.
"""

import json
import os
from pathlib import Path
import sys
import tempfile
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "libexec"))
from t_core import Core
from t_bridge import handle

with tempfile.TemporaryDirectory(prefix="t-desktop-golden-") as home:
    # Do not inherit T_LOCAL_RC, ZDOTDIR, agent homes, or a developer tmux socket.
    task_path = os.environ.get("PATH", "/usr/bin:/bin")
    os.environ.clear()
    os.environ.update(
        HOME=home,
        PATH=task_path,
        ZDOTDIR=home,
        TMUX_TMPDIR=home + "/tmux",
        T_LOCAL_RC=home + "/local.zsh",
        XDG_CONFIG_HOME=home + "/config",
        XDG_CACHE_HOME=home + "/cache",
        XDG_STATE_HOME=home + "/state",
        T_NO_UPDATE_CHECK="1",
    )
    Path(home + "/tmux").mkdir(mode=0o700)
    Path(home + "/local.zsh").write_text("")
    core = Core(state_root=Path(home) / "operations")
    # Goldens exercise real bridge/operation serialization with injected read/effect
    # adapters; never run host discovery, process scans, SSH, or developer config.
    cfg = core.cli.Config()
    cfg.branch = "main"
    cfg.worktree_root = "/tmp/t-desktop-fixture/worktrees"
    core.config = lambda: cfg
    core.sessions = lambda config, all_hosts=False: []
    core.cli._t_version = lambda: "v0.1.0"

    # Destructive-operation review captures an owner before serialization. Supply
    # entirely synthetic metadata here; never inspect a live tmux pane or agent.
    core.capture_owner = lambda config, arguments, expected=None: {
        "session": "dev-fixture-1",
        "identity": "100\t$1\t%1\t200\t/tmp/t-desktop-fixture/worktrees/project/1",
        "host": "local",
        "target": None,
        "sid": "22222222-2222-4222-8222-222222222222",
        "agent": "codex",
        "clientPid": "300",
    }

    def call(method, params):
        response = handle(
            core, {"version": 1, "id": 1, "method": method, "params": params}
        )
        if "error" in response:
            raise RuntimeError(response["error"])
        return response["result"]

    golden = {
        "initialize": call("initialize", {}),
        "snapshot": call("workspace.snapshot", {}),
    }
    assert (
        golden["snapshot"]["sessions"] == []
        and golden["snapshot"]["repositories"] == []
        and golden["snapshot"]["hosts"] == []
    ), "Fixture isolation failed; refusing to export developer state"
    # Inject repository/provider reads before issue calls; aggregation, sanitized
    # errors, and bridge serialization still come from the real core.
    class FixtureIssueCommand:
        repositories = []

        def __init__(self, config, api):
            pass

        def list_repositories(self, repo, all_repos=False):
            assert repo is None and all_repos
            return self.repositories, None, []

        def issues(self, repo, state):
            assert state == "open"
            if repo == "fixture/unavailable":
                raise OSError("synthetic unavailable provider")
            return [{"repo": repo, "number": 12, "state": "OPEN",
                     "title": "Synthetic workspace task",
                     "url": "https://github.com/fixture/project/issues/12"}]

    with patch("t_core.runpy.run_path", return_value={"IssueCommand": FixtureIssueCommand}):
        golden["issues.list.empty"] = call("issues.list", {"all": True})
        FixtureIssueCommand.repositories = ["fixture/project", "fixture/unavailable"]
        golden["issues.list.partial"] = call("issues.list", {"all": True, "state": "open"})
        FixtureIssueCommand.repositories = ["fixture/unavailable"]
        golden["issues.list.unavailable"] = call("issues.list", {"all": True})
    core.attachment = lambda session, cwd=None: {
        "kind": "terminal",
        "argv": ["tmux", "attach-session", "-t", "=" + session + ":"],
        "cwd": cwd or home,
        "env": {},
        "detachKeepsSession": True,
    }
    rec = {
        "sid": "22222222-2222-4222-8222-222222222222",
        "cwd": "/tmp/original-fixture",
        "n_prompts": 1,
        "prompts": [{"text": "Fixture shared workspace search", "ts": 1700000000}],
        "first_ts": 1700000000,
        "last_ts": 1700000000,
    }
    golden["search"] = core.cli._find_sessions(
        [rec], ["workspace"], {}, cfg, days=0, now=1700000001
    )
    core._mutate = lambda method, args: (
        {
            "sessions": [{"host": "local", "session": "dev-fixture-1"}],
            "attachments": [
                core.attachment("dev-fixture-1", home),
                core.attachment("dev-fixture-2", home),
            ],
        }
        if method == "issues.start"
        else {
            "session": "dev-fixture-1",
            "attachment": core.attachment("dev-fixture-1", home)
            if method != "sessions.close"
            else None,
        }
    )
    for method, params in [
        ("sessions.open", {"repo": "fixture", "new": True}),
        ("sessions.resume", {"repo": "fixture", "slot": "1"}),
        ("sessions.restart", {"repo": "fixture", "slot": "1"}),
        ("sessions.close", {"repo": "fixture", "slot": "1"}),
        ("issues.start", {"targets": ["fixture", "12", "34"]}),
    ]:
        prepared = call(method, params)
        applied = call("operations.apply", {"operationId": prepared["operationId"]})
        golden[method] = {"prepared": prepared, "applied": applied}
    mcp_prepared = call(
        "operations.prepare", {"command": ["mcp", "--install"], "cwd": home}
    )
    golden["mcp.install"] = {
        "prepared": mcp_prepared,
        "applied": call(
            "operations.apply", {"operationId": mcp_prepared["operationId"]}
        ),
    }
    for name, flag in (("mcp.listTools", "--list-tools"), ("mcp.reindex", "--reindex")):
        golden[name] = call(
            "operations.prepare", {"command": ["mcp", flag], "cwd": home}
        )
    # Share send is the standalone public syntax, not a fictional send verb.
    # Preparation validates real parser argv but never sends context or reads a
    # summary. Apply only exposes the reviewed terminal plan as ready.
    share_commands = {
        "share.send": ["share", "--to", "codex:22222222-2222-4222-8222-222222222222",
                       "--file", "/tmp/t-desktop-fixture/summary.txt"],
        "share.targets": ["share", "targets", "--json"],
        "share.inbox": ["share", "inbox", "--for", "codex:22222222-2222-4222-8222-222222222222"],
        "share.read": ["share", "read", "fixture-message",
                       "--for", "codex:22222222-2222-4222-8222-222222222222"],
    }
    for name, command in share_commands.items():
        prepared = call("operations.prepare", {"command": command, "cwd": home})
        golden[name] = {
            "prepared": prepared,
            "applied": call("operations.apply", {"operationId": prepared["operationId"]}),
        }
    golden["session.history"] = call(
        "operations.prepare", {"command": ["session", "history"], "cwd": home}
    )
    expected_share = {
        "share.send": ["share"], "share.targets": ["share", "targets"],
        "share.inbox": ["share", "inbox"], "share.read": ["share", "read"],
    }
    catalog = {entry["method"]: entry for entry in golden["initialize"]["capabilities"]}
    assert "session.history" in catalog
    for method, command in expected_share.items():
        assert catalog[method]["command"] == command and catalog[method]["kind"] == "terminal"
        assert golden[method]["applied"]["state"] == "ready"
    assert golden["issues.list.empty"]["issues"] == [] and not golden["issues.list.empty"]["incomplete"]
    assert len(golden["issues.list.partial"]["issues"]) == 1 and golden["issues.list.partial"]["incomplete"]
    assert golden["issues.list.unavailable"]["issues"] == [] and golden["issues.list.unavailable"]["incomplete"]
    golden["terminal"] = call(
        "operations.prepare", {"command": ["session", "view-plan"], "cwd": home}
    )
    text = json.dumps(golden, indent=2).replace(home, "/tmp/t-desktop-fixture")
    # Keep goldens deterministic and public; operation IDs are synthetic metadata.
    import re

    text = re.sub(
        r'(?<="operationId": ")[0-9a-f-]{36}',
        "11111111-1111-4111-8111-111111111111",
        text,
    )
    text = re.sub(r'(?<="fingerprint": ")[0-9a-f]{64}', "0" * 64, text)
    text = text.replace(str(ROOT), "/fixture/core").replace(
        str(Path(sys.executable).parent), "/fixture/python/bin"
    )
    (ROOT / "desktop/tests/fixtures/core-protocol.json").write_text(text + "\n")
