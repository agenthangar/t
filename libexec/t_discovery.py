"""Bounded, read-only setup discovery and explicitly selected GitHub clones.

These helpers never prompt, register hosts, run SSH, or alter existing checkouts.
Only ``clone_repo`` writes, after the caller has reviewed its exact destination.
"""

import ipaddress
import json
import os
import runpy
from pathlib import Path
import re
import shutil
import subprocess
from urllib.parse import urlsplit

_spinner = runpy.run_path(str(Path(__file__).with_name("t_progress.py")))["spinner"]


_OWNER = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")
_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}\Z")
_HOST = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9.-]{0,252})\Z")
_SKIP_DIRS = {"node_modules", "vendor", "venv", "__pycache__"}


@_spinner('Discovering repositories and tools')
def _run(args, timeout=15):
    env = os.environ.copy()
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
        env.pop(key, None)
    env.update(GH_PROMPT_DISABLED="1", GH_NO_UPDATE_NOTIFIER="1", GH_TELEMETRY="disabled",
               GIT_TERMINAL_PROMPT="0", GCM_INTERACTIVE="never", TAILSCALE_BE_CLI="1")
    try:
        result = subprocess.run(args, stdin=subprocess.DEVNULL, capture_output=True,
                                text=True, timeout=timeout, env=env)
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return None
    return result if result.returncode == 0 else None


def _full_name(value):
    if not isinstance(value, str) or value.count("/") != 1:
        return False
    owner, name = value.split("/")
    return bool(_OWNER.fullmatch(owner) and _NAME.fullmatch(name)
                and name not in (".", ".."))


def github_identity(remote):
    """Normalize a GitHub origin without retaining credentials or arbitrary URLs."""
    if not isinstance(remote, str):
        return ""
    remote = remote.strip()
    if remote.startswith("git@github.com:"):
        name = remote[len("git@github.com:"):]
    else:
        try:
            parsed = urlsplit(remote)
            if (parsed.scheme not in ("https", "ssh", "git")
                    or parsed.hostname != "github.com" or parsed.query or parsed.fragment):
                return ""
            name = parsed.path.lstrip("/")
        except ValueError:
            return ""
    name = name.removesuffix("/").removesuffix(".git")
    return name.lower() if _full_name(name) else ""


def _checkout(path):
    """Return primary checkout metadata, excluding linked worktrees/submodules."""
    path = Path(path)
    gitdir = path / ".git"
    if (any(not c.isprintable() for c in str(path)) or path.is_symlink()
            or gitdir.is_symlink() or not gitdir.is_dir()):
        return None
    top = _run(["git", "-C", str(path), "rev-parse", "--show-toplevel"])
    if top is None or os.path.realpath(top.stdout.strip()) != str(path.resolve()):
        return None
    origin = _run(["git", "-C", str(path), "config", "--get", "remote.origin.url"])
    return {"path": str(path.resolve()), "name": path.name,
            "identity": github_identity(origin.stdout) if origin else ""}


@_spinner('Discovering local repositories')
def discover_local(roots, excluded=(), max_depth=3, max_directories=5000):
    """Find primary checkouts once, without traversing hidden/generated trees.

    Explicit roots may be symlinks. Descendant symlinks are not followed, and
    exclusions cover the entire subtree (including the session-worktree root).
    """
    excluded = [Path(p).expanduser().resolve() for p in excluded]
    found, visited = {}, set()
    for raw in roots:
        root = Path(raw).expanduser().resolve()
        for current, dirs, _files in os.walk(root, followlinks=False):
            path = Path(current)
            real = path.resolve()
            if (real in visited or any(real == p or p in real.parents for p in excluded)):
                dirs[:] = []
                continue
            if len(visited) >= max_directories:
                return sorted(found.values(), key=lambda row: row["path"])
            visited.add(real)
            if os.path.lexists(path / ".git"):
                dirs[:] = []
                row = _checkout(path)
                if row is not None:
                    found[row["path"]] = row
                continue
            depth = len(path.relative_to(root).parts)
            dirs[:] = (sorted(d for d in dirs if not d.startswith(".")
                              and d not in _SKIP_DIRS and not (path / d).is_symlink())
                       if depth < max_depth else [])
    return sorted(found.values(), key=lambda row: row["path"])


def _github_pages(endpoint):
    result = _run(["gh", "api", "--hostname", "github.com", endpoint,
                   "--paginate", "--slurp"], timeout=60)
    if result is None:
        return [], ["GitHub discovery is unavailable; check `gh auth status` and organization access."]
    try:
        pages = json.loads(result.stdout)
        if not isinstance(pages, list) or any(not isinstance(page, list) for page in pages):
            raise ValueError("expected paginated arrays")
        return [row for page in pages for row in page if isinstance(row, dict)], []
    except (ValueError, TypeError):
        return [], ["GitHub returned an invalid discovery response; try again later."]


def github_orgs():
    """Membership organizations visible to the authenticated github.com account."""
    rows, warnings = _github_pages("user/orgs?per_page=100")
    unique = {}
    for row in rows:
        name = row.get("login")
        if isinstance(name, str) and _OWNER.fullmatch(name):
            unique.setdefault(name.lower(), name)
    return sorted(unique.values(), key=str.lower), warnings


