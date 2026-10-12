#!/usr/bin/env python3
"""Headless relocation smoke. Never opens the application or developer tmux."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile


def smoke(app):
    with tempfile.TemporaryDirectory(prefix="t-app-relocated-") as directory:
        destination = Path(directory) / "Relocated Workspace.app"
        shutil.copytree(app, destination, symlinks=True)
        runtime = destination / "Contents/Resources/runtime"
        core, python = runtime / "core", runtime / "python/bin/python3"
        for file in runtime.rglob("*"):
            if file.is_symlink():
                resolved = file.resolve()
                if not resolved.is_relative_to(runtime):
                    raise SystemExit(
                        "Bundle contains an external symlink: " + str(file)
                    )
        assert os.access(python, os.X_OK) and os.access(core / "bin/t", os.X_OK), (
            "Runtime executable bits were lost"
        )
        home = Path(directory) / "home"
        home.mkdir()
        (home / "tmux").mkdir(mode=0o700)
        (home / "local.zsh").write_text("")
        # macOS has no process namespace: suppress unrelated foreground agents.
        probes = home / "process-probes"
        probes.mkdir()
        for name in ("ps", "pgrep", "lsof"):
            probe = probes / name
            probe.write_text("#!/bin/sh\nexit 0\n")
            probe.chmod(0o755)
        # This synthetic startup file contains no developer config or plugin code.
        (home / ".zshrc").write_text('export PATH="$HOME/process-probes:$PATH"\n')

        env = {
            "HOME": str(home),
            "ZDOTDIR": str(home),
            "T_LOCAL_RC": str(home / "local.zsh"),
            "TMUX_TMPDIR": str(home / "tmux"),
            "XDG_CACHE_HOME": str(home / "cache"),
            "XDG_CONFIG_HOME": str(home / "config"),
            "XDG_STATE_HOME": str(home / "state"),
            "PATH": str(probes) + ":/usr/bin:/bin:/usr/sbin:/sbin",
            "T_NO_UPDATE_CHECK": "1",
        }
        requests = "".join(
            json.dumps({"version": 1, "id": i, "method": method, "params": {}}) + "\n"
            for i, method in enumerate(
                ["initialize", "workspace.snapshot", "system.diagnostics"], 1
            )
        )
        response = subprocess.run(
            [str(python), str(core / "libexec/t_bridge.py")],
            input=requests,
            text=True,
            capture_output=True,
            env=env,
            cwd=home,
            check=True,
            timeout=45,
        )
        replies = [
            json.loads(line)
            for line in response.stdout.splitlines()
            if "event" not in json.loads(line)
        ]
        assert len(replies) == 3 and all("result" in reply for reply in replies), (
            response.stdout
        )
        assert replies[0]["result"]["protocolVersion"] == 1
        assert (
            not replies[1]["result"]["sessions"]
            and not replies[1]["result"]["repositories"]
            and not replies[1]["result"]["hosts"]
        ), "Bundle smoke isolation failed"
        script = "import sys;sys.path.insert(0,sys.argv[1]);from t_core import Core;import subprocess;c=Core();p=subprocess.run(c.shell('whence -p ps'),capture_output=True,text=True);assert p.stdout.strip().endswith('/process-probes/ps');r=subprocess.run(c.shell('t --version'),capture_output=True,text=True);assert r.returncode==0;assert 'v0.1.0' in r.stdout"
        subprocess.run(
            [str(python), "-c", script, str(core / "libexec")],
            env=env,
            cwd=home,
            check=True,
            timeout=45,
            capture_output=True,
        )
        print(
            "Relocated .app helper, snapshot, diagnostics, and pinned CLI shell smoke passed with fresh HOME and Finder PATH."
        )


if __name__ == "__main__":
    smoke(
        Path(
            sys.argv[1]
            if len(sys.argv) > 1
            else "desktop/src-tauri/target/release/bundle/macos/t Workspace.app"
        ).resolve()
    )
