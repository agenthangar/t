#!/usr/bin/env python3
"""Launch a disposable Linux account for exercising t's first-run workflow."""

import argparse
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parent.parent
HELPERS = (
    "Dockerfile", "entrypoint.sh", "playground-install",
    "playground-demo-agent", "fake-claude", "smoke.py",
)


def stage_context(repo, destination):
    """Copy tracked runtime files, including edits, without host Git or secrets."""
    manifest = runpy.run_path(str(repo / "scripts/build-release.py"))
    tracked = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=repo,
    ).decode().split("\0")
    selected = {
        name for name in tracked if name in manifest["EXACT"]
        or name.startswith(manifest["PREFIXES"]) or name == ".githooks/pre-commit"
    }
    missing = manifest["EXACT"] - selected
    if missing:
        raise ValueError("missing tracked runtime files: " + ", ".join(sorted(missing)))
    files = [(name, "source/" + name) for name in sorted(selected)]
    files += [("scripts/playground/" + name, "playground/" + name) for name in HELPERS]
    for name, target in files:
        source = repo / name
        components = [repo.joinpath(*Path(name).parts[:i])
                      for i in range(1, len(Path(name).parts) + 1)]
        if (not source.is_file() or any(part.is_symlink() for part in components)
                or not source.resolve().is_relative_to(repo.resolve())):
            raise ValueError("playground requires a regular file inside the checkout: " + name)
        output = destination / target
        output.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, output)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", nargs="?", default="fresh",
                        choices=("fresh", "installed", "smoke", "build"),
                        help="fresh shell (default), installed shell, smoke test, or image only")
    parser.add_argument("--image", default="t-playground:local", help="local Docker image tag")
    args = parser.parse_args()
    if not args.image or args.image.startswith("-"):
        parser.error("image must be a Docker image tag, not an option")
    if not shutil.which("docker"):
        parser.error("Docker is required; start Docker Desktop or a Docker Engine first")
    if args.mode in ("fresh", "installed") and not sys.stdin.isatty():
        parser.error("interactive modes need a terminal; use smoke for an automated check")
    try:
        subprocess.run(["docker", "info"], check=True, stdout=subprocess.DEVNULL)
        with tempfile.TemporaryDirectory(prefix="t-playground-") as temporary:
            context = Path(temporary)
            stage_context(ROOT, context)
            subprocess.run([
                "docker", "build", "--tag", args.image,
                "--file", str(context / "playground/Dockerfile"), str(context),
            ], check=True)
        if args.mode == "build":
            return 0
        command = ["docker", "run", "--rm", "--init", "--hostname", "t-playground"]
        if args.mode != "smoke":
            command.append("-it")
        # No host mounts, forwarded credentials, ports, or host tmux socket.
        return subprocess.run([*command, args.image, args.mode]).returncode
    except (OSError, ValueError, subprocess.CalledProcessError) as error:
        parser.exit(1, f"playground: {error}\n")


if __name__ == "__main__":
    raise SystemExit(main())
