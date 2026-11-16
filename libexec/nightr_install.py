# Shared with agenthangar/nightr scripts/nightr_install.py; preserve verified package ownership.
#!/usr/bin/env python3
"""Nightr's stdlib package owner/verified-release installer. No source builds.

Shared unchanged with t; acquisition authority is the repository's HTTPS GitHub
release API. The artifact additionally needs Apple Developer ID/notary approval.
This script never authorizes the power helper or changes enabled intent.
"""
from __future__ import annotations
import argparse
import contextlib
import ctypes
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
GLOBAL_LOCK = Path("/Library/Application Support/Nightr/update.lock")
CONTROLLER_LOCK = GLOBAL_LOCK.parent / "controller.lock"
ROOT_MARKER = GLOBAL_LOCK.parent / "install-transaction.json"
INSTALL_GUARD = Path("/Library/PrivilegedHelperTools/ai.nightr.installguard")

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

def phase(message):
    print(message, file=sys.stderr, flush=True)

def run(arguments, *, timeout=30, env_updates=None):
    try:
        result = subprocess.run([str(x) for x in arguments], check=True, capture_output=True, text=True,
                                timeout=timeout, env={**os.environ, "LC_ALL": "C", **(env_updates or {})})
    except (subprocess.SubprocessError, OSError) as error:
        raise InstallError(f"Command failed: {arguments[0]}: {error}") from error
    return result.stdout + result.stderr

def run_passthrough(arguments, *, timeout, env_updates=None):
    try:
        subprocess.run([str(value) for value in arguments], check=True, timeout=timeout,
                       stdout=sys.stderr, stderr=sys.stderr,
                       env={**os.environ, "LC_ALL": "C", **(env_updates or {})})
    except (subprocess.SubprocessError, OSError) as error:
        raise InstallError(f"Command failed: {arguments[0]}: {error}") from error


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

class PackageLockHandle:
    def __init__(self, fd, path): self.fd = fd; self.path = path; self.held = False
    def acquire(self):
        try:
            if self.path == GLOBAL_LOCK: fcntl.lockf(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 0, 0, os.SEEK_SET)
            else: fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error: raise InstallError("Another user's Nightr installation or update is running") from error
        self.held = True
    def release(self):
        if self.held:
            if self.path == GLOBAL_LOCK: fcntl.lockf(self.fd, fcntl.LOCK_UN, 0, 0, os.SEEK_SET)
            else: fcntl.flock(self.fd, fcntl.LOCK_UN)
            self.held = False

@contextlib.contextmanager
def global_installation_lock(*, required=False, path=None):
    path = GLOBAL_LOCK if path is None else path
    # The signed root-owned package installs this readable advisory lock. Users
    # can flock a read-only descriptor; nobody can replace its protected entry.
    if not path.exists() and not path.is_symlink():
        if required: raise InstallError("The machine package lock is missing; repair the signed Nightr.pkg installation")
        yield False
        return
    current = path.parent
    while current != Path("/"):
        metadata = current.lstat()
        if metadata.st_uid != 0 or metadata.st_mode & 0o022 or stat.S_ISLNK(metadata.st_mode):
            raise InstallError("Unsafe machine package lock directory")
        current = current.parent
    fd = os.open(path, (os.O_RDWR if path == GLOBAL_LOCK else os.O_RDONLY) | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(fd)
        if metadata.st_uid != 0 or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or stat.S_IMODE(metadata.st_mode) != 0o666:
            raise InstallError("Unsafe machine package lock")
        handle = PackageLockHandle(fd, path)
        handle.acquire()
        try: yield handle
        finally: handle.release()
    finally: os.close(fd)

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
        with global_installation_lock(required=APP.exists()) as global_locked:
            assert_no_marker()
            yield global_locked
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
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", "--all-architectures", "-R=" + requirement, path])
    run(["/usr/sbin/spctl", "--assess", "--type", "execute", "--verbose=2", path])
    for binary, identifier in [("nightr-native", "ai.nightr.controller"), ("nightr-powerhelper", "ai.nightr.powerhelper"), ("NightrNetworkWorker", "ai.nightr.networkworker")]:
        requirement = f'anchor apple generic and identifier "{identifier}" and certificate leaf[subject.OU] = "{team}"'
        run(["/usr/bin/codesign", "--verify", "--strict", "--all-architectures", "-R=" + requirement, path / "Contents/MacOS" / binary])
    return info["CFBundleShortVersionString"]

def verify_package(path, team):
    phase("Verifying Nightr package...")
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
    current = INSTALL_GUARD
    while current != Path("/"):
        info = current.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode): raise InstallError("Unsafe canonical installer guard")
        current = current.parent

