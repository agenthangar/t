#!/usr/bin/env python3
"""Nightr's stdlib package owner/verified-release installer. No source builds.

Shared unchanged with t; acquisition authority is the repository's HTTPS GitHub
release API. The artifact additionally needs Apple Developer ID/notary approval.
This script never authorizes the power helper or changes enabled intent.
"""
from __future__ import annotations
# Vendored from AgentHangar/nightr launch implementation (MIT).
# Keep synchronized with its scripts/nightr_install.py; t owns only opt-in routing.
import argparse
import contextlib
import fcntl
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import uuid

REPOSITORY = "agenthangar/nightr"
API = f"https://api.github.com/repos/{REPOSITORY}/releases/latest"
APP = Path("/Applications/Nightr.app")
BUNDLE_ID = "ai.nightr.app"
MAX_ARTIFACT = 2 * 1024**3

class InstallError(RuntimeError):
    pass

class HTTPSOnly(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not newurl.startswith("https://"):
            raise InstallError("Release download redirected away from HTTPS")
        return super().redirect_request(req, fp, code, msg, headers, newurl)

def download(url, target, maximum):
    if not url.startswith("https://"):
        raise InstallError("Release downloads require HTTPS")
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "Nightr/1"})
    with urllib.request.build_opener(HTTPSOnly).open(request, timeout=30) as response, target.open("wb") as output:
        count = 0
        while True:
            block = response.read(1024 * 1024)
            if not block:
                break
            count += len(block)
            if count > maximum:
                raise InstallError("Release download exceeds its size limit")
            output.write(block)
    return count

def run(arguments, *, timeout=30):
    try:
        result = subprocess.run([str(x) for x in arguments], check=True, capture_output=True, text=True,
                                timeout=timeout, env={**os.environ, "LC_ALL": "C"})
    except (subprocess.SubprocessError, OSError) as error:
        raise InstallError(f"Command failed: {arguments[0]}: {error}") from error
    return result.stdout + result.stderr

def state_directory():
    return Path.home() / "Library/Application Support/Nightr"

def read_record():
    path = state_directory() / "installation.json"
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_uid != os.getuid():
        raise InstallError("Unsafe Nightr installation record")
    data = json.loads(path.read_text())
    if not isinstance(data, dict): raise InstallError("Invalid Nightr installation record")
    if data.get("schemaVersion") != 1 or data.get("owner") not in ("direct", "homebrew") or data.get("appPath") != str(APP):
        raise InstallError("Unrecognized Nightr installation owner; repair setup first")
    version_key(data.get("version"))
    if not re.fullmatch(r"[A-Z0-9]{10}", data.get("teamID", "")):
        raise InstallError("Invalid installation signing team")
    return data

def write_record(owner, version, team, artifact_sha256=None):
    directory = state_directory()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid():
        raise InstallError("Unsafe installation state directory")
    record = {"schemaVersion": 1, "owner": owner, "appPath": str(APP), "version": version, "teamID": team}
    if artifact_sha256 is not None: record["artifactSHA256"] = artifact_sha256
    fd, name = tempfile.mkstemp(prefix=".installation-", dir=directory)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(record, output, sort_keys=True); output.write("\n"); output.flush(); os.fsync(output.fileno())
        os.replace(name, directory / "installation.json")
    finally:
        with contextlib.suppress(FileNotFoundError): os.unlink(name)
    return record

