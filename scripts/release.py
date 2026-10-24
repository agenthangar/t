#!/usr/bin/env python3
"""Plan patch releases and publish verified assets. Requires authenticated gh."""

import argparse
import gzip
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile


VERSION = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\Z")
SHA = re.compile(r"[0-9a-f]{40}\Z")


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def api(repo, path):
    return json.loads(run("gh", "api", f"repos/{repo}/{path}"))


def releases(repo):
    pages = json.loads(run("gh", "api", "--paginate", "--slurp",
                           f"repos/{repo}/releases?per_page=100"))
    return [release for page in pages for release in page]


def version(tag):
    match = VERSION.fullmatch(tag)
    if not match:
        raise ValueError(f"Expected a vX.Y.Z release tag, got {tag!r}")
    return tuple(map(int, match.groups()))


def latest_stable(items):
    stable = [r for r in items if not r["draft"] and not r["prerelease"]
              and VERSION.fullmatch(r["tag_name"])]
    if not stable:
        raise ValueError("Create the initial stable release manually first")
    return max(stable, key=lambda r: version(r["tag_name"]))


def commit(ref):
    return run("git", "rev-parse", "--verify", f"{ref}^{{commit}}")


def ancestor(base, head):
    result = subprocess.run(["git", "merge-base", "--is-ancestor", base, head])
    if result.returncode not in (0, 1):
        result.check_returncode()
    return result.returncode == 0


def marker(sha):
    return f"<!-- t automatic patch release: {sha} -->"


def draft_target(release):
    sha = release["target_commitish"]
    if (not release["draft"] or release["prerelease"] or not SHA.fullmatch(sha)
            or marker(sha) not in (release.get("body") or "")):
        raise ValueError("Next patch version is already reserved by another release")
    return sha


def ci_passed(repo, sha):
    data = api(repo, f"actions/workflows/ci.yml/runs?event=push&head_sha={sha}&per_page=1")
    runs = data["workflow_runs"]
    return bool(runs and runs[0]["head_sha"] == sha and runs[0]["event"] == "push"
                and runs[0]["status"] == "completed" and runs[0]["conclusion"] == "success")


def plan(repo, tag=""):
    items = releases(repo)
    if tag:
        version(tag)
        release = next((r for r in items if r["tag_name"] == tag), None)
        if not release or release["draft"] or release["prerelease"]:
            raise ValueError("An explicit tag must name an existing published stable release")
        return dict(tag=tag, sha=commit(f"refs/tags/{tag}"), previous="", automatic="false")

    latest = latest_stable(items)
    previous = latest["tag_name"]
    base, head = commit(f"refs/tags/{previous}"), commit("HEAD")
    if not ancestor(base, head):
        raise ValueError("Latest stable release is not an ancestor of main")
    if base == head:
        print("No unreleased commits on main; no release needed.")
        return {}
    major, minor, patch = version(previous)
    tag = f"v{major}.{minor}.{patch + 1}"
    pending = next((r for r in items if r["tag_name"] == tag), None)
    if pending:
        # Finish an interrupted draft before releasing newer commits from main.
        head = draft_target(pending)
        if head == base or not ancestor(base, head) or not ancestor(head, commit("HEAD")):
            raise ValueError("Pending draft is outside the unreleased main history")
    else:
        existing = run("git", "tag", "--list", tag)
        if existing:
            raise ValueError(f"{tag} already exists without a resumable draft; refusing to reuse it")
    if not ci_passed(repo, head):
        print(f"CI has not passed for {head}; leaving it for a later run.")
        return {}
    return dict(tag=tag, sha=head, previous=previous, automatic="true")