def download_verified_package(manifest, directory):
    phase("Downloading Nightr...")
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

def adopt_direct(manifest, directory, *, explicit_claim=False):
    existing = read_record()
    if existing and existing["owner"] == "homebrew": raise InstallError("Homebrew installation ownership was preserved")
    if homebrew_receipt(known_direct=bool(explicit_claim or (existing and existing["owner"] == "direct"))):
        raise InstallError("This app belongs to Homebrew; use adopt-homebrew without transferring ownership")
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
    phase("Installing Nightr...")
    arguments = ["/usr/sbin/installer", "-pkg", str(path), "-target", "/"]
    if os.geteuid() != 0:
        if not sys.stdin.isatty():
            raise InstallError("Administrator authorization is required; run the downloaded signed Nightr.pkg interactively, then nightr setup")
        arguments = ["/usr/bin/sudo", "--", *arguments]
    try: subprocess.run(arguments, check=True, timeout=180, stdout=sys.stderr, stderr=sys.stderr)
    except (subprocess.SubprocessError, OSError) as error: raise InstallError("The signed package installation failed") from error

def coordinate(action):
    run([APP / "Contents/MacOS/nightr-native", "package", action], timeout=30)

# Becomes true only after the reviewed PKG-only template is accepted.
HOMEBREW_CONTRACT_VERIFIED = True
AUDITED_CASK_TEMPLATE = 'cask "nightr" do\n  version "{{VERSION}}"\n  sha256 "{{SHA256}}"\n\n  url "https://github.com/agenthangar/nightr/releases/download/v#{version}/Nightr-#{version}-universal.dmg"\n  name "Nightr"\n  desc "Keep a Mac awake for coding-agent work"\n  homepage "https://nightr.ai"\n  depends_on macos: ">= :ventura"\n\n  pkg "Nightr.pkg"\nend\n'

def homebrew_receipt(brew=None, *, known_direct=False):
    brew = brew or shutil.which("brew")
    if not brew:
        if not known_direct:
            raise InstallError("Homebrew ownership is unknown because its receipt query is unavailable. If this app was installed directly and no custom-prefix Homebrew owns it, use nightr setup --direct-updates")
        for path in [Path("/opt/homebrew/Caskroom/nightr"), Path("/usr/local/Caskroom/nightr")]:
            try: path.lstat()
            except FileNotFoundError: continue
            except OSError as error: raise InstallError("Homebrew receipt locations could not be checked; installation ownership remains unknown") from error
            else: return True
        return False
    # Listing all receipts succeeds with an empty result when no casks exist.
    # Querying one missing cask can itself fail, which is not absence evidence.
    try:
        output = run([brew, "list", "--cask", "--versions"], env_updates={"HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_INSTALL_FROM_API": "1"})
    except InstallError as error:
        raise InstallError("Homebrew ownership could not be checked; repair Homebrew before adopting or replacing the installed app") from error
    for line in output.splitlines():
        fields = line.split()
        if fields and fields[0].split("/")[-1] == "nightr": return True
    return False


