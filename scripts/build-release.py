#!/usr/bin/env python3
"""Build a deterministic, curated runtime archive from a committed Git tree."""

import argparse
import hashlib
import io
from pathlib import Path
import re
import subprocess
import tarfile

VERSION = re.compile(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
EXACT = {
    ".t-install-version", "install.sh", "t.plugin.zsh", "local.zsh.example",
    "LICENSE", "README.md",
    "bin/t", "bin/claude-stamp-tmux", "bin/cursor-beam",
    "claude/settings.json.example", "claude/commands/tpush.md",
    "claude/commands/tpop.md", "codex/prompts/tpush.md",
    "codex/prompts/tpop.md", "scripts/install-release.py", "ui/fzf.sh",
}
PREFIXES = ("libexec/", "zsh/")


def git(*args, cwd):
    return subprocess.check_output(["git", *args], cwd=cwd)


def build(version, repo, output):
    if not VERSION.fullmatch(version):
        raise ValueError("release version must be vX.Y.Z")
    root = Path(repo).resolve()
    entries = []
    for row in git("ls-tree", "-r", "-z", "HEAD", cwd=root).split(b"\0"):
        if not row:
            continue
        header, name = row.split(b"\t", 1)
        mode, kind, _ = header.split()
        path = name.decode("utf-8")
        if path not in EXACT and not path.startswith(PREFIXES):
            continue
        if "__pycache__" in Path(path).parts or path.endswith((".pyc", ".pyo")):
            continue
        if kind != b"blob" or mode not in (b"100644", b"100755"):
            raise ValueError("runtime tree includes a non-file: " + path)
        entries.append((path, mode == b"100755"))
    actual = {name for name, _ in entries}
    missing = EXACT - actual
    if missing:
        raise ValueError("runtime tree misses " + ", ".join(sorted(missing)))
    if git("show", "HEAD:.t-install-version", cwd=root).strip() != b"1":
        raise ValueError("unsupported install capability")
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    archive = output / "t.tar.gz"
    # gzip timestamp and tar metadata are normalized for repeatable release bytes.
    import gzip
    with archive.open("wb") as file, gzip.GzipFile(fileobj=file, mode="wb", filename="", mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            def add(name, contents, mode):
                info = tarfile.TarInfo("t/" + name)
                info.size = len(contents)
                info.mode = mode
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                tar.addfile(info, io.BytesIO(contents))
            for name, executable in sorted(entries):
                add(name, git("show", "HEAD:" + name, cwd=root), 0o755 if executable else 0o644)
            add(".t-release-version", (version + "\n").encode(), 0o644)
    digest = hashlib.sha256(archive.read_bytes()).hexdigest()
    (output / "SHA256SUMS").write_text(digest + "  t.tar.gz\n")
    return archive


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("version")
    parser.add_argument("--repo", default=Path(__file__).resolve().parent.parent)
    parser.add_argument("--output", default="dist")
    args = parser.parse_args()
    archive = build(args.version, args.repo, args.output)
    print(archive)


if __name__ == "__main__":
    main()
