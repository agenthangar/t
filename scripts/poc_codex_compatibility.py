#!/usr/bin/env python3
"""Verify t open --new bypasses shared feature conflicts without credentials or turns.

Requires Codex 0.160.0, zsh, and tmux. All config, sockets, processes, and pane
captures are disposable. Only the attachment UI is skipped; t creates the slots.
"""

import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time
import tomllib

from poc_desktop_shared_backend import connect, stop

ROOT = Path(__file__).resolve().parent.parent


def until(check):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.1)
    raise RuntimeError("isolated compatibility probe timed out")


def run(binary):
    binary = str(Path(binary).resolve())
    tmux_binary = shutil.which("tmux")
    zsh_binary = shutil.which("zsh")
    if not tmux_binary or not zsh_binary:
        raise RuntimeError("tmux and zsh are required")
    with tempfile.TemporaryDirectory(prefix="t-compat-", dir="/tmp") as temporary:
        root = Path(temporary).resolve()
        env = {"PATH": os.environ.get("PATH", os.defpath), "TERM": "xterm-256color",
               "SHELL": zsh_binary, "T_RECOVERY_DISABLE": "1", "T_AUTO_TRUST": "0",
               "LANG": "en_US.UTF-8", "GIT_CONFIG_NOSYSTEM": "1"}
        for key in ("HOME", "CODEX_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME",
                    "XDG_STATE_HOME", "XDG_DATA_HOME", "TMPDIR", "ZDOTDIR"):
            directory = root / key.lower()
            directory.mkdir()
            env[key] = str(directory)
        work = root / "work"
        work.mkdir()
        bins = root / "bin"
        bins.mkdir()
        (bins / "codex").symlink_to(binary)
        socket = root / "tmux.sock"
        wrapper = bins / "tmux"
        wrapper.write_text("#!/bin/sh\nexec " + shlex.join(
            [tmux_binary, "-S", str(socket), "-f", os.devnull]) + ' "$@"\n')
        wrapper.chmod(0o755)
        env["PATH"] = os.pathsep.join([str(bins), str(ROOT / "bin"), env["PATH"]])
        home = Path(env["CODEX_HOME"])
        config = home / "config.toml"

        def configure(enabled):
            config.write_text(
                'model_provider="poc"\nmodel="poc"\ncheck_for_update_on_startup=false\n'
                '[features]\nplugins=false\ndaemon_auto_start=true\n'
                'api_key_model_discovery=' + str(enabled).lower() + '\n'
                '[model_providers.poc]\nname="No inference"\n'
                'base_url="http://127.0.0.1:9/v1"\nwire_api="responses"\n'
                'requires_openai_auth=false\n[projects.' + json.dumps(str(work)) + ']\n'
                'trust_level="trusted"\n')

        configure(True)
        zdot = Path(env["ZDOTDIR"])
        (zdot / ".zshenv").write_text("skip_global_compinit=1\n")
        # macOS path_helper reorders PATH during login. Restore the isolated
        # wrappers inside .zshrc before sourcing t or starting any pane command.
        (zdot / ".zshrc").write_text(
            "export PATH=" + shlex.quote(env["PATH"]) + "\nsource " +
            shlex.quote(str(ROOT / "t.plugin.zsh")) + "\nDEV_REPOS[demo]=" +
            shlex.quote(str(work)) + "\nDEV_WORKTREE[demo]=0\nDEV_AGENT[demo]=codex\n"
            "_dev_repo_prepare() { :; }\n"
            'tmux() { [[ $1 == attach-session ]] && return 0; command tmux "$@"; }\n')

        def tmux(*args):
            return subprocess.run([str(wrapper), *args], env=env, capture_output=True,
                                  text=True, timeout=10, check=True).stdout

        def shell(command):
            return subprocess.run([zsh_binary, "-lic", command], env=env, cwd=work,
                                  capture_output=True, text=True, timeout=30, check=True)

        def screen(slot):
            return tmux("capture-pane", "-p", "-J", "-t", "=dev-demo-" + str(slot) + ":")

        subprocess.run(["git", "init", "-q", str(work)], env=env, check=True)
        server = rpc = None
        try:
            tmux("new-session", "-d", "-s", "probe-holder", "sleep 120")
            tmux("set-option", "-g", "default-shell", zsh_binary)
            with (root / "server.log").open("w") as errors:
                server = subprocess.Popen(
                    [binary, "app-server", "--enable", "api_key_model_discovery", "--listen", "unix://"],
                    env=env, cwd=work, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=errors, start_new_session=True)
                control = home / "app-server-control/app-server-control.sock"
                until(control.exists)
                rpc = connect(str(control), "t_compatibility_probe")
                # Seed an unrelated shared-server client. New t slots now use
                # private backends even when the shared settings happen to match.
                tmux("new-session", "-d", "-s", "peer", "-c", str(work),
                     zsh_binary, "-lic", "codex")
                until(lambda: rpc.request("thread/loaded/list")["data"])
                peer = tmux("display-message", "-p", "-t", "=peer:", "#{pane_pid}")
                loaded = rpc.request("thread/loaded/list")["data"]
                shell("t open demo --new")
                until(lambda: "poc default" in screen(1))
                assert "incompatible" not in screen(1)
                assert rpc.request("thread/loaded/list")["data"] == loaded
                print("compatible t open --new: ready on private backend")

                configure(False)
                client_config = tomllib.loads(config.read_text())
                shell("t open demo --new")
                until(lambda: "poc default" in screen(2))
                assert "incompatible" not in screen(2)
                # Codex itself appends first-run TUI acknowledgements. All
                # declared provider, project, and feature settings must survive.
                current_config = tomllib.loads(config.read_text())
                assert all(current_config[key] == value for key, value in client_config.items())
                assert server.poll() is None
                assert rpc.request("thread/loaded/list")["data"] == loaded
                assert tmux("display-message", "-p", "-t", "=peer:", "#{pane_pid}") == peer
                for sid in loaded:
                    assert rpc.request("thread/read", {"threadId": sid, "includeTurns": False})["thread"]["id"] == sid
                features = rpc.request("experimentalFeature/list", {"limit": 100})["data"]
                assert any(item["name"] == "api_key_model_discovery" and item["enabled"]
                           for item in features)
                print("incompatible shared settings: new client ready without recovery; "
                      "shared server, settings, and peer preserved; no turns")
        finally:
            subprocess.run([str(wrapper), "kill-server"], env=env, capture_output=True, timeout=10)
            if rpc is not None:
                rpc.sock.close()
            stop(server)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--codex", default=shutil.which("codex"), help="Codex 0.160.0 executable")
    args = parser.parse_args()
    if not args.codex:
        parser.error("codex is required")
    run(args.codex)