def verified_homebrew_contract(manifest, *, required=True):
    brew = shutil.which("brew")
    if brew: brew = str(Path(brew).resolve())
    if not HOMEBREW_CONTRACT_VERIFIED or not brew:
        if required: raise InstallError("This release has no approved Homebrew upgrade contract; use or repair the signed direct package")
        return None
    try:
        policy = {"HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_INSTALL_FROM_API": "1"}
        taps = run([brew, "tap"], env_updates=policy).splitlines()
        if "agenthangar/tap" not in taps: raise InstallError("The reviewed Nightr tap is not installed; use the verified direct package")
        source = run([brew, "cat", "--cask", "agenthangar/tap/nightr"], env_updates=policy)
        version = re.search(r'^  version "(0\.[0-9]+\.[0-9]+)"$', source, re.MULTILINE)
        digest = re.search(r'^  sha256 "([0-9a-f]{64})"$', source, re.MULTILINE)
        if not version or not digest or version.group(1) != manifest["version"] or digest.group(1) != manifest["asset"]["sha256"]:
            raise InstallError("Homebrew cask version or checksum differs from the verified release")
        normalized = source[:version.start(1)] + "{{VERSION}}" + source[version.end(1):]
        normalized = re.sub(r'^  sha256 "[0-9a-f]{64}"$', '  sha256 "{{SHA256}}"', normalized, flags=re.MULTILINE)
        if normalized.rstrip() != AUDITED_CASK_TEMPLATE.rstrip(): raise InstallError("Homebrew cask contains unreviewed actions; package ownership was preserved")
        return brew
    except InstallError:
        if required: raise
        return None

def verify_installed_receipt(team):
    actual = verify_app(APP, team); verify_installed_root()
    requirement = f'anchor apple generic and identifier "ai.nightr.installguard" and certificate leaf[subject.OU] = "{team}"'
    run(["/usr/bin/codesign", "--verify", "--strict", "--all-architectures", "-R=" + requirement, INSTALL_GUARD])
    receipt = plistlib.loads(run(["/usr/sbin/pkgutil", "--pkg-info-plist", "ai.nightr.package"]).encode())
    if receipt.get("pkg-version") != actual: raise InstallError("Installed package receipt differs from the signed app")
    return actual

def brew_transaction(mode, manifest, global_handle, post, existing):
    brew = verified_homebrew_contract(manifest)
    if existing:
        coordinate("prepare-update"); post.append("finish-update")
        with global_installation_lock(required=True, path=CONTROLLER_LOCK): pass
    if global_handle:
        if not isinstance(global_handle, PackageLockHandle): raise InstallError("Homebrew updates require the machine transaction coordinator")
        global_handle.release()
    failure = None
    try:
        phase("Installing Nightr with Homebrew...")
        run_passthrough([brew, "upgrade" if existing else "install", "--cask", "agenthangar/tap/nightr"], timeout=600, env_updates={"HOMEBREW_NO_AUTO_UPDATE": "1", "HOMEBREW_NO_INSTALL_FROM_API": "1"})
    except BaseException as error: failure = error
    # The audited cask mutates payload only through its guarded PKG. Every exit
    # reacquires the machine gate; no Python rollback or foreign marker clearing.
    try:
        reacquire = contextlib.nullcontext(global_handle) if global_handle else global_installation_lock(required=True)
        if global_handle: global_handle.acquire()
        with reacquire:
            assert_no_marker()
            actual = verify_installed_receipt(manifest["teamID"])
            if not homebrew_receipt(brew): raise InstallError("The Homebrew receipt is missing; repair ownership with Homebrew")
            result = write_record("homebrew", actual, manifest["teamID"])
    except BaseException:
        post.clear()
        raise
    if failure:
        if isinstance(failure, Exception): raise failure
        post.clear(); raise InstallError("Homebrew was interrupted; the installed package was verified and protection remains stopped")
    if actual != manifest["version"]: raise InstallError("Homebrew kept a different verified version; check the tap before retrying")
    return result

def refreshed_record(record, manifest, directory):
    actual = verify_app(APP, record["teamID"])
    verify_installed_root(); version_key(actual)
    if actual == record["version"]: return record
    # Another user may have updated this machine. Never drive the new controller
    # or choose rollback from stale per-user metadata.
    if record["owner"] == "homebrew":
        if not homebrew_receipt(): raise InstallError("Homebrew receipt is missing; repair package ownership")
        return write_record("homebrew", actual, record["teamID"])
    receipt = plistlib.loads(run(["/usr/sbin/pkgutil", "--pkg-info-plist", "ai.nightr.package"]).encode())
    if receipt.get("pkg-version") != actual: raise InstallError("Installed version and package receipt differ")
    prior_manifest = manifest
    if actual != manifest["version"]:
        path = directory / "installed-manifest.json"
        download(f"https://github.com/{REPOSITORY}/releases/download/v{actual}/nightr-release.json", path, 1024 * 1024)
        prior_manifest = validate_manifest(json.loads(path.read_text()))
    if prior_manifest["version"] != actual or prior_manifest["teamID"] != record["teamID"]:
        raise InstallError("Installed release metadata does not match the signed package")
    image = download_verified_package(prior_manifest, directory)
    with mounted_dmg(image, directory) as mount:
        verify_app(mount / "Nightr.app", record["teamID"], actual)
        verify_package(mount / "Nightr.pkg", record["teamID"])
        cached = cache_artifact(image, actual)
    return write_record("direct", actual, record["teamID"], _sha256(cached))

