#!/usr/bin/env python3
"""Generate TEST ONLY protocol goldens through the actual Core and bridge handler.
Mutations are replaced by a fixture return to avoid creating sessions or touching HOME.
"""

import json
import os
from pathlib import Path
import sys
import tempfile

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
