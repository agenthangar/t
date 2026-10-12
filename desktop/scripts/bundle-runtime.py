#!/usr/bin/env python3
"""Stage a relocatable, checksum-pinned Python and curated shared-core resources."""

import hashlib
import json
import platform
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request

ROOT = Path(__file__).resolve().parents[2]
DEST = ROOT / "desktop/src-tauri/resources"
VERSION = "3.13.16"
RELEASE = "20261009"
HASHES = {
    "arm64": "11785155d0f70e634178219c20925d8f20be00a3da4684fc4f5019dd15602ef7",
    "x86_64": "bd085c51e960f00ee33b630e2b13ac90fe90cdc3da25a0f0bc0719b1294920bf",
}


def main():
    if platform.system() != "Darwin" or platform.machine() not in HASHES:
        raise SystemExit("Desktop packaging currently supports macOS arm64 and x86_64.")
    arch = "aarch64" if platform.machine() == "arm64" else "x86_64"
    name = (
        f"cpython-{VERSION}+{RELEASE}-{arch}-apple-darwin-install_only_stripped.tar.gz"
    )
    cache = ROOT / "desktop/.runtime-cache"
    cache.mkdir(exist_ok=True)
    archive = cache / name
    if not archive.exists():
        urllib.request.urlretrieve(
            f"https://github.com/astral-sh/python-build-standalone/releases/download/{RELEASE}/{name}",
            archive,
        )
    if hashlib.sha256(archive.read_bytes()).hexdigest() != HASHES[platform.machine()]:
        raise SystemExit("Standalone Python checksum mismatch; refusing to package.")
    if DEST.exists():
        shutil.rmtree(DEST)
    DEST.mkdir(parents=True)
    with tarfile.open(archive) as tar:
        tar.extractall(DEST, filter="data")
    core = DEST / "core"
    core.mkdir()
    # Only tracked runtime sources; never package user config, local tests, or transcripts.
    files = (
        subprocess.check_output(
            [
                "git",
                "ls-files",
                "-z",
                "bin",
                "libexec",
                "zsh",
                "claude",
                "codex",
                "t.plugin.zsh",
                "install.sh",
                "local.zsh.example",
                "ui",
                "scripts/install-release.py",
                ".t-install-version",
                "LICENSE",
            ],
            cwd=ROOT,
        )
        .decode()
        .split("\0")
    )
    # Newly added shared-core modules are intentionally included before their first commit.
    files += ["libexec/t_core.py", "libexec/t_bridge.py"]
    for name in sorted(set(files)):
        if not name or name.endswith(".pyc"):
            continue
        source = ROOT / name
        if not source.is_file():
            continue
        target = core / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    desktop_version = json.loads((ROOT / "desktop/package.json").read_text())["version"]
    (core / ".t-release-version").write_text("v" + desktop_version + "\n")
    (core / ".t-install-version").write_text("1\n")
    (DEST / "runtime-manifest.json").write_text(
        json.dumps(
            {
                "python": VERSION,
                "release": RELEASE,
                "sha256": HASHES[platform.machine()],
                "source": "https://github.com/astral-sh/python-build-standalone",
                "desktopVersion": desktop_version,
                "sourceCommit": subprocess.check_output(
                    ["git", "rev-parse", "HEAD"], cwd=ROOT
                )
                .decode()
                .strip(),
            },
            indent=2,
        )
    )
    # Fresh HOME and restricted Finder-like PATH; no source checkout on sys.path.
    with tempfile.TemporaryDirectory(prefix="t-desktop-smoke-") as home:
        Path(home + "/tmux").mkdir(mode=0o700)
        Path(home + "/local.zsh").write_text("")
        probes = Path(home) / "process-probes"
        probes.mkdir()
        for name in ("ps", "pgrep", "lsof"):
            probe = probes / name
            probe.write_text("#!/bin/sh\nexit 0\n")
            probe.chmod(0o755)
        # Login path_helper prepends system tools; re-pin TEST ONLY process probes.
        Path(home + "/.zshrc").write_text('export PATH="$HOME/process-probes:$PATH"\n')

        env = {
            "HOME": home,
            "ZDOTDIR": home,
            "T_LOCAL_RC": home + "/local.zsh",
            "TMUX_TMPDIR": home + "/tmux",
            "XDG_CACHE_HOME": home + "/cache",
            "XDG_CONFIG_HOME": home + "/config",
            "XDG_STATE_HOME": home + "/state",
            "PATH": str(probes) + ":/usr/bin:/bin:/usr/sbin:/sbin",
            "T_NO_UPDATE_CHECK": "1",
        }
        messages = "".join(
            json.dumps({"version": 1, "id": i, "method": method, "params": {}}) + "\n"
            for i, method in enumerate(["initialize", "workspace.snapshot"], 1)
        )
        result = subprocess.run(
            [str(DEST / "python/bin/python3"), str(core / "libexec/t_bridge.py")],
            input=messages,
            text=True,
            capture_output=True,
            env=env,
            cwd=home,
            timeout=45,
            check=True,
        )
        replies = [json.loads(line) for line in result.stdout.splitlines()]
        if (
            replies[1].get("result", {}).get("sessions")
            or replies[1].get("result", {}).get("repositories")
            or replies[1].get("result", {}).get("hosts")
        ):
            raise SystemExit("Bundle smoke isolation failed")
        if (
            len(replies) != 2
            or any("error" in reply for reply in replies)
            or replies[0]["result"]["protocolVersion"] != 1
        ):
            raise SystemExit("Bundled helper smoke failed: " + result.stdout)
    with tempfile.TemporaryDirectory(prefix="t-desktop-shell-") as home:
        Path(home + "/tmux").mkdir(mode=0o700)
        Path(home + "/local.zsh").write_text("")
        probes = Path(home) / "process-probes"
        probes.mkdir()
        for name in ("ps", "pgrep", "lsof"):
            probe = probes / name
            probe.write_text("#!/bin/sh\nexit 0\n")
            probe.chmod(0o755)
        # Login path_helper prepends system tools; re-pin TEST ONLY process probes.
        Path(home + "/.zshrc").write_text('export PATH="$HOME/process-probes:$PATH"\n')

        env = {
            "HOME": home,
            "ZDOTDIR": home,
            "T_LOCAL_RC": home + "/local.zsh",
            "TMUX_TMPDIR": home + "/tmux",
            "XDG_CACHE_HOME": home + "/cache",
            "XDG_CONFIG_HOME": home + "/config",
            "XDG_STATE_HOME": home + "/state",
            "PATH": str(probes) + ":/usr/bin:/bin:/usr/sbin:/sbin",
            "T_NO_UPDATE_CHECK": "1",
        }
        script = (
            "import sys;sys.path.insert(0,sys.argv[1]);from t_core import Core;import subprocess;c=Core();p=subprocess.run(c.shell('whence -p ps'),capture_output=True,text=True);assert p.stdout.strip().endswith('/process-probes/ps');r=subprocess.run(c.shell('t --version'),text=True,capture_output=True);print(r.stdout);assert r.returncode == 0;assert 'v"
            + desktop_version
            + "' in r.stdout"
        )
        subprocess.run(
            [str(DEST / "python/bin/python3"), "-c", script, str(core / "libexec")],
            env=env,
            cwd=home,
            timeout=45,
            check=True,
            capture_output=True,
        )
    print(
        f"Staged standalone Python {VERSION} and shared core; isolated bundle smoke passed."
    )


if __name__ == "__main__":
    main()