@contextlib.contextmanager
def installation_lock():
    directory = state_directory()
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    if directory.is_symlink() or directory.stat().st_uid != os.getuid():
        raise InstallError("Unsafe installation state directory")
    fd = os.open(directory / "package.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_nlink != 1 or not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077:
            raise InstallError("Unsafe package lock")
        try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error: raise InstallError("Another Nightr installation or update is running") from error
        yield
    finally: os.close(fd)

def validate_manifest(data):
    if not isinstance(data, dict): raise InstallError("Release manifest must be an object")
    version = data.get("version", "")
    if not isinstance(version, str): raise InstallError("Invalid release version")
    if not re.fullmatch(r"0\.[0-9]+\.[0-9]+", version):
        raise InstallError("Invalid release version")
    name = f"Nightr-{version}-universal.dmg"
    asset = data.get("asset", {})
    url = f"https://github.com/{REPOSITORY}/releases/download/v{version}/{name}"
    if not isinstance(asset, dict): raise InstallError("Invalid release asset")
    if (data.get("schemaVersion") != 1 or data.get("bundleID") != BUNDLE_ID or
        data.get("controllerID") != "ai.nightr.controller" or data.get("helperID") != "ai.nightr.powerhelper" or
        data.get("protocolVersion") != 1 or data.get("platform") != "macos" or data.get("minVersion") != "13.0" or
        sorted(data.get("architectures", [])) != ["arm64", "x86_64"] or
        not re.fullmatch(r"[A-Z0-9]{10}", data.get("teamID", "")) or
        asset.get("name") != name or asset.get("url") != url or
        not re.fullmatch(r"[0-9a-f]{64}", asset.get("sha256", "")) or
        not isinstance(asset.get("size"), int) or not 0 < asset["size"] <= MAX_ARTIFACT):
        raise InstallError("Release manifest is incompatible or incomplete")
    return data

def latest_manifest(directory):
    release_file = directory / "release.json"
    download(API, release_file, 1024 * 1024)
    release = json.loads(release_file.read_text())
    asset = next((a for a in release.get("assets", []) if a.get("name") == "nightr-release.json"), None)
    if release.get("draft") or release.get("prerelease") or not asset:
        raise InstallError("A signed Nightr release is not available yet")
    expected = f"https://github.com/{REPOSITORY}/releases/download/{release.get('tag_name')}/nightr-release.json"
    if asset.get("browser_download_url") != expected:
        raise InstallError("Unexpected release manifest URL")
    path = directory / "manifest.json"
    download(expected, path, 1024 * 1024)
    data = validate_manifest(json.loads(path.read_text()))
    if release.get("tag_name") != "v" + data["version"]:
        raise InstallError("Release tag and manifest version differ")
    return data

def verify_app(path, team, version=None):
    if path.is_symlink() or not path.is_dir():
        raise InstallError("Expected canonical Nightr application is missing")
    with (path / "Contents/Info.plist").open("rb") as source:
        info = plistlib.load(source)
    if info.get("CFBundleIdentifier") != BUNDLE_ID or info.get("NightrTeamID") != team or (version and info.get("CFBundleShortVersionString") != version):
        raise InstallError("Application identity or version differs from release")
    requirement = f'anchor apple generic and identifier "{BUNDLE_ID}" and certificate leaf[subject.OU] = "{team}"'
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", "--all-architectures", "-R", requirement, path])
    run(["/usr/sbin/spctl", "--assess", "--type", "execute", "--verbose=2", path])
    for binary, identifier in [("nightr-native", "ai.nightr.controller"), ("nightr-powerhelper", "ai.nightr.powerhelper")]:
        requirement = f'anchor apple generic and identifier "{identifier}" and certificate leaf[subject.OU] = "{team}"'
        run(["/usr/bin/codesign", "--verify", "--strict", "--all-architectures", "-R", requirement, path / "Contents/MacOS" / binary])
    return info["CFBundleShortVersionString"]

def verify_package(path, team):
    if path.is_symlink() or not path.is_file(): raise InstallError("Installer package must be a regular file on the verified read-only image")
    output = run(["/usr/sbin/pkgutil", "--check-signature", path])
    if not re.search(r"Developer ID Installer: [^\n]+ \(" + re.escape(team) + r"\)", output):
        raise InstallError("Installer signature does not match the release team")
    run(["/usr/sbin/spctl", "--assess", "--type", "install", "--verbose=2", path])

@contextlib.contextmanager
def mounted_dmg(path, directory):
    # A user-writable temporary parent would allow mountpoint path substitution
    # between signature verification and the privileged system installer.
    parent = Path("/Volumes")
    current = parent
    while current != Path("/"):
        metadata = current.lstat()
        if metadata.st_uid != 0 or metadata.st_mode & 0o022 or stat.S_ISLNK(metadata.st_mode):
            raise InstallError("A protected system mount directory is required")
        current = current.parent
    mount = parent / ("Nightr-" + uuid.uuid4().hex)
    run(["/usr/bin/hdiutil", "attach", "-readonly", "-nobrowse", "-noautoopen", "-mountpoint", mount, path], timeout=60)
    try:
        if not os.path.ismount(mount) or not (os.statvfs(mount).f_flag & getattr(os, "ST_RDONLY", 1)):
            raise InstallError("Release image is not mounted read-only")
        yield mount
    finally: run(["/usr/bin/hdiutil", "detach", mount], timeout=60)

def verify_installed_root():
    current = APP / "Contents/MacOS/nightr-powerhelper"
    # /Applications itself is normally root:admin 0775. Apple ServiceManagement
    # validates the signed embedded daemon at this shared namespace boundary.
    # No executable in that namespace is ever invoked through sudo.
    while current != APP.parent:
        metadata = current.lstat()
        if metadata.st_uid != 0 or metadata.st_mode & 0o022 or stat.S_ISLNK(metadata.st_mode):
            raise InstallError("The canonical app and embedded helper must be root-owned and not writable by other users; reinstall the signed Nightr.pkg")
        current = current.parent

def download_verified_package(manifest, directory):
    asset = manifest["asset"]
    dmg = directory / asset["name"]
    size = download(asset["url"], dmg, asset["size"])
    if size != asset["size"] or _sha256(dmg) != asset["sha256"]:
        raise InstallError("Release checksum mismatch; nothing was installed")
    return dmg

def cache_artifact(source, version):
    cache = state_directory() / "packages"
    cache.mkdir(mode=0o700, exist_ok=True)
    if cache.is_symlink() or cache.stat().st_uid != os.getuid(): raise InstallError("Unsafe package cache")
    cached = cache / ("Nightr-" + version + "-universal.dmg")
    if cached.is_symlink(): raise InstallError("Unsafe cached package")
    fd, temporary = tempfile.mkstemp(prefix=".package-", dir=cache)
    try:
        with source.open("rb") as input_file, os.fdopen(fd, "wb") as output:
            shutil.copyfileobj(input_file, output); output.flush(); os.fsync(output.fileno())
        os.replace(temporary, cached)
    finally:
        with contextlib.suppress(FileNotFoundError): os.unlink(temporary)
    return cached

def adopt_direct(manifest, directory):
    existing = read_record()
    if existing:
        if existing["owner"] != "direct": raise InstallError("Homebrew installation ownership was preserved")
        return existing
    verify_installed_root()
    receipt = plistlib.loads(run(["/usr/sbin/pkgutil", "--pkg-info-plist", "ai.nightr.package"]).encode())
    with (APP / "Contents/Info.plist").open("rb") as source: info = plistlib.load(source)
    version = info.get("CFBundleShortVersionString")
    if receipt.get("pkg-version") != version or not re.fullmatch(r"0\.[0-9]+\.[0-9]+", version or ""):
        raise InstallError("The installed app has no matching Nightr package receipt")
    if manifest["version"] != version:
        path = directory / "previous-manifest.json"
        download(f"https://github.com/{REPOSITORY}/releases/download/v{version}/nightr-release.json", path, 1024 * 1024)
        manifest = validate_manifest(json.loads(path.read_text()))
        if manifest["version"] != version: raise InstallError("Installed release manifest version differs")
    verify_app(APP, manifest["teamID"], version)
    dmg = download_verified_package(manifest, directory)
    with mounted_dmg(dmg, directory) as mount:
        verify_app(mount / "Nightr.app", manifest["teamID"], version)
        package = mount / "Nightr.pkg"; verify_package(package, manifest["teamID"])
        cached = cache_artifact(dmg, version)
    return write_record("direct", version, manifest["teamID"], _sha256(cached))

def check_owned_link():
    link = Path("/usr/local/bin/nightr")
    if (link.exists() or link.is_symlink()) and (not link.is_symlink() or os.readlink(link) != "/Applications/Nightr.app/Contents/MacOS/nightr"):
        raise InstallError("/usr/local/bin/nightr belongs to another installation; it was preserved")

def install_pkg(path):
    arguments = ["/usr/sbin/installer", "-pkg", str(path), "-target", "/"]
    if os.geteuid() != 0:
        if not sys.stdin.isatty():
            raise InstallError("Administrator authorization is required; run the downloaded signed Nightr.pkg interactively, then nightr setup")
        arguments = ["/usr/bin/sudo", "--", *arguments]
    try: subprocess.run(arguments, check=True, timeout=180)
    except (subprocess.SubprocessError, OSError) as error: raise InstallError("The signed package installation failed") from error

def coordinate(action):
    run([APP / "Contents/MacOS/nightr-native", "package", action], timeout=30)

def perform(mode, manifest, directory):
    record = read_record()
    if record and record["owner"] == "homebrew":
        if mode == "install":
            if APP.is_dir(): return record
            raise InstallError("The Homebrew-owned app is missing; repair it with Homebrew")
        brew = shutil.which("brew")
        if not brew: raise InstallError("This installation belongs to Homebrew; repair Homebrew before updating")
        verify_app(APP, record["teamID"], record["version"]); verify_installed_root()
        coordinate("prepare-update")
        try:
            run([brew, "upgrade", "--cask", "agenthangar/tap/nightr"], timeout=600)
            version = verify_app(APP, record["teamID"]); verify_installed_root()
            coordinate("finish-update")
        except Exception as error:
            # Homebrew owns rollback. Resume only a verifiably valid installation.
            try:
                verify_app(APP, record["teamID"]); coordinate("finish-update")
            except Exception:
                raise InstallError("Homebrew update left an unverified package; controller remains stopped with desired intent preserved. Repair with Homebrew") from error
            raise
        return write_record("homebrew", version, record["teamID"])
    exists = APP.exists() or APP.is_symlink()
    if mode == "update" and not exists:
        raise InstallError("Nightr is not installed; update never installs a missing app")
    if exists and not record:
        record = adopt_direct(manifest, directory)
    if record and exists:
        verify_app(APP, record["teamID"], record["version"])
        if record["teamID"] != manifest["teamID"]: raise InstallError("Release signing team changed; installation was preserved")
        if version_key(record["version"]) >= version_key(manifest["version"]): return record
    check_owned_link()
    dmg = download_verified_package(manifest, directory)
    with mounted_dmg(dmg, directory) as mount:
        verify_app(mount / "Nightr.app", manifest["teamID"], manifest["version"])
        package = mount / "Nightr.pkg"
        verify_package(package, manifest["teamID"])
        cache = state_directory() / "packages"
        cache.mkdir(mode=0o700, exist_ok=True)
        if cache.is_symlink() or cache.stat().st_uid != os.getuid():
            raise InstallError("Unsafe package cache")
        previous = None
        if exists:
            previous = cache / ("Nightr-" + record["version"] + "-universal.dmg")
            if previous.is_symlink() or not previous.is_file() or _sha256(previous) != record.get("artifactSHA256"):
                raise InstallError("The prior signed installer needed for rollback is unavailable; repair from its original release before updating")
            with mounted_dmg(previous, directory) as prior_mount:
                verify_app(prior_mount / "Nightr.app", record["teamID"], record["version"])
                verify_package(prior_mount / "Nightr.pkg", record["teamID"])
            coordinate("prepare-update")
        try:
            install_pkg(package)
            verify_app(APP, manifest["teamID"], manifest["version"]); verify_installed_root()
            if exists: coordinate("finish-update")
            cached = cache_artifact(dmg, manifest["version"])
            return write_record("direct", manifest["version"], manifest["teamID"], _sha256(cached))
        except Exception as original:
            if previous:
                try:
                    if _sha256(previous) != record["artifactSHA256"]: raise InstallError("Rollback image checksum changed")
                    with mounted_dmg(previous, directory) as prior_mount:
                        verify_app(prior_mount / "Nightr.app", record["teamID"], record["version"])
                        verify_package(prior_mount / "Nightr.pkg", record["teamID"])
                        install_pkg(prior_mount / "Nightr.pkg")
                    verify_app(APP, record["teamID"], record["version"])
                    coordinate("finish-update")
                    write_record("direct", record["version"], record["teamID"], record["artifactSHA256"])
                except Exception as rollback:
                    raise InstallError(f"Update and rollback failed; enabled intent and settings were preserved. Repair with {previous}. Update: {original}; rollback: {rollback}") from original
                raise InstallError(f"Update failed and the previous signed version was restored: {original}") from original
            raise

def version_key(value):
    if not isinstance(value, str) or not re.fullmatch(r"0\.[0-9]+\.[0-9]+", value):
        raise InstallError("Invalid installed version")
    return tuple(int(part) for part in value.split("."))

def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""): digest.update(block)
    return digest.hexdigest()