def sync_assets(repo, tag, release, directory):
    """Fill missing assets on retries; never replace existing asset bytes."""
    names = {a["name"] for a in release["assets"]}
    archive = (directory / "t.tar.gz").read_bytes()
    with tempfile.TemporaryDirectory() as temporary:
        downloaded = Path(temporary)
        for name in ("t.tar.gz", "SHA256SUMS"):
            if name in names:
                run("gh", "release", "download", tag, "--repo", repo,
                    "--pattern", name, "--dir", str(downloaded))
        if "t.tar.gz" in names:
            published = (downloaded / "t.tar.gz").read_bytes()
            if gzip.decompress(published) != gzip.decompress(archive):
                raise ValueError("Published archive differs from the tagged Git tree")
            archive = published
        checksums = hashlib.sha256(archive).hexdigest() + "  t.tar.gz\n"
        if "SHA256SUMS" in names:
            if (downloaded / "SHA256SUMS").read_text().strip() != checksums.strip():
                raise ValueError("Existing checksums do not match the release archive")
        else:
            (downloaded / "SHA256SUMS").write_text(checksums)
        missing = []
        if "t.tar.gz" not in names:
            missing.append(str(directory / "t.tar.gz"))
        if "SHA256SUMS" not in names:
            missing.append(str(downloaded / "SHA256SUMS"))
        if missing:
            run("gh", "release", "upload", tag, "--repo", repo, *missing)


def publish(repo, tag, sha, previous, automatic, directory):
    version(tag)
    if not SHA.fullmatch(sha) or commit("HEAD") != sha:
        raise ValueError("The archive checkout is not the planned release commit")
    items = releases(repo)
    release = next((r for r in items if r["tag_name"] == tag), None)
    if automatic:
        version(previous)
        if version(tag) != (*version(previous)[:2], version(previous)[2] + 1):
            raise ValueError("Automatic releases may only increment the patch number")
        latest = latest_stable(items)["tag_name"]
        if latest != previous and not (latest == tag and release and not release["draft"]):
            raise ValueError("A newer release appeared after planning; retry from main")
        if not ci_passed(repo, sha):
            raise ValueError("CI no longer passes for the planned commit")
        if release and release["draft"] and draft_target(release) != sha:
            raise ValueError("Pending draft targets a different commit")
        if not release:
            # Check live remote refs too: a manual tag may have appeared since checkout.
            if run("git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}"):
                raise ValueError("Release tag appeared after planning; refusing to reuse it")
            changes = run("git", "log", "--format=- %s (%h)", f"refs/tags/{previous}..{sha}")
            notes = (f"{marker(sha)}\n\nPatch release from tested main commit `{sha}`.\n\n"
                     f"{changes}\n\nFull changes: https://github.com/{repo}/compare/{previous}...{tag}\n")
            with tempfile.TemporaryDirectory() as temporary:
                path = Path(temporary) / "notes.md"
                path.write_text(notes)
                run("gh", "release", "create", tag, "--repo", repo, "--draft",
                    "--target", sha, "--title", f"t {tag}", "--notes-file", str(path))
            release = next(r for r in releases(repo) if r["tag_name"] == tag)
    if not release or release["prerelease"]:
        raise ValueError("Expected a stable release")
    if release["draft"]:
        if not automatic or draft_target(release) != sha:
            raise ValueError("Refusing to publish an unrelated draft")
    elif api(repo, f"commits/{tag}")["sha"] != sha:
        raise ValueError("Published tag targets a different commit")
    sync_assets(repo, tag, release, Path(directory))
    if automatic and release["draft"]:
        if (run("git", "ls-remote", "--tags", "origin", f"refs/tags/{tag}")
                and api(repo, f"commits/{tag}")["sha"] != sha):
            raise ValueError("Release tag moved to a different commit; refusing to publish")
        run("gh", "release", "edit", tag, "--repo", repo, "--draft=false", "--latest")
    if api(repo, f"commits/{tag}")["sha"] != sha:
        raise ValueError("Published tag does not match the planned commit")
    print(f"Verified release: https://github.com/{repo}/releases/tag/{tag}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "publish"))
    parser.add_argument("--repo", default=os.environ.get("GITHUB_REPOSITORY", "agenthangar/t"))
    parser.add_argument("--tag", default="")
    parser.add_argument("--sha", default="")
    parser.add_argument("--previous", default="")
    parser.add_argument("--automatic", action="store_true")
    parser.add_argument("--directory", default="dist")
    args = parser.parse_args()
    if args.action == "plan":
        values = plan(args.repo, args.tag)
        print(json.dumps(values, sort_keys=True))
        if os.environ.get("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a") as output:
                for key, value in values.items():
                    output.write(f"{key}={value}\n")
    else:
        publish(args.repo, args.tag, args.sha, args.previous, args.automatic, args.directory)


if __name__ == "__main__":
    main()
