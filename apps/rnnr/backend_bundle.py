"""Assemble the application backend using the same file inventory as CLI releases."""
import hashlib
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import tarfile
import tempfile
import urllib.request


def extract_locked(item, folder):
    archive = folder / "download.tar.gz"
    with urllib.request.urlopen(item["url"], timeout=90) as source, archive.open("wb") as out:
        shutil.copyfileobj(source, out)
    with archive.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != item["sha256"]:
            raise ValueError("Bundled dependency digest mismatch")
    with tarfile.open(archive) as source:
        source.extractall(folder, filter="data")
    archive.unlink()


def build_tmux(packages, workspace, binaries, licenses):
    # Upstream macOS binaries require macOS 15. Build the two macOS 13 slices
    # against a private static libevent and the SDK ncurses.
    for item in packages.values():
        extract_locked(item, workspace)
    event, = workspace.glob("libevent-*")
    tmux, = workspace.glob("tmux-*")
    for source in (event, tmux):
        shutil.copy2(source / ("LICENSE" if source == event else "COPYING"), licenses / (source.name + "-COPYING"))
    slices = []
    for arch in ("arm64", "x86_64"):
        prefix = workspace / arch
        env = dict(os.environ, PATH="/usr/bin:/bin:/usr/sbin:/sbin", MACOSX_DEPLOYMENT_TARGET="13.0",
                   CC="/usr/bin/clang", CFLAGS=f"-O2 -arch {arch}", LDFLAGS=f"-arch {arch}")
        host = "aarch64" if arch == "arm64" else arch
        for source in (event, tmux):
            build = prefix / source.name
            build.mkdir(parents=True)
            options = ["--disable-shared", "--disable-openssl", "--disable-samples", "--disable-libevent-regress"] if source == event else ["--disable-utf8proc", "--disable-jemalloc"]
            if source == tmux:
                env.update(LIBEVENT_CFLAGS=f"-I{prefix}/include", LIBEVENT_LIBS=str(prefix / "lib/libevent_core.a"),
                           LIBTINFO_CFLAGS=" ", LIBTINFO_LIBS="-lncurses", LIBNCURSES_CFLAGS=" ", LIBNCURSES_LIBS="-lncurses")
            subprocess.run([source / "configure", f"--host={host}-apple-darwin", f"--prefix={prefix}", *options],
                           env=env, cwd=build, check=True)
            subprocess.run(["make", "-j4"], env=env, cwd=build, check=True)
            if source == event:
                subprocess.run(["make", "install"], env=env, cwd=build, check=True)
            else:
                slices.append(build / "tmux")
    subprocess.run(["/usr/bin/lipo", "-create", *slices, "-output", binaries / "tmux"], check=True)


def add_backend(resources, root, version):
    archive_builder = runpy.run_path(str(root / "scripts/build-release.py"))
    backend = resources / "backend"
    with tempfile.TemporaryDirectory() as folder:
        archive = archive_builder["build"]("v" + version, root, folder)
        with tarfile.open(archive) as source:
            source.extractall(folder, filter="data")
        shutil.move(str(Path(folder) / "t"), backend)
    lock = json.loads(Path(__file__).with_name("runtime-lock.json").read_text())
    for arch, item in lock["architectures"].items():
        with tempfile.TemporaryDirectory() as folder:
            extract_locked(item, Path(folder))
            shutil.move(str(Path(folder) / "python"), backend / arch)
    binaries = backend / "bin"
    tools = json.loads(Path(__file__).with_name("tool-lock.json").read_text())
    licenses = resources / "Licenses"
    licenses.mkdir()
    shutil.copy2(root / "apps/rnnr/Resources/fzf-LICENSE", licenses)
    for tool, packages in tools.items():
        slices = []
        with tempfile.TemporaryDirectory() as workspace:
            if tool == "tmux":
                build_tmux(packages, Path(workspace), binaries, licenses)
                continue
            for arch, item in packages.items():
                folder = Path(workspace) / arch
                folder.mkdir()
                extract_locked(item, folder)
                binary, = (path for path in folder.rglob(tool) if path.is_file())
                slices.append(binary)
            subprocess.run(["/usr/bin/lipo", "-create", *slices, "-output", binaries / tool], check=True)
            (binaries / tool).chmod(0o755)
    python = binaries / "python3"
    python.write_text('#!/bin/sh\nset -eu\nexport PYTHONDONTWRITEBYTECODE=1\nroot=$(CDPATH="" cd -- "$(dirname -- "$0")/.." && pwd)\nexec "$root/$(/usr/bin/uname -m)/bin/python3" "$@"\n')
    python.chmod(0o755)
    # An absolute MCP registration can be launched with an empty PATH.
    entry = binaries / "t"
    entry.rename(binaries / "t_cli.py")
    entry.write_text("""#!/bin/sh
set -eu
entry=$0
while [ -L "$entry" ]; do
    target=$(/usr/bin/readlink "$entry")
    case "$target" in
        /*) entry=$target ;;
        *) entry=$(dirname -- "$entry")/$target ;;
    esac
done
bin=$(CDPATH="" cd -- "$(dirname -- "$entry")" && pwd)
export PATH="$bin:${PATH:-/usr/bin:/bin}"
export PYTHONDONTWRITEBYTECODE=1
exec "$bin/python3" "$bin/t_cli.py" "$@"
""")
    entry.chmod(0o755)
    (binaries / "rn").symlink_to("t")
