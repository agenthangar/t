"""Inspect or explicitly add this app's sessions MCP registration."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

BIN = Path(__file__).resolve().parent
RN = str(BIN / "rn")


def run(args, payload=None):
    return subprocess.run(args, input=payload or "", capture_output=True, timeout=8,
                          text=True, cwd="/", check=True).stdout


def registration(client, executable):
    if client == "codex":
        entries = json.loads(run([executable, "mcp", "list", "--json"]))
        return next((entry for entry in entries if entry["name"] == "sessions"), None)
    path = Path.home() / ".claude.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    if "sessions" in data.get("disabledMcpServers", []):
        return {"enabled": False}
    return data.get("mcpServers", {}).get("sessions")


def add(client, executable, command):
    args = [executable, "mcp", "add"]
    if client == "claude":
        args += ["--scope", "user"]
    run([*args, "sessions", "--", command, "mcp"])


def known_legacy(command):
    if not command or not os.path.isabs(command):
        return False
    old = Path(command).resolve()
    root = old.parent.parent
    candidates = [Path.home() / "bin/t", Path("/opt/homebrew/bin/t"), Path("/usr/local/bin/t")]
    return (old.name == "t" and (root / ".t-install-version").is_file()
            and (root / "libexec/t_mcp_setup.py").is_file()
            and any(old == candidate.resolve() for candidate in candidates))


def state(client, action):
    executable = shutil.which(client)
    if not executable:
        return "missing"
    entry = registration(client, executable)
    if entry is None:
        if action != "connect":
            return "available"
        login = ["login", "status"] if client == "codex" else ["auth", "status"]
        if subprocess.run([executable, *login], capture_output=True, timeout=8,
                          stdin=subprocess.DEVNULL, cwd="/").returncode != 0:
            return "signIn"
        add(client, executable, RN)
        entry = registration(client, executable)
        if not entry:
            return "unverified"
    transport = entry.get("transport", entry)
    if entry.get("enabled") is False or entry.get("disabled_reason"):
        return "disabled"
    if (transport.get("args") != ["mcp"]
            or transport.get("type", "stdio") != "stdio"
            or any(transport.get(key) for key in ("env", "cwd", "env_vars"))):
        return "custom"
    command = transport.get("command")
    if command != RN:
        # Per-tool policies and customized launch options belong to the user.
        if any(entry.get(key) for key in ("enabled_tools", "disabled_tools", "tool_timeout_sec", "startup_timeout_sec")):
            return "custom"
        if not known_legacy(command):
            return "custom"
        if action != "migrate":
            return "legacy"
        # Re-read immediately before changing the owned entry. Restore it if
        # the new registration fails and the name is still free.
        if registration(client, executable) != entry:
            return "unverified"
        remove = [executable, "mcp", "remove", "sessions"]
        if client == "claude":
            remove += ["--scope", "user"]
        run(remove)
        try:
            if registration(client, executable) is not None:
                return "custom"
            add(client, executable, RN)
        except subprocess.SubprocessError:
            if registration(client, executable) is None:
                add(client, executable, command)
            raise
        return state(client, "check")
    messages = [
        {"id": 1, "method": "initialize", "params": {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "rnnr", "version": "1"}}},
        {"method": "notifications/initialized"},
        {"id": 2, "method": "tools/list"},
    ]
    payload = "".join(json.dumps({"jsonrpc": "2.0", **message}) + "\n" for message in messages)
    responses = {r["id"]: r for r in map(json.loads, run([RN, "mcp"], payload).splitlines())}
    if "tools" in responses[1]["result"]["capabilities"] and any(
            tool["name"] == "list_sessions" for tool in responses[2]["result"]["tools"]):
        return "connected"
    return "unverified"


if __name__ == "__main__":
    client, action = sys.argv[1:]
    if client not in {"codex", "claude"} or action not in {"check", "connect"}:
        sys.exit(2)
    home = Path.home()
    os.environ["PATH"] = os.pathsep.join(map(str, [BIN, home / ".local/bin", "/opt/homebrew/bin",
                                                  "/usr/local/bin", os.defpath]))
    try:
        result = state(client, action)
    except (OSError, ValueError, TypeError, KeyError, subprocess.SubprocessError):
        result = "unverified"
    print(json.dumps(result))
