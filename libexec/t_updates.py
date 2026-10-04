"""Quiet, source-aware update discovery for the t CLI.

The foreground only reads a small cache and starts one detached checker. Discovery
never installs software or changes a worktree's files.
"""

import contextlib
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.request

INTERVAL = 24 * 60 * 60
RETRY = 60 * 60
SEMVER = re.compile(r"v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)\Z")
SHA = re.compile(r"[0-9a-f]{40,64}\Z")
API = "https://api.github.com/repos/agenthangar/t/releases/latest"
TAP_FORMULA = "https://raw.githubusercontent.com/agenthangar/homebrew-tap/main/Formula/t.rb"
PROJECT_REMOTES = {"https://github.com/agenthangar/t", "https://github.com/agenthangar/t.git",
                   "git@github.com:agenthangar/t.git", "ssh://git@github.com/agenthangar/t.git"}


def _valid_gap(gap):
    return (isinstance(gap, dict) and isinstance(gap.get("version"), str)
            and bool(SEMVER.fullmatch(gap["version"])) and type(gap.get("count")) is int
            and 0 < gap["count"] <= 1000000)


def _homebrew_gap(identity):
    """Compare main with the actual tap version, only for this project's checkout."""
    if identity["kind"] != "git" or identity["remote"] not in PROJECT_REMOTES:
        return None
    request = urllib.request.Request(TAP_FORMULA, headers={"User-Agent": "t-update-check"})
    with urllib.request.urlopen(request, timeout=8) as response:
        formula = response.read(65537)
    if len(formula) > 65536:
        return None
    match = re.search(rb'url "https://github.com/agenthangar/t/releases/download/(v[0-9.]+)/t\.tar\.gz"', formula)
    if not match:
        return None
    version = match[1].decode("ascii")
    if not SEMVER.fullmatch(version):
        return None
    url = f"https://api.github.com/repos/agenthangar/t/compare/{version}...main?per_page=1&page=2"
    request = urllib.request.Request(url, headers={"Accept": "application/vnd.github+json", "User-Agent": "t-update-check"})
    with urllib.request.urlopen(request, timeout=8) as response:
        payload = response.read(65537)
    if len(payload) > 65536:
        return None
    data = json.loads(payload)
    if not isinstance(data, dict) or data.get("status") != "ahead":
        return None
    gap = {"version": version, "count": data.get("ahead_by")}
    return gap if _valid_gap(gap) else None


def _identity(installation):
    if not isinstance(installation, dict):
        return None
    kind = installation.get("kind")
    source = installation.get("source")
    current = installation.get("current")
    if kind not in ("git", "release", "brew") or not isinstance(source, str) or not os.path.isabs(source):
        return None
    if not isinstance(current, str) or len(current) > 100:
        return None
    fields = {"kind": kind, "source": os.path.realpath(source), "current": current}
    if kind == "git":
        remote = installation.get("remote")
        if not SHA.fullmatch(current) or not isinstance(remote, str) or not remote:
            return None
        fields["remote"] = remote
    else:
        if not SEMVER.fullmatch(current):
            return None
        if kind == "brew":
            brew = installation.get("brew")
            formula = installation.get("formula")
            if not isinstance(brew, str) or not os.path.isabs(brew) or formula != "agenthangar/tap/t":
                return None
            fields.update(brew=brew, formula=formula)
    return fields


def _cache_path(identity):
    base = os.environ.get("XDG_CACHE_HOME")
    if not base or not os.path.isabs(base):
        base = os.path.join(os.path.expanduser("~"), ".cache")
    digest = hashlib.sha256((identity["kind"] + "\0" + identity["source"]).encode()).hexdigest()
    return Path(base) / "t" / "updates" / (digest + ".json")


def _skip_path(identity):
    # Keep the prompt choice outside the checker cache so a slow refresh cannot
    # block it or overwrite it when the cache is replaced.
    return _cache_path(identity).with_suffix(".skip.json")


def _skipped_version(identity):
    path = _skip_path(identity)
    try:
        if path.is_symlink() or path.stat().st_size > 8192:
            return ""
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return ""
    if not isinstance(data, dict) or data.get("identity") != identity:
        return ""
    version = data.get("version")
    if not isinstance(version, str):
        return ""
    valid = SHA if identity["kind"] == "git" else SEMVER
    return version if valid.fullmatch(version) else ""


