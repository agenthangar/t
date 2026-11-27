"""Read-only inventory and the optional rnnr Homebrew installation."""
import json
from pathlib import Path
import plistlib
import shutil
import subprocess
import sys

APP = Path("/Applications/rnnr.app")
CASK = "agenthangar/tap/rnnr"


def status():
    try:
        with (APP / "Contents/Info.plist").open("rb") as source:
            info = plistlib.load(source)
        if info.get("CFBundleIdentifier") != "com.agenthangar.rnnr":
            raise ValueError("Unrecognized app at /Applications/rnnr.app")
        return {"installed": True, "version": info["CFBundleShortVersionString"]}
    except FileNotFoundError:
        if APP.exists() or APP.is_symlink():
            raise ValueError("Incomplete installation at /Applications/rnnr.app")
        return {"installed": False, "version": None}


def install():
    if sys.platform != "darwin":
        raise ValueError("rnnr requires macOS")
    brew = shutil.which("brew")
    if not brew:
        raise ValueError("Install Homebrew or download the signed rnnr app.")
    args = [brew, "install", "--cask", CASK]
    result = subprocess.run(args, check=False)
    if result.returncode:
        return result.returncode
    if not status()["installed"]:
        raise ValueError("Homebrew finished but the rnnr app could not be verified in Applications.")
    return 0


def command(args, ui_factory):
    try:
        if args.action == "status":
            value = status()
            print(json.dumps(value) if args.json else "rnnr: " + (value["version"] or "not installed"))
            return 0
        label = "brew install --cask " + CASK
        if args.dry_run or not (args.yes or (sys.stdin.isatty() and sys.stdout.isatty())):
            print(label)
            return 0
        if not args.yes:
            ui = ui_factory()
            ui.raw()
            try:
                if ui.pick("rnnr", [("install", label), ("cancel", "Cancel")]) != "install":
                    return 130
            finally:
                ui.restore()
        return install()
    except (OSError, ValueError, KeyError, plistlib.InvalidFileException) as error:
        print(str(error), file=sys.stderr)
        return 1
