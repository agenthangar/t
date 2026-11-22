#!/usr/bin/env python3
"""Build a worktree-scoped rnnr menu-bar app for macOS."""

from pathlib import Path
import plistlib
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

    run("swift", "build", "-c", "release", "--product", "RnnrApp")
    binary_dir = Path(subprocess.check_output(
        ["swift", "build", "-c", "release", "--show-bin-path"], cwd=APP, text=True,
    ).strip())

    with tempfile.TemporaryDirectory(dir=BUILD) as folder:
        bundle = Path(folder) / "rnnr.app"
        contents = bundle / "Contents"
        (contents / "MacOS").mkdir(parents=True)
        (contents / "Resources").mkdir()
        shutil.copy2(binary_dir / "RnnrApp", contents / "MacOS" / "RnnrApp")
        shutil.copy2(APP / "Resources" / "fox-31.png", contents / "Resources" / "fox-31.png")
        shutil.copy2(APP / "terminal.zsh", contents / "Resources" / "terminal.zsh")

        with (contents / "Info.plist").open("wb") as info:
            plistlib.dump({
                "CFBundleDevelopmentRegion": "en",
                "CFBundleDisplayName": "rnnr",
                "CFBundleExecutable": "RnnrApp",
                "CFBundleIdentifier": BUNDLE_ID,
                "CFBundleInfoDictionaryVersion": "6.0",
                "CFBundleName": "rnnr",
                "CFBundlePackageType": "APPL",
                "CFBundleShortVersionString": "0.1",
                "CFBundleVersion": "1",
                "LSMinimumSystemVersion": "13.0",
                "LSUIElement": True,
                "NSAppleEventsUsageDescription": "rnnr opens a new Terminal window for session actions.",
                "RnnrSourceRoot": str(ROOT),
            }, info)
        run("codesign", "--force", "--sign", "-", str(bundle))

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
