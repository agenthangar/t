"""Check the standalone package without launching UI or using local sessions."""
from pathlib import Path
import json
import plistlib
import subprocess
import tempfile


def verify(app):
    root = app / "Contents/Resources/backend"
    with (app / "Contents/Info.plist").open("rb") as stream:
        info = plistlib.load(stream)
    assert info["CFBundleIdentifier"] == "com.agenthangar.rnnr"
    assert "RnnrSourceRoot" not in info
    for executable in (app / "Contents/MacOS/RnnrApp", root / "bin/tmux", root / "bin/fzf"):
        assert set(subprocess.check_output(["/usr/bin/lipo", "-archs", executable], text=True).split()) == {"arm64", "x86_64"}
        versions = subprocess.check_output(["xcrun", "vtool", "-show-build", executable], text=True)
        minimums = [line.split()[1] for line in versions.splitlines() if line.strip().startswith("minos ")]
        assert len(minimums) == 2 and all(tuple(map(int, text.split("."))) <= (13, 0, 0) for text in minimums)
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        env = {"HOME": tmp, "TMPDIR": tmp, "TMUX_TMPDIR": tmp, "PATH": "/usr/bin:/bin",
               "XDG_CONFIG_HOME": str(home / ".config"), "XDG_STATE_HOME": str(home / ".state"),
               "XDG_CACHE_HOME": str(home / ".cache"), "GIT_CONFIG_NOSYSTEM": "1", "PYTHONDONTWRITEBYTECODE": "1"}
        rn = home / "rn"
        rn.symlink_to(root / "bin/rn")
        def check(*argv):
            return subprocess.check_output(argv, env=env, cwd=tmp, stdin=subprocess.DEVNULL, timeout=30, text=True)
        check(root / "bin/tmux", "-V")
        check(root / "bin/fzf", "--version")
        check(root / "bin/python3", "-I", "-c", "import ssl")
        assert "list_sessions" in check(rn, "mcp", "--list-tools")
        result = json.loads(check(rn, "mcp", "--call", "list_sessions", "{}"))
        assert isinstance(result["sessions"], list)
    subprocess.run(["codesign", "--verify", "--strict", "--deep", "--all-architectures", app], check=True)


if __name__ == "__main__":
    import sys
    verify(Path(sys.argv[1]).resolve())