def assert_no_marker():
    try: ROOT_MARKER.lstat()
    except FileNotFoundError: return
    raise InstallError("A protected package transaction is pending; let installation finish, or reboot before repairing with its signed Nightr.pkg")

def current_process_identity():
    # Public Darwin proc_bsdinfo layout, from the macOS SDK sys/proc_info.h.
    class BSDInfo(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint32) for name in ("flags", "status", "xstatus", "pid", "ppid", "uid", "gid", "ruid", "rgid", "svuid", "svgid", "reserved")]
        _fields_ += [("comm", ctypes.c_char * 16), ("name", ctypes.c_char * 32)]
        _fields_ += [(name, ctypes.c_uint32) for name in ("nfiles", "pgid", "pjobc", "dev", "tpgid")]
        _fields_ += [("nice", ctypes.c_int32), ("startSeconds", ctypes.c_uint64), ("startMicroseconds", ctypes.c_uint64)]
    library = ctypes.CDLL("/usr/lib/libproc.dylib", use_errno=True)
    library.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
    info = BSDInfo()
    if library.proc_pidinfo(os.getpid(), 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info) or info.pid != os.getpid() or info.uid != os.geteuid():
        raise InstallError("Cannot authenticate this package coordinator's process identity")
    system = ctypes.CDLL("/usr/lib/libSystem.B.dylib", use_errno=True)
    system.sysctlbyname.argtypes = [ctypes.c_char_p, ctypes.c_void_p, ctypes.POINTER(ctypes.c_size_t), ctypes.c_void_p, ctypes.c_size_t]
    size = ctypes.c_size_t(128); buffer = ctypes.create_string_buffer(128)
    if system.sysctlbyname(b"kern.bootsessionuuid", buffer, ctypes.byref(size), None, 0) != 0:
        raise InstallError("Cannot authenticate this boot session")
    return {"pid": info.pid, "uid": info.uid, "processStartSeconds": info.startSeconds, "processStartMicroseconds": info.startMicroseconds, "bootID": str(uuid.UUID(buffer.value.decode("ascii"))).upper()}


def write_install_request(transaction, package, version, original_version, team, operation="install"):
    directory = state_directory()
    if directory.is_symlink() or directory.stat().st_uid != os.geteuid() or stat.S_IMODE(directory.stat().st_mode) != 0o700:
        raise InstallError("Package transaction requests require a private owned Nightr state directory")
    request = {"schemaVersion": 1, "transaction": str(transaction), "owner": current_process_identity(),
               "packagePath": str(package), "version": version, "originalVersion": original_version, "teamID": team, "operation": operation}
    fd, name = tempfile.mkstemp(prefix=".install-request-", dir=directory)
    try:
        with os.fdopen(fd, "w") as output:
            json.dump(request, output, sort_keys=True); output.flush(); os.fsync(output.fileno())
        os.replace(name, directory / "install-request.json")
    finally:
        with contextlib.suppress(FileNotFoundError): os.unlink(name)