def github_auth_state():
    """Return missing, authenticated, or unavailable without exposing tokens.

    Unavailable covers absent/expired credentials and offline checks; the wizard
    may offer login, but can always continue with local repositories instead.
    """
    if shutil.which("gh") is None:
        return "missing"
    # Avoid auth status: recent gh versions may refresh credentials during that
    # check. GET /user works across versions and tests the active credentials.
    return ("authenticated" if _run(["gh", "api", "--hostname", "github.com", "user"])
            else "unavailable")


def github_install_command():
    """Offer Homebrew's documented gh installation; other platforms use the docs.

    Returning a command is read-only. The caller must show it for explicit review
    before running it with the user's terminal.
    """
    brew = shutil.which("brew")
    return [brew, "install", "gh"] if brew else None


def _single_line(value):
    return " ".join("".join(c for c in str(value or "") if c.isprintable() or c.isspace()).split())


def github_repos(org):
    """List an organization's visible repositories; retain fork/archive labels."""
    if not isinstance(org, str) or not _OWNER.fullmatch(org):
        return [], ["Invalid GitHub organization name."]
    rows, warnings = _github_pages(f"orgs/{org}/repos?per_page=100&type=all&sort=full_name")
    unique = {}
    for row in rows:
        name = row.get("full_name")
        if not _full_name(name) or name.split("/")[0].lower() != org.lower():
            continue
        unique.setdefault(name.lower(), {
            "full_name": name, "name": name.split("/")[1],
            "description": _single_line(row.get("description")),
            "archived": row.get("archived") is True,
            "fork": row.get("fork") is True,
            "private": row.get("private") is True,
        })
    return sorted(unique.values(), key=lambda row: row["full_name"].lower()), warnings


def tailscale_hosts():
    """Online Unix peers are candidates, not evidence that SSH is authorized."""
    # The macOS App Store variant bundles the CLI in its app executable.
    # https://tailscale.com/docs/reference/tailscale-cli?tab=macos
    bundled = "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    executable = shutil.which("tailscale") or (bundled if os.access(bundled, os.X_OK) else "tailscale")
    result = _run([executable, "status", "--json"])
    if result is None:
        return [], ["Tailscale discovery is unavailable; install or connect Tailscale to discover hosts."]
    try:
        status = json.loads(result.stdout)
        if not isinstance(status, dict):
            raise ValueError("expected object")
        if status.get("BackendState") != "Running":
            return [], ["Tailscale is not connected; hosts can be added later."]
        peers = status.get("Peer") or {}
        if not isinstance(peers, dict):
            raise ValueError("expected peers")
    except (ValueError, TypeError):
        return [], ["Tailscale returned an invalid discovery response; try again later."]
    own = status.get("Self") or {}
    own_id = own.get("ID") if isinstance(own, dict) else None
    unique = {}
    for peer in peers.values():
        if (not isinstance(peer, dict) or peer.get("Online") is not True
                or (own_id is not None and peer.get("ID") == own_id)
                or str(peer.get("OS", "")).lower() not in
                {"linux", "macos", "darwin", "freebsd", "openbsd", "netbsd"}):
            continue
        target = str(peer.get("DNSName") or "").rstrip(".")
        if not _HOST.fullmatch(target):
            target = ""
            for address in peer.get("TailscaleIPs", []) if isinstance(peer.get("TailscaleIPs"), list) else []:
                if not isinstance(address, str):
                    continue
                try:
                    target = str(ipaddress.ip_address(address))
                    break
                except ValueError:
                    pass
        if not target:
            continue
        alias = str(peer.get("HostName") or target.split(".")[0])
        alias = re.sub(r"[^A-Za-z0-9_-]", "-", alias).strip("-") or "host"
        unique.setdefault(target.lower(), {"alias": alias, "target": target,
                                           "online": True, "os": str(peer["OS"])})
    return sorted(unique.values(), key=lambda row: row["target"]), []


def clone_destination(full_name, root=None):
    if not _full_name(full_name):
        raise ValueError("Invalid GitHub repository name.")
    root = Path(root or Path.home() / "code").expanduser().absolute()
    return str(root / full_name.replace("/", "-"))


def clone_repo(full_name, path):
    """Clone a reviewed repository without replacing or cleaning any checkout.

    Reserve the exact directory before invoking gh. Failed partial checkouts stay
    on disk for inspection; only an empty reservation may be removed on failure.
    """
    if not _full_name(full_name):
        return False, "Invalid GitHub repository name."
    path = Path(path).expanduser().absolute()
    if any(not c.isprintable() for c in str(path)):
        return False, "Invalid clone destination."
    if os.path.lexists(path):
        existing = _checkout(path)
        if existing and existing["identity"] == full_name.lower():
            return True, f"Using existing checkout at {path}"
        return False, f"Destination already exists; left unchanged: {path}"
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.mkdir()
    except OSError:
        return False, f"Could not reserve clone destination; left unchanged: {path}"
    result = _run(["gh", "repo", "clone", f"https://github.com/{full_name}", str(path)], timeout=360)
    existing = _checkout(path)
    if result is not None and existing and existing["identity"] == full_name.lower():
        return True, f"Cloned {full_name} to {path}"
    try:
        path.rmdir()
    except OSError:
        pass
    return False, f"Could not finish cloning {full_name}; inspect {path} before retrying."
