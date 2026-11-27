#!/usr/bin/env python3
"""Build the standalone universal rnnr application."""

from pathlib import Path
import plistlib
import os
import re
from backend_bundle import add_backend
import shutil
import subprocess
import sys
import tempfile


APP = Path(__file__).resolve().parent
ROOT = APP.parents[1]
BUILD = APP / ".build"
BUNDLE_ID = "com.agenthangar.rnnr"


def run(*arguments):
    return subprocess.run(arguments, cwd=APP, check=True)


def build():
    if sys.platform != "darwin":
        raise SystemExit("rnnr requires macOS 13 or later and the Swift toolchain.")

    arches = ["--arch", "arm64", "--arch", "x86_64"]
    version = subprocess.check_output(["git", "describe", "--tags", "--abbrev=0", "--match", "v[0-9]*"], cwd=ROOT, text=True).strip().removeprefix("v")
    version = os.environ.get("RNNR_VERSION", version)
    if not re.fullmatch(r"0\.[0-9]+\.[0-9]+", version):
        raise ValueError("Expected a pre-1.0 patch version")
    run("swift", "build", *arches, "-c", "release", "--product", "RnnrApp")
    binary_dir = Path(subprocess.check_output(
        ["swift", "build", *arches, "-c", "release", "--show-bin-path"], cwd=APP, text=True,
    ).strip())

    with tempfile.TemporaryDirectory(dir=BUILD) as folder:
        bundle = Path(folder) / "rnnr.app"
        contents = bundle / "Contents"
        (contents / "MacOS").mkdir(parents=True)
        (contents / "Resources").mkdir()
        shutil.copy2(binary_dir / "RnnrApp", contents / "MacOS" / "RnnrApp")
        shutil.copy2(APP / "Resources" / "fox-31.png", contents / "Resources" / "fox-31.png")
        shutil.copy2(APP / "terminal.zsh", contents / "Resources" / "terminal.zsh")

        add_backend(contents / "Resources", ROOT, version)
        shutil.copy2(APP / "integration.py", contents / "Resources/backend/bin/integration.py")
        with (contents / "Info.plist").open("wb") as info:
            plistlib.dump({
                "CFBundleDevelopmentRegion": "en",
                "CFBundleDisplayName": "rnnr",
                "CFBundleExecutable": "RnnrApp",
                "CFBundleIdentifier": BUNDLE_ID,
                "CFBundleInfoDictionaryVersion": "6.0",
                "CFBundleName": "rnnr",
                "CFBundlePackageType": "APPL",
                "CFBundleShortVersionString": version,
                "CFBundleVersion": "1",
                "LSMinimumSystemVersion": "13.0",
                "LSUIElement": True,
                "NSAppleEventsUsageDescription": "rnnr opens a new Terminal window for session actions.",

            }, info)
        identity = os.environ.get("RNNR_SIGN_IDENTITY", "-")
        options = ["--options", "runtime", "--timestamp"] if identity != "-" else []
        if options:
            for file in (contents / "Resources/backend").rglob("*"):
                if file.is_symlink() or not file.is_file():
                    continue
                with file.open("rb") as source:
                    if source.read(4).hex() in {"cffaedfe", "feedfacf", "cafebabe", "bebafeca"}:
                        run("codesign", "--force", *options, "--sign", identity, str(file))
        run("codesign", "--force", *options, "--entitlements", str(APP / "apple-events.plist"), "--sign", identity, str(bundle))
        run("codesign", "--verify", "--strict", "--deep", str(bundle))

        target = BUILD / bundle.name
        if target.is_symlink():
            raise SystemExit(f"Refusing to replace a symlink at {target}")
        if target.exists():
            previous_info = target / "Contents" / "Info.plist"
            if not previous_info.is_file():
                raise SystemExit(f"Expected an rnnr bundle at {target}")
            with previous_info.open("rb") as previous:
                if plistlib.load(previous).get("CFBundleIdentifier") != BUNDLE_ID:
                    raise SystemExit(f"Expected an rnnr bundle at {target}")
            shutil.rmtree(target)
        bundle.rename(target)

    print(target)


if __name__ == "__main__":
    build()
