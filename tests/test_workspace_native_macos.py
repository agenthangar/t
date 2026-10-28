"""Opt-in real WKWebView -> Rust IPC -> helper -> tmux/PTY integration.

The native debug example is the production workspace plus a test-only reporting
command; its helper and terminal transports are real, never fixture responses.
"""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from test_desktop_core_integration import lifecycle, lifecycle_environment, _launches, _tmux

ROOT = Path(__file__).resolve().parents[1]
pytestmark = [pytest.mark.macos_ui, pytest.mark.skipif(sys.platform != "darwin", reason="requires macOS WKWebView")]


def test_workspace_native_helper_and_terminal(lifecycle):
    base, env = lifecycle
    executable = Path(os.environ.get("T_DESKTOP_NATIVE_PROOF", str(ROOT / "desktop/src-tauri/target/debug/examples/native-proof")))
    if not executable.is_file():
        pytest.skip("build the native-proof Cargo example with native-proof,custom-protocol features first")
    # Rust builds a Finder-safe PATH through HOME/bin. Retain this fixture's
    # unique tmux socket and synthetic tools through that production path.
    (base / "home" / "bin").symlink_to(base / "bin", target_is_directory=True)
    tmux = base / "bin" / "tmux"
    tmux.write_text("#!/bin/sh\n" + tmux.read_text().splitlines()[-1] + "\n")
    for name in ("pgrep", "lsof"):
        probe = base / "bin" / name
        probe.write_text("#!/bin/sh\nexit 0\n")
        probe.chmod(0o755)
    for key in ("T_HOME", "T_LOCAL_RC", "CLAUDE_RESUME_ID", "CLAUDE_TPUSH_ATTACH"):
        env.pop(key, None)
    env["T_DESKTOP_PYTHON"] = sys.executable
    env["T_DESKTOP_NATIVE_PROOF_SANDBOX"] = str(base / "home")
    (base / "home" / ".native-proof-sandbox").touch()
    completed = subprocess.run([str(executable)], cwd=base, env=env,
                               capture_output=True, text=True, timeout=90)
    reports = [json.loads(line.removeprefix("NATIVE_PROOF:")) for line in completed.stdout.splitlines()
               if line.startswith("NATIVE_PROOF:")]
    stages = [line for line in completed.stdout.splitlines() if line.startswith("NATIVE_STAGE:")]
    assert reports and reports[-1].get("ok"), (reports, stages, completed.stderr[-1500:])
    assert completed.returncode == 0
    assert len(_launches(base)) == 1
    assert _tmux(base, env, "has-session", "-t", "=dev-repo-1:").returncode == 0