def _read(path, identity):
    try:
        if path.is_symlink() or path.stat().st_size > 8192:
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return {}
    if not isinstance(data, dict) or data.get("identity") != identity:
        return {}
    if type(data.get("checked_at")) not in (int, float) or type(data.get("expires_at")) not in (int, float):
        return {}
    if type(data.get("available")) is not bool or not isinstance(data.get("latest"), str):
        return {}
    if type(data.get("snoozed_until", 0)) not in (int, float):
        return {}
    if not all(math.isfinite(data[key]) for key in ("checked_at", "expires_at")) or not math.isfinite(data.get("snoozed_until", 0)):
        return {}
    if type(data.get("pending", False)) is not bool or type(data.get("error", False)) is not bool:
        return {}
    if data.get("unreleased") is not None and not _valid_gap(data["unreleased"]):
        return {}
    latest = data["latest"]
    if data["available"] and (not latest or data.get("pending") or data.get("error")):
        return {}
    if len(latest) > 100 or (identity["kind"] == "git" and latest and not SHA.fullmatch(latest)) or (identity["kind"] != "git" and latest and not SEMVER.fullmatch(latest)):
        return {}
    return data


def _write(path, data):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    fd, tmp = tempfile.mkstemp(prefix=".updates-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, separators=(",", ":"))
            stream.write("\n")
        os.replace(tmp, path)
    finally:
        with contextlib.suppress(OSError):
            os.unlink(tmp)


def _run(args, cwd=None, timeout=12):
    env = {k: v for k, v in os.environ.items() if not k.startswith("COV_CORE_")}
    env.update(GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="Never")
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes -o ConnectTimeout=5")
    return subprocess.run(args, cwd=cwd, input="", capture_output=True, text=True,
                          timeout=timeout, env=env, check=False)


def _git(identity):
    source = identity["source"]
    def command(*args):
        result = _run(["git", "-C", source, *args])
        return result.stdout.strip() if result.returncode == 0 else None
    if command("rev-parse", "HEAD") != identity["current"]:
        return None
    if command("symbolic-ref", "--quiet", "--short", "HEAD") != "main":
        return None
    if command("remote", "get-url", "origin") != identity["remote"]:
        return None
    if command("status", "--porcelain", "--untracked-files=normal") != "":
        return None
    fetched = _run(["git", "-C", source, "fetch", "--quiet", "--no-tags", "origin",
                    "+refs/heads/main:refs/remotes/origin/main"], timeout=20)
    if fetched.returncode != 0:
        return None
    latest = command("rev-parse", "refs/remotes/origin/main")
    if not latest or not SHA.fullmatch(latest):
        return None
    if command("rev-parse", "HEAD") != identity["current"] or command("remote", "get-url", "origin") != identity["remote"]:
        return None
    if command("status", "--porcelain", "--untracked-files=normal") != "":
        return None
    if latest == identity["current"]:
        return ""
    ancestor = _run(["git", "-C", source, "merge-base", "--is-ancestor", identity["current"], latest])
    return latest if ancestor.returncode == 0 else None


def _release(identity):
    request = urllib.request.Request(API, headers={"Accept": "application/vnd.github+json", "User-Agent": "t-update-check"})
    with urllib.request.urlopen(request, timeout=8) as response:
        payload = response.read(65537)
    if len(payload) > 65536:
        return None
    data = json.loads(payload)
    if not isinstance(data, dict) or data.get("draft") is not False or data.get("prerelease") is not False:
        return None
    latest = data.get("tag_name")
    if not isinstance(latest, str) or not SEMVER.fullmatch(latest):
        return None
    return latest if _version(latest) > _version(identity["current"]) else ""


def _version(value):
    return tuple(int(part) for part in SEMVER.fullmatch(value).groups())


def _brew(identity):
    result = _run([identity["brew"], "outdated", "--json=v2", "--formula", identity["formula"]], timeout=20)
    # brew outdated returns 1 when at least one formula is outdated.
    if result.returncode not in (0, 1) or len(result.stdout) > 65536:
        return None
    data = json.loads(result.stdout)
    if not isinstance(data, dict) or not isinstance(data.get("formulae"), list):
        return None
    formulae = data["formulae"]
    if not formulae:
        return ""
    if len(formulae) != 1 or not isinstance(formulae[0], dict):
        return None
    formula = formulae[0]
    if formula.get("name") not in ("t", identity["formula"]):
        return None
    installed = formula.get("installed_versions")
    if not isinstance(installed, list) or identity["current"].lstrip("v") not in [str(item).lstrip("v") for item in installed]:
        return None
    raw = formula.get("current_version")
    latest = raw if isinstance(raw, str) and raw.startswith("v") else "v" + raw if isinstance(raw, str) else ""
    return latest if SEMVER.fullmatch(latest) and _version(latest) > _version(identity["current"]) else None


def _state(installation, respect_snooze):
    identity = _identity(installation)
    state = {"available": False, "latest": "", "current": installation.get("current", "") if isinstance(installation, dict) else "",
             "checked": False, "error": False}
    if identity is None:
        return state
    data = _read(_cache_path(identity), identity)
    skipped = _skipped_version(identity) if respect_snooze else ""
    now = time.time()
    if data and data.get("expires_at", 0) > now and not data.get("pending"):
        state.update(checked=True, error=bool(data.get("error")))
    if (data.get("expires_at", 0) > now and data.get("available")
            and (not respect_snooze or (data.get("snoozed_until", 0) <= now
                                        and skipped != data["latest"]))):
        if identity["kind"] != "git" or _git_offer_safe(identity):
            state.update(available=True, latest=data["latest"])
    if (identity["kind"] == "git" and identity["remote"] in PROJECT_REMOTES
            and data.get("expires_at", 0) > now and data.get("unreleased")
            and not data.get("available")
            and (not respect_snooze or data.get("snoozed_until", 0) <= now)
            and _git_offer_safe(identity)):
        state["unreleased"] = data["unreleased"]
    return state


def cached(installation):
    """Return a validated, fresh and unsnoozed update offer, or no offer."""
    return _state(installation, True)


def _git_offer_safe(identity):
    source = identity["source"]
    checks = (("rev-parse", "HEAD", identity["current"]),
              ("symbolic-ref", "--quiet", "--short", "HEAD", "main"),
              ("remote", "get-url", "origin", identity["remote"]),
              ("status", "--porcelain", "--untracked-files=normal", ""))
    for *args, expected in checks:
        try:
            result = _run(["git", "-C", source, *args], timeout=4)
        except (OSError, subprocess.SubprocessError):
            return False
        if result.returncode != 0 or result.stdout.strip() != expected:
            return False
    return True


def check(installation, force=False):
    """Perform a bounded source check and cache its result; suppress failures."""
    identity = _identity(installation)
    if identity is None:
        return _state(installation, False)
    path = _cache_path(identity)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    try:
        with open(lock_path, "a+b") as lock:
            os.chmod(lock_path, 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            prior = _read(path, identity)
            now = time.time()
            if not force and prior.get("expires_at", 0) > now and not prior.get("pending"):
                return _state(installation, False)
            try:
                latest = {"git": _git, "release": _release, "brew": _brew}[identity["kind"]](identity)
            except (OSError, ValueError, TimeoutError, subprocess.SubprocessError, json.JSONDecodeError):
                latest = None
            gap = None
            if latest is not None:
                try:
                    gap = _homebrew_gap(identity)
                except (OSError, ValueError, TimeoutError):
                    pass  # Tap/network failure must not hide an ordinary update.
            data = {"identity": identity, "checked_at": now,
                    "expires_at": now + (INTERVAL if latest is not None else RETRY),
                    "available": bool(latest), "latest": latest or "",
                    "snoozed_until": prior.get("snoozed_until", 0), "error": latest is None,
                    "pending": False, "unreleased": gap}
            _write(path, data)
    except OSError:
        pass
    return _state(installation, False)


def snooze(installation, seconds=INTERVAL):
    identity = _identity(installation)
    if identity is None:
        return
    path = _cache_path(identity)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(path.with_suffix(".lock"), "a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            data = _read(path, identity)
            if not data:
                data = {"identity": identity, "checked_at": 0, "expires_at": 0,
                        "available": False, "latest": ""}
            data["snoozed_until"] = time.time() + max(0, seconds)
            _write(path, data)
    except OSError:
        pass


def skip_version(installation, version):
    """Hide one offered version until discovery finds a different target."""
    identity = _identity(installation)
    if identity is None or not isinstance(version, str) or not (
            SHA.fullmatch(version) if identity["kind"] == "git" else SEMVER.fullmatch(version)):
        return
    # A prompt can stay open across a refresh. Its old choice must not replace
    # a skip for the version that discovery now offers.
    latest = _read(_cache_path(identity), identity).get("latest")
    if latest and latest != version:
        return
    path = _skip_path(identity)
    try:
        _write(path, {"identity": identity, "version": version})
    except OSError:
        pass


def schedule(installation, executable):
    """Start one detached cache refresh when due, without delaying the CLI."""
    identity = _identity(installation)
    if identity is None:
        return
    path = _cache_path(identity)
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with open(path.with_suffix(".lock"), "a+b") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            prior = _read(path, identity)
            now = time.time()
            if prior.get("expires_at", 0) > now:
                return
            prior = {"identity": identity, "checked_at": now, "expires_at": now + RETRY,
                     "available": False, "latest": "",
                     "snoozed_until": prior.get("snoozed_until", 0), "pending": True,
                     "error": False}
            _write(path, prior)
            env = {k: v for k, v in os.environ.items() if not k.startswith("COV_CORE_")}
            home = os.path.expanduser("~")
            subprocess.Popen([sys.executable, executable, "__update-check"], cwd=home if os.path.isdir(home) else "/",
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, close_fds=True, env=env)
    except (OSError, subprocess.SubprocessError):
        pass
