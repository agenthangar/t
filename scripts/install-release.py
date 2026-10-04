#!/usr/bin/env python3
"""Install a verified, checkout-free t release from GitHub.

Public entry point: curl -fsSL .../scripts/install-release.py | python3
Only the Python standard library is required for download and extraction.
"""

import argparse
import fcntl
import hashlib
import io
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from urllib.request import urlopen

RELEASES = "https://github.com/agenthangar/t/releases"
VERSION = re.compile(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\Z")
SHA256 = re.compile(r"([0-9a-fA-F]{64})[ \t]+\*?t\.tar\.gz\Z")
OWNED_LINKS = (
    ("bin/t", "bin/t"),
    ("bin/claude-stamp-tmux", "bin/claude-stamp-tmux"),
    ("bin/cursor-beam", "bin/cursor-beam"),
    ("claude/commands/tpush.md", ".claude/commands/tpush.md"),
    ("claude/commands/tpop.md", ".claude/commands/tpop.md"),
    ("codex/prompts/tpush.md", ".codex/prompts/tpush.md"),
    ("codex/prompts/tpop.md", ".codex/prompts/tpop.md"),
)
REQUIRED = (
    ".t-install-version", ".t-release-version", "install.sh", "t.plugin.zsh",
    "local.zsh.example", "bin/t", "bin/claude-stamp-tmux", "bin/cursor-beam",
    "LICENSE", "README.md", "ui/fzf.sh", "libexec/t_app_window.py",
    "libexec/t_desktop.py", "libexec/t_markdown.py", "libexec/t_recovery.py", "libexec/vendor/mistune/LICENSE",
    "zsh/config.zsh", "zsh/agent.zsh", "zsh/worktree.zsh",
    "zsh/sessions.zsh", "zsh/remote.zsh", "zsh/resume.zsh", "zsh/shim.zsh",
    "claude/settings.json.example", "claude/commands/tpush.md",
    "claude/commands/tpop.md", "codex/prompts/tpush.md", "codex/prompts/tpop.md",
    "scripts/install-release.py",
)
MAX_ARCHIVE = 64 * 1024 * 1024
MAX_MEMBER = 32 * 1024 * 1024


def download(url):
    with urlopen(url, timeout=30) as response:
        data = response.read(MAX_ARCHIVE + 1)
    if len(data) > MAX_ARCHIVE:
        raise ValueError("release asset exceeds 64 MiB")
    return data


def expected_digest(checksums):
    matches = []
    for line in checksums.decode("ascii").splitlines():
        match = SHA256.fullmatch(line.strip())
        if match:
            matches.append(match.group(1).lower())
    if len(matches) != 1:
        raise ValueError("SHA256SUMS must contain exactly one t.tar.gz digest")
    return matches[0]


def validate_tree(root, expected_version=None):
    for name in REQUIRED:
        if not (root / name).is_file() or (root / name).is_symlink():
            raise ValueError("release is missing a regular file: " + name)
    if (root / ".t-install-version").read_text().strip() != "1":
        raise ValueError("unsupported t installer capability")
    version = (root / ".t-release-version").read_text().strip()
    if not VERSION.fullmatch(version) or (expected_version and version != expected_version):
        raise ValueError("invalid or unexpected t release version")
    for name in ("install.sh", "bin/t", "bin/claude-stamp-tmux",
                 "bin/cursor-beam", "scripts/install-release.py"):
        if not os.access(root / name, os.X_OK):
            raise ValueError("release executable is not executable: " + name)
    return version


def extract_verified(archive, destination, expected_version=None):
    """Extract only regular files and directories beneath the single t/ prefix."""
    seen = set()
    total = 0
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        members = tar.getmembers()
        if len(members) > 2000:
            raise ValueError("release archive has too many members")
        for member in members:
            name = member.name
            parts = PurePosixPath(name).parts
            if (not parts or parts[0] != "t" or name.startswith("/")
                    or any(part in (".", "..") for part in name.split("/"))
                    or "\\" in name or name in seen):
                raise ValueError("unsafe release archive path: " + name)
            seen.add(name)
            if not (member.isfile() or member.isdir()):
                raise ValueError("release archive contains a link or special file: " + name)
            if member.size < 0 or member.size > MAX_MEMBER:
                raise ValueError("oversized release member: " + name)
            total += member.size
            if total > MAX_ARCHIVE:
                raise ValueError("release contents exceed 64 MiB")
            relative = Path(*parts[1:]) if len(parts) > 1 else Path()
            target = destination / relative
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = tar.extractfile(member)
            if source is None:
                raise ValueError("cannot read release member: " + name)
            with target.open("xb") as output:
                shutil.copyfileobj(source, output)
            target.chmod(0o755 if member.mode & 0o111 else 0o644)
    return validate_tree(destination, expected_version)


def install_base(explicit=None):
    if explicit:
        return Path(explicit).expanduser().resolve()
    if os.environ.get("T_INSTALL_DIR"):
        return Path(os.environ["T_INSTALL_DIR"]).expanduser().resolve()
    # A bundled updater keeps a custom installation base across upgrades.
    current = Path(__file__).resolve().parent.parent
    if current.parent.name == "releases" and VERSION.fullmatch(current.name):
        try:
            validate_tree(current, current.name)
        except ValueError:
            pass
        else:
            return current.parent.parent
    xdg = os.environ.get("XDG_DATA_HOME")
    return (Path(xdg).expanduser() if xdg else Path.home() / ".local/share") / "t"


def backup_name(path):
    candidate = Path(str(path) + ".bak")
    index = 1
    while candidate.exists() or candidate.is_symlink():
        candidate = Path(str(path) + ".bak." + str(index))
        index += 1
    return candidate


def snapshot_links(home):
    states = []
    for _, relative in OWNED_LINKS:
        path = home / relative
        if path.is_symlink():
            states.append((path, "symlink", os.readlink(path), None))
        elif path.exists():
            states.append((path, "file", None, backup_name(path)))
        else:
            states.append((path, "missing", None, None))
    return states


def rollback_links(states):
    for path, kind, target, backup in reversed(states):
        if kind == "symlink":
            if path.is_symlink() and os.readlink(path) == target:
                continue
            if path.is_symlink() or path.is_file():
                path.unlink()
            os.symlink(target, path)
        elif kind == "file":
            if backup.exists() or backup.is_symlink():
                if path.is_symlink() or path.is_file():
                    path.unlink()
                shutil.move(str(backup), str(path))
        elif path.is_symlink():
            path.unlink()


def in_use(root, home):
    physical_root = root.resolve()
    for _, relative in OWNED_LINKS:
        link = home / relative
        if link.is_symlink() and Path(os.path.realpath(link)).is_relative_to(physical_root):
            return True
    return False


def trees_match(left, right):
    for source in right.rglob("*"):
        relative = source.relative_to(right)
        target = left / relative
        if source.is_file():
            if target.is_symlink() or not target.is_file():
                return False
            if hashlib.sha256(source.read_bytes()).digest() != hashlib.sha256(target.read_bytes()).digest():
                return False
        elif not target.is_dir() or target.is_symlink():
            return False
    return True


def _install_locked(archive, checksums, base, home, requested_version=None):
    actual = hashlib.sha256(archive).hexdigest()
    if actual != expected_digest(checksums):
        raise ValueError("t.tar.gz SHA256 mismatch; installation unchanged")
    releases = base / "releases"
    releases.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix=".t-release-", dir=releases))
    published = False
    root = None
    quarantined = None
    states = snapshot_links(home)
    try:
        version = extract_verified(archive, staging, requested_version)
        root = releases / version
        if root.exists():
            try:
                validate_tree(root, version)
                # An already-installed version may be in use. Preserve its bytes.
                if not trees_match(root, staging):
                    raise ValueError("existing release files differ from the downloaded asset")
            except ValueError:
                if in_use(root, home):
                    raise ValueError("existing release is damaged but still linked; refusing to replace it; "
                                     "restore its files or install into a separate --install-dir")
                quarantined = Path(tempfile.mkdtemp(prefix=".corrupt-" + version + "-", dir=releases))
                quarantined.rmdir()
                root.rename(quarantined)
                staging.rename(root)
                published = True
            else:
                shutil.rmtree(staging)
                staging = None
        else:
            staging.rename(root)
            published = True
        env = os.environ.copy()
        env["HOME"] = str(home)
        # Updates preserve a user's existing setup; fresh installs seed it.
        if (home / "bin/t").is_symlink():
            env["T_LINKS_ONLY"] = "1"
        else:
            env.pop("T_LINKS_ONLY", None)
        env.pop("T_LINK_DEV", None)
        subprocess.run([str(root / "install.sh")], env=env, cwd=str(root), check=True)
        print("Installed t " + version + " at " + str(root))
        return root
    except Exception:
        rollback_links(states)
        if published and root is not None and not in_use(root, home):
            shutil.rmtree(root)
        if quarantined and quarantined.exists() and root is not None and not root.exists():
            quarantined.rename(root)
        raise
    finally:
        if staging and staging.exists():
            shutil.rmtree(staging)


def install(archive, checksums, base, home, requested_version=None):
    base = Path(base).expanduser().resolve()
    base.mkdir(parents=True, exist_ok=True)
    # The lock covers link snapshots and rollback as well as archive promotion.
    with (base / ".install.lock").open("a+b") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _install_locked(archive, checksums, base, Path(home), requested_version)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Install a verified t release without Git checkout")
    parser.add_argument("--version", help="exact release tag, for example v1.2.3")
    parser.add_argument("--install-dir", help="base directory containing releases/")
    args = parser.parse_args(argv)
    if args.version and not VERSION.fullmatch(args.version):
        parser.error("--version must be vX.Y.Z")
    path = ("download/" if not args.version else "download/" + args.version + "/")
    prefix = RELEASES + "/latest/" + path if not args.version else RELEASES + "/" + path
    try:
        archive = download(prefix + "t.tar.gz")
        checksums = download(prefix + "SHA256SUMS")
        install(archive, checksums, install_base(args.install_dir), Path.home(), args.version)
    except (OSError, ValueError, tarfile.TarError, subprocess.CalledProcessError) as error:
        print("t release install: " + str(error), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
