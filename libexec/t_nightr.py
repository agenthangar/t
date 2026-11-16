"""Optional Nightr package integration; never starts protection or authorizes helpers."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import plistlib
import re
import runpy
import shutil
import stat
import subprocess
import sys
import tempfile

CASK = "agenthangar/tap/nightr"
APP = Path("/Applications/Nightr.app")
CLI = APP / "Contents/MacOS/nightr-native"


def state_path():
    return Path(os.environ.get("XDG_STATE_HOME", str(Path.home() / ".local/state"))) / "t/nightr.json"


def integration():
    try:
        value = json.loads(state_path().read_text())
    except FileNotFoundError:
        return None
    if not isinstance(value, dict) or value.get("schemaVersion") != 1 or value.get("owner") not in ("homebrew", "direct"):
        raise ValueError("invalid Nightr integration record; inspect " + str(state_path()))
    return value


def record(owner):
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("refusing a symlinked Nightr integration record")
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".nightr-", delete=False) as stream:
        temporary = Path(stream.name)
        os.fchmod(stream.fileno(), 0o600)
        stream.write(json.dumps({"schemaVersion": 1, "owner": owner, "managed": True}).encode())
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def installed():
    try:
        with (APP / "Contents/Info.plist").open("rb") as stream:
            info = plistlib.load(stream)
        if info.get("CFBundleIdentifier") != "ai.nightr.app" or not CLI.is_file():
            return None
        return str(info["CFBundleShortVersionString"])
    except (OSError, ValueError, KeyError, plistlib.InvalidFileException):
        return None


def package(command):
    helper = Path(__file__).with_name("nightr_install.py")
    return runpy.run_path(str(helper))["main"]([command])


def package_owner():
    helper = Path(__file__).with_name("nightr_install.py")
    saved = runpy.run_path(str(helper))["read_record"]()
    if not saved or saved.get("owner") not in ("direct", "homebrew"):
        raise ValueError("verified package ownership is missing; run setup repair before opting into updates")
    return saved["owner"]


def brew_owned(brew):
    if not brew:
        return False
    result = subprocess.run([brew, "list", "--cask", CASK], capture_output=True, timeout=30)
    return result.returncode == 0


def assert_migration_safe():
    """Never stop or reset an unowned legacy monitor during package migration."""
    power = subprocess.run(["/usr/bin/pmset", "-g"], capture_output=True, text=True, timeout=5)
    if power.returncode:
        raise ValueError("could not check existing sleep protection; installation was not started")
    if not re.search(r"(?m)^\s*SleepDisabled\s+1\s*$", power.stdout):
        return
    if installed():
        result = subprocess.run([str(CLI), "status", "--json"], capture_output=True, text=True, timeout=5)
        try:
            response = json.loads(result.stdout)
            owned = (result.returncode == 0 and response.get("version") == 1
                     and not response.get("error") and response.get("status", {}).get("holding") is True)
        except (ValueError, AttributeError):
            owned = False
        if owned:
            return
    raise ValueError("another sleep utility is active. Stop the existing nosleep monitor with Ctrl-C, then retry; its power settings were preserved")


def status():
    saved = integration()
    version = installed() if sys.platform == "darwin" else None
    return {"name": "nightr", "supported": sys.platform == "darwin", "installed": bool(version),
            "version": version, "managed": bool(saved and saved.get("managed")),
            "owner": saved["owner"] if saved else None}


def update_managed(*, check=False):
    saved = integration()
    if sys.platform != "darwin" or not saved or not saved.get("managed"):
        return 0
    if not installed():
        print("Nightr: not installed; skipped (t update never installs absent apps).")
        return 0
    return package("check" if check else "update")


def install():
    if sys.platform != "darwin":
        raise ValueError("Nightr requires macOS; nothing installed")
    print("Nightr: checking existing protection…", file=sys.stderr)
    assert_migration_safe()
    saved = integration()
    if installed():
        if saved:
            print("Nightr is installed and managed by t. Run nightr setup to repair setup.")
            return 0
        brew = shutil.which("brew")
        owner = "homebrew" if brew_owned(brew) else "direct"
        # The package installer verifies existing direct provenance before adoption.
        if owner == "direct":
            result = package("adopt-direct")
            if result:
                return result
        if owner == "homebrew":
            result = package("adopt-homebrew")
            if result:
                return result
        record(owner)
        print("Nightr updates are now included in t system update.")
        return 0
    if APP.exists():
        raise ValueError("an unrecognized Nightr.app exists; it will not be overwritten")
    # The shared installer audits the cask before choosing Homebrew and owns
    # transaction guards, signatures, receipts and rollback for direct installs.
    result = package("install")
    if result:
        return result
    if not installed():
        raise ValueError("installation did not produce the expected Nightr app")
    record(package_owner())
    print("Nightr installed. Run nightr setup for helper and login setup.")
    return 0


def command(args, ui_factory=None):
    try:
        if args.action == "status":
            value = status()
            if args.json:
                print(json.dumps(value))
            else:
                print("Nightr: " + (value["version"] or "not installed") +
                      (" · updates managed by t" if value["managed"] else ""))
            return 0
        if sys.platform != "darwin":
            raise ValueError("Nightr requires macOS; nothing installed")
        label = "Install Nightr and include its updates in t system update"
        if installed():
            label = "Include the existing Nightr installation in t system update"
        if args.dry_run or not (args.yes or (sys.stdin.isatty() and sys.stdout.isatty())):
            print(label)
            print("Homebrew owns Homebrew installs; direct installs use verified Nightr releases.")
            print("Helper authorization stays in nightr setup.")
            return 0
        if not args.yes:
            ui = ui_factory()
            ui.raw()
            try:
                choice = ui.pick("Nightr", [("install", label), ("cancel", "Cancel")])
            finally:
                ui.restore()
            if choice != "install":
                return 130
        path = state_path().parent
        path.mkdir(parents=True, exist_ok=True)
        fd = os.open(path / ".nightr.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise ValueError("invalid Nightr integration lock")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise ValueError("another Nightr installation is in progress") from None
            return install()
        finally:
            os.close(fd)
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print(f"t system apps: {error}", file=sys.stderr)
        return 1


def add_parser(sub):
    parser = sub.add_parser("apps", help="Install and inspect optional desktop apps",
                            description="Optional apps on this Mac. Use install nightr to opt into managed updates; status nightr is read-only. No repository scope is used.")
    actions = parser.add_subparsers(dest="action", required=True)
    for action in ("install", "status"):
        child = actions.add_parser(action, help=f"{action.capitalize()} Nightr on this Mac")
        child.add_argument("app", choices=["nightr"])
        if action == "status":
            child.add_argument("--json", action="store_true", help="Print read-only JSON")
        else:
            child.add_argument("-y", "--yes", action="store_true", help="Install without the review prompt")
            child.add_argument("--dry-run", action="store_true", help="Show the plan without installing")