def finish_install_transaction(transaction, team, *, operation="finish"):
    phase("Finishing Nightr installation..." if operation == "finish" else "Checking verified rollback readiness...")
    current = INSTALL_GUARD
    while current != Path("/"):
        info = current.lstat()
        if info.st_uid != 0 or info.st_mode & 0o022 or stat.S_ISLNK(info.st_mode): raise InstallError("Unsafe canonical installer guard")
        current = current.parent
    requirement = f'anchor apple generic and identifier "ai.nightr.installguard" and certificate leaf[subject.OU] = "{team}"'
    run(["/usr/bin/codesign", "--verify", "--strict", "--all-architectures", "-R=" + requirement, INSTALL_GUARD])
    arguments = [str(INSTALL_GUARD), operation, str(transaction)]
    if os.geteuid() != 0:
        if not sys.stdin.isatty(): raise InstallError("Administrator authorization is needed to finish the verified package transaction")
        arguments = ["/usr/bin/sudo", "--", *arguments]
    try: subprocess.run(arguments, check=True, timeout=30, stdout=sys.stderr, stderr=sys.stderr)
    except (subprocess.SubprocessError, OSError) as error: raise InstallError("The protected package transaction remains pending; reboot before repairing with its signed Nightr.pkg") from error
    if operation == "finish":
        with contextlib.suppress(FileNotFoundError): (state_directory() / "install-request.json").unlink()


