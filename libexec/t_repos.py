"""Resolve existing repository selectors without changing registrations."""
import os
import re
import subprocess
from urllib.parse import urlsplit


def origin_name(url):
    """Extract a case-sensitive safe name from common network Git URL forms."""
    if not url or any(ord(char) < 33 or ord(char) == 127 for char in url):
        return None
    try:
        if "://" in url:
            parsed = urlsplit(url)
            if (parsed.scheme not in ("https", "http", "ssh", "git")
                    or not parsed.hostname or parsed.query or parsed.fragment):
                return None
            parsed.port  # Validate a malformed port before accepting the URL.
            path = parsed.path
        else:
            match = re.fullmatch(r"(?:[^/@:]+@)?[^/@:]+:(.+)", url)
            if not match:
                return None
            path = match[1]
    except ValueError:
        return None
    path = path.removesuffix("/")
    name = path.rsplit("/", 1)[-1].removesuffix(".git")
    return name if re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", name) else None


def local_origin_name(directory):
    """Read only this checkout's local origin; no discovery, includes or network."""
    if not os.path.exists(os.path.join(directory, ".git")):
        return None
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_TERMINAL_PROMPT="0")
    try:
        result = subprocess.run(
            ["git", "--git-dir", os.path.join(directory, ".git"), "config", "--local", "--no-includes", "--get",
             "remote.origin.url"],
            env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, timeout=2)
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return None
    return origin_name(result.stdout.removesuffix("\n")) if result.returncode == 0 else None


def repository_names(repos):
    """Build a fresh candidate map with one metadata read per real checkout."""
    names, origins = {}, {}
    for alias, directory in repos.items():
        real = os.path.realpath(directory)
        if real not in origins:
            origins[real] = local_origin_name(real)
        for name in {os.path.basename(os.path.normpath(directory)), origins[real]} - {None, ""}:
            names.setdefault(name, []).append(alias)
    return names


def resolve_repo(repos, selector, *, required=False):
    """Return a registered alias, or None for an unknown optional selector.

    Exact aliases keep their own policy. Basenames and origin names identify only one
    real checkout; aliases of that checkout use a stable shortest/lexical choice.
    Ambiguity is always an error, including in mixed repository/session grammars.
    """
    if selector in repos:
        return selector
    matches = repository_names(repos).get(selector, []) if selector else []
    if len({os.path.realpath(repos[alias]) for alias in matches}) > 1:
        raise ValueError(f"ambiguous repository {selector!r}; use an explicit alias: "
                         + ", ".join(sorted(matches)))
    if matches:
        return min(matches, key=lambda alias: (len(alias), alias))
    if required:
        raise ValueError(f"unknown repository {selector!r}; registered aliases: "
                         + (", ".join(sorted(repos)) or "(none registered)"))
    return None


if __name__ == "__main__":
    import sys
    value = local_origin_name(sys.argv[1])
    if value:
        print(value)