def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["install", "update", "check", "adopt-homebrew", "adopt-direct"])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        if sys.platform != "darwin": raise InstallError("Nightr packages support macOS 13 or later")
        lock = contextlib.nullcontext() if args.action == "check" else installation_lock()
        with lock, tempfile.TemporaryDirectory(prefix="nightr-package-") as temporary:
            directory = Path(temporary)
            manifest = latest_manifest(directory)
            record = read_record()
            if args.action == "check":
                result = {"installed": APP.is_dir(), "owner": record["owner"] if record else None,
                          "version": record["version"] if record else None, "availableVersion": manifest["version"],
                          "updateAvailable": bool(record and version_key(record["version"]) < version_key(manifest["version"]))}
            elif args.action == "adopt-direct":
                result = adopt_direct(manifest, directory)
            elif args.action == "adopt-homebrew":
                brew = shutil.which("brew")
                if not brew: raise InstallError("Homebrew is unavailable")
                receipt = run([brew, "list", "--cask", "--versions", "agenthangar/tap/nightr"])
                if not receipt.strip(): raise InstallError("The Nightr cask is not installed")
                if record and record["owner"] != "homebrew": raise InstallError("Direct installation ownership was preserved")
                version = verify_app(APP, manifest["teamID"]); verify_installed_root()
                result = write_record("homebrew", version, manifest["teamID"])
            else: result = perform(args.action, manifest, directory)
            print(json.dumps(result, sort_keys=True) if args.json else ("Nightr " + str(result.get("version") or "not installed") + " · " + str(result.get("owner") or "unmanaged")))
        return 0
    except (InstallError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        if args.json: print(json.dumps({"error": str(error)}, sort_keys=True))
        else: print(f"nightr: {error}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