def perform(mode, manifest, directory, *, global_lock_held=True, resume_after_unlock=None):
    post = resume_after_unlock if resume_after_unlock is not None else []
    record = read_record()
    exists = APP.exists() or APP.is_symlink()
    if mode == "update" and not exists:
        raise InstallError("Nightr is not installed; update never installs a missing app")
    if record and record["owner"] == "homebrew":
        if not exists: raise InstallError("The Homebrew-owned app is missing; repair it with Homebrew")
        record = refreshed_record(record, manifest, directory)
        if mode == "install": return record
        if not homebrew_receipt(): raise InstallError("The Homebrew receipt is missing; repair ownership")
        return brew_transaction(mode, manifest, global_lock_held, post, True)
    if not exists and not record and mode == "install" and verified_homebrew_contract(manifest, required=False):
        return brew_transaction(mode, manifest, global_lock_held, post, False)
    if exists and not record: record = adopt_direct(manifest, directory)
    if record and exists:
        if homebrew_receipt(known_direct=True): raise InstallError("The machine now has a Homebrew receipt; direct ownership was not transferred")
        record = refreshed_record(record, manifest, directory)
        if record["teamID"] != manifest["teamID"]: raise InstallError("Release signing team changed; installation was preserved")
        if version_key(record["version"]) >= version_key(manifest["version"]): return record
    check_owned_link()
    dmg = download_verified_package(manifest, directory)
    transaction = uuid.uuid4()
    managed = global_lock_held
    with mounted_dmg(dmg, directory) as mount:
        verify_app(mount / "Nightr.app", manifest["teamID"], manifest["version"])
        package = mount / "Nightr.pkg"; verify_package(package, manifest["teamID"])
        cache = state_directory() / "packages"; cache.mkdir(mode=0o700, exist_ok=True)
        if cache.is_symlink() or cache.stat().st_uid != os.getuid(): raise InstallError("Unsafe package cache")
        previous = None
        if exists:
            previous = cache / ("Nightr-" + record["version"] + "-universal.dmg")
            if previous.is_symlink() or not previous.is_file() or _sha256(previous) != record.get("artifactSHA256"):
                raise InstallError("The prior signed installer needed for rollback is unavailable; repair from its original release before updating")
            with mounted_dmg(previous, directory) as prior_mount:
                verify_app(prior_mount / "Nightr.app", record["teamID"], record["version"])
                verify_package(prior_mount / "Nightr.pkg", record["teamID"])
            coordinate("prepare-update"); post.append("finish-update")
        controller_guard = global_installation_lock(required=True, path=CONTROLLER_LOCK) if managed else contextlib.nullcontext()
        with controller_guard:
            committed = False
            try:
                if managed: write_install_request(transaction, package, manifest["version"], record["version"] if record else None, manifest["teamID"])
                install_pkg(package)
                # Fresh installs have no machine lock until the signed preinstall
                # creates it. Reacquire and verify the actual installed package.
                update_guard = contextlib.nullcontext(True) if managed else global_installation_lock(required=True)
                with update_guard:
                    verify_app(APP, manifest["teamID"], manifest["version"]); verify_installed_root()
                    if not managed: assert_no_marker()
                    receipt = plistlib.loads(run(["/usr/sbin/pkgutil", "--pkg-info-plist", "ai.nightr.package"]).encode())
                    if receipt.get("pkg-version") != manifest["version"]: raise InstallError("Installed package receipt differs from the release")
                    cached = cache_artifact(dmg, manifest["version"])
                    result = write_record("direct", manifest["version"], manifest["teamID"], _sha256(cached))
                    committed = True
                    if managed: finish_install_transaction(transaction, manifest["teamID"])
                    return result
            except Exception as original:
                if committed:
                    post.clear()
                    raise InstallError(f"The signed package and metadata were committed, but its protected transaction could not finish; protection remains stopped. {original}") from original
                if previous:
                    try:
                        finish_install_transaction(transaction, record["teamID"], operation="rollback-ready")
                        if _sha256(previous) != record["artifactSHA256"]: raise InstallError("Rollback image checksum changed")
                        with mounted_dmg(previous, directory) as prior_mount:
                            verify_app(prior_mount / "Nightr.app", record["teamID"], record["version"])
                            verify_package(prior_mount / "Nightr.pkg", record["teamID"])
                            write_install_request(transaction, prior_mount / "Nightr.pkg", record["version"], record["version"], record["teamID"], "rollback")
                            install_pkg(prior_mount / "Nightr.pkg")
                        verify_app(APP, record["teamID"], record["version"]); verify_installed_root()
                        write_record("direct", record["version"], record["teamID"], record["artifactSHA256"])
                        finish_install_transaction(transaction, record["teamID"])
                    except Exception as rollback:
                        post.clear()
                        raise InstallError(f"Update could not roll back safely; protection remains stopped. If a package transaction is pending, reboot before repairing or reinstalling its signed package. Update: {original}; rollback: {rollback}") from original
                    raise InstallError(f"Update failed and the previous signed version was restored: {original}") from original
                post.clear()
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
    post = []
    try:
        if sys.platform != "darwin": raise InstallError("Nightr packages support macOS 13 or later")
        lock = contextlib.nullcontext(False) if args.action == "check" else installation_lock()
        with lock as global_locked, tempfile.TemporaryDirectory(prefix="nightr-package-") as temporary:
            directory = Path(temporary)
            phase("Checking Nightr release...")
            manifest = latest_manifest(directory)
            record = read_record()
            if args.action == "check":
                result = {"installed": APP.is_dir(), "owner": record["owner"] if record else None,
                          "version": record["version"] if record else None, "availableVersion": manifest["version"],
                          "updateAvailable": bool(record and version_key(record["version"]) < version_key(manifest["version"]))}
            elif args.action == "adopt-direct":
                result = adopt_direct(manifest, directory, explicit_claim=True)
            elif args.action == "adopt-homebrew":
                brew = verified_homebrew_contract(manifest)
                if not brew: raise InstallError("Homebrew is unavailable")
                receipt = run([brew, "list", "--cask", "--versions", "agenthangar/tap/nightr"])
                if not receipt.strip(): raise InstallError("The Nightr cask is not installed")
                if record and record["owner"] != "homebrew": raise InstallError("Direct installation ownership was preserved")
                version = verify_installed_receipt(manifest["teamID"])
                result = write_record("homebrew", version, manifest["teamID"])
            else: result = perform(args.action, manifest, directory, global_lock_held=global_locked, resume_after_unlock=post)
        if post:
            post.clear()
            try: coordinate("finish-update")
            except Exception as error:
                raise InstallError(f"Package changes committed, but protection could not resume. Run nightr start after repairing setup: {error}") from error
            post.clear()
        print(json.dumps(result, sort_keys=True) if args.json else ("Nightr " + str(result.get("version") or "not installed") + " · " + str(result.get("owner") or "unmanaged")))
        return 0
    except (InstallError, OSError, ValueError, KeyError, TypeError, AttributeError) as error:
        if post:
            try: coordinate("finish-update")
            except Exception as resume_error: error = InstallError(f"{error}; protection could not resume: {resume_error}")
        if args.json: print(json.dumps({"error": str(error)}, sort_keys=True))
        else: print(f"nightr: {error}", file=sys.stderr)
        return 1

if __name__ == "__main__":
    raise SystemExit(main())
