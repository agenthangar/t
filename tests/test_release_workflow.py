"""Exercise release planning against real Git history and a simulated GitHub host."""

import copy
import gzip
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("release_workflow", ROOT / "scripts/release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)


def record(tag, sha, *, draft=False, prerelease=False, body=""):
    return dict(tag_name=tag, target_commitish=sha, draft=draft,
                prerelease=prerelease, body=body, assets=[])


@pytest.fixture
def host(tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.chdir(source)

    def git(*args):
        return subprocess.check_output(["git", *args], text=True).strip()

    git("init", "-q")
    git("config", "user.name", "Release Test")
    git("config", "user.email", "release@example.invalid")
    git("remote", "add", "origin", str(source))

    sequence = []

    def advance():
        sequence.append(1)
        git("commit", "--allow-empty", "-qm", f"A routine change {len(sequence)}")
        return git("rev-parse", "HEAD")

    base = advance()
    git("tag", "v0.6.0")
    head = advance()
    state = dict(base=base, head=head, items=[record("v0.6.0", base)],
                 ci="success", status="completed", calls=[], assets={}, git=git, advance=advance)
    monkeypatch.setattr(release, "releases", lambda repo: copy.deepcopy(state["items"]))

    def api(repo, path):
        if path.startswith("actions/workflows/ci.yml/runs?"):
            sha = path.split("head_sha=")[1].split("&")[0]
            if state["ci"] is None:
                return {"workflow_runs": []}
            return {"workflow_runs": [dict(head_sha=sha, event="push",
                                          status=state["status"], conclusion=state["ci"])]}
        if path.startswith("commits/"):
            return {"sha": git("rev-parse", path.removeprefix("commits/"))}
        raise AssertionError(path)

    original_run = release.run

    def run(*args):
        if args[0] != "gh":
            return original_run(*args)
        state["calls"].append(args)
        if args[1] == "api":
            assert args[2:5] == ("--method", "POST", "repos/test/repo/releases")
            fields = dict(args[i + 1].split("=", 1) for i, value in enumerate(args)
                          if value in ("-f", "-F"))
            assert fields["draft"] == "true"
            item = record(fields["tag_name"], fields["target_commitish"],
                          draft=True, body=fields["body"])
            assert not any(r["tag_name"] == item["tag_name"] for r in state["items"])
            state["items"].append(item)
            return json.dumps(item)
        assert args[1] == "release"
        action, tag = args[2:4]
        item = next((r for r in state["items"] if r["tag_name"] == tag), None)
        if action == "upload":
            for filename in args[6:]:
                path = Path(filename)
                assert (tag, path.name) not in state["assets"]
                state["assets"][tag, path.name] = path.read_bytes()
                item["assets"].append({"name": path.name})
        elif action == "download":
            name = args[args.index("--pattern") + 1]
            destination = Path(args[args.index("--dir") + 1])
            (destination / name).write_bytes(state["assets"][tag, name])
        elif action == "edit":
            assert {a["name"] for a in item["assets"]} == {"t.tar.gz", "SHA256SUMS"}
            if not git("tag", "--list", tag):
                git("tag", tag, item["target_commitish"])
            item["draft"] = False
        else:
            raise AssertionError(args)
        return ""

    monkeypatch.setattr(release, "api", api)
    monkeypatch.setattr(release, "run", run)
    return state


def actions(host):
    return ["create" if c[1] == "api" else c[2] for c in host["calls"]]


@pytest.mark.parametrize("old,new", [("v0.6.0", "v0.6.1"), ("v0.1.9", "v0.1.10"),
                                    ("v0.1.99", "v0.1.100"), ("v1.2.999", "v1.2.1000")])
def test_patch_components_are_integers(host, old, new):
    host["git"]("tag", "-f", old, host["base"])
    host["items"] = [record(old, host["base"])]
    assert release.plan("test/repo") == dict(tag=new, sha=host["head"], previous=old, automatic="true")


def test_highest_stable_is_numeric_and_ignores_drafts_prereleases(host):
    host["git"]("tag", "v0.6.10", host["base"])
    host["items"] += [record("v0.6.9", host["base"]), record("v0.6.10", host["base"]),
                       record("v0.7.0", host["head"], draft=True),
                       record("v0.8.0", host["head"], prerelease=True)]
    assert release.plan("test/repo")["tag"] == "v0.6.11"


def test_no_commits_means_no_release(host):
    host["git"]("checkout", "-q", host["base"])
    assert release.plan("test/repo") == {}
    assert host["calls"] == []


@pytest.mark.parametrize("conclusion", [None, "failure", "cancelled", "skipped"])
def test_no_successful_ci_means_no_release(host, conclusion):
    host["ci"] = conclusion
    assert release.plan("test/repo") == {}
    assert host["calls"] == []


def test_pending_ci_is_not_publishable(host):
    host["status"] = "in_progress"
    assert release.plan("test/repo") == {}


def test_explicit_published_tag_selects_its_exact_commit(host):
    assert release.plan("test/repo", "v0.6.0") == dict(
        tag="v0.6.0", sha=host["base"], previous="", automatic="false")


@pytest.mark.parametrize("tag", ["v0.6.1", "main", "v0.01.0", "v0.7.0-rc1"])
def test_explicit_tag_must_be_a_published_stable_version(host, tag):
    with pytest.raises(ValueError):
        release.plan("test/repo", tag)


def test_resume_draft_at_original_commit_when_main_advances(host):
    host["items"].append(record("v0.6.1", host["head"], draft=True, body=release.marker(host["head"])))
    host["advance"]()
    assert release.plan("test/repo")["sha"] == host["head"]


def test_do_not_take_over_unrelated_draft_or_bare_tag(host):
    host["items"].append(record("v0.6.1", host["head"], draft=True))
    with pytest.raises(ValueError, match="reserved"):
        release.plan("test/repo")
    host["items"].pop()
    host["git"]("tag", "v0.6.1")
    with pytest.raises(ValueError, match="refusing to reuse"):
        release.plan("test/repo")


def test_refuse_divergent_release_history(host):
    host["git"]("checkout", "--orphan", "other")
    host["advance"]()
    with pytest.raises(ValueError, match="not an ancestor"):
        release.plan("test/repo")


@pytest.fixture
def artifact(tmp_path):
    directory = tmp_path / "dist"
    directory.mkdir()
    data = gzip.compress(b"deterministic runtime tar contents", mtime=0)
    (directory / "t.tar.gz").write_bytes(data)
    (directory / "SHA256SUMS").write_text(hashlib.sha256(data).hexdigest() + "  t.tar.gz\n")
    return directory


def publish(host, artifact):
    release.publish("test/repo", "v0.6.1", host["head"], "v0.6.0", True, artifact)


def test_publish_complete_draft_once_and_retry_without_mutation(host, artifact):
    publish(host, artifact)
    assert actions(host) == ["create", "upload", "edit"]
    assert host["git"]("rev-parse", "v0.6.1") == host["head"]
    assert release.plan("test/repo") == {}
    host["calls"].clear()
    publish(host, artifact)
    assert actions(host) == ["download", "download"]


def test_new_draft_need_not_appear_in_release_list_immediately(host, artifact, monkeypatch):
    before_creation = copy.deepcopy(host["items"])
    monkeypatch.setattr(release, "releases", lambda repo: copy.deepcopy(before_creation))
    publish(host, artifact)
    assert actions(host) == ["create", "upload", "edit"]
    assert not host["items"][-1]["draft"]


@pytest.mark.parametrize("existing", [[], ["t.tar.gz"], ["SHA256SUMS"], ["t.tar.gz", "SHA256SUMS"]])
def test_resume_partial_uploads_without_overwriting(host, artifact, existing):
    item = record("v0.6.1", host["head"], draft=True, body=release.marker(host["head"]))
    item["assets"] = [{"name": name} for name in existing]
    host["items"].append(item)
    for name in existing:
        host["assets"]["v0.6.1", name] = (artifact / name).read_bytes()
    publish(host, artifact)
    assert not item["draft"]
    assert len(host["assets"]) == 2
    assert "create" not in actions(host)


def test_bad_existing_archive_is_never_replaced_or_published(host, artifact):
    item = record("v0.6.1", host["head"], draft=True, body=release.marker(host["head"]))
    item["assets"] = [{"name": "t.tar.gz"}]
    host["items"].append(item)
    host["assets"]["v0.6.1", "t.tar.gz"] = gzip.compress(b"different tree")
    with pytest.raises(ValueError, match="differs"):
        publish(host, artifact)
    assert actions(host) == ["download"]
    assert item["draft"]


def test_bad_existing_checksum_blocks_publication(host, artifact):
    item = record("v0.6.1", host["head"], draft=True, body=release.marker(host["head"]))
    item["assets"] = [{"name": "SHA256SUMS"}]
    host["items"].append(item)
    host["assets"]["v0.6.1", "SHA256SUMS"] = b"0" * 64 + b"  t.tar.gz\n"
    with pytest.raises(ValueError, match="checksums"):
        publish(host, artifact)
    assert actions(host) == ["download"]


def test_new_remote_tag_cannot_be_reused_after_planning(host, artifact):
    assert release.plan("test/repo")["tag"] == "v0.6.1"
    host["git"]("tag", "v0.6.1", host["base"])
    with pytest.raises(ValueError, match="tag appeared"):
        publish(host, artifact)
    assert host["calls"] == []


def test_pending_draft_cannot_publish_over_a_different_tag(host, artifact):
    item = record("v0.6.1", host["head"], draft=True, body=release.marker(host["head"]))
    host["items"].append(item)
    host["git"]("tag", "v0.6.1", host["base"])
    with pytest.raises(ValueError, match="tag moved"):
        publish(host, artifact)
    assert item["draft"]


def test_equivalent_gzip_keeps_original_bytes_and_checksum(host, artifact):
    item = record("v0.6.1", host["head"], draft=True, body=release.marker(host["head"]))
    item["assets"] = [{"name": "t.tar.gz"}]
    host["items"].append(item)
    original = gzip.compress(gzip.decompress((artifact / "t.tar.gz").read_bytes()), mtime=123)
    host["assets"]["v0.6.1", "t.tar.gz"] = original
    publish(host, artifact)
    assert host["assets"]["v0.6.1", "t.tar.gz"] == original
    assert host["assets"]["v0.6.1", "SHA256SUMS"].decode().startswith(hashlib.sha256(original).hexdigest())


def test_publication_rechecks_ci_and_competing_release(host, artifact):
    host["ci"] = "failure"
    with pytest.raises(ValueError, match="CI no longer passes"):
        publish(host, artifact)
    host["ci"] = "success"
    host["items"].append(record("v0.7.0", host["head"]))
    with pytest.raises(ValueError, match="newer release"):
        publish(host, artifact)
    assert host["calls"] == []


def test_manual_milestone_during_upload_keeps_latest(host, artifact, monkeypatch):
    original = release.sync_assets

    def sync(*args):
        original(*args)
        host["items"].append(record("v0.7.0", host["head"]))

    monkeypatch.setattr(release, "sync_assets", sync)
    with pytest.raises(ValueError, match="during upload"):
        publish(host, artifact)
    assert "edit" not in actions(host)


def test_wrong_checkout_and_minor_bump_cannot_be_published(host, artifact):
    with pytest.raises(ValueError, match="planned release commit"):
        release.publish("test/repo", "v0.6.1", host["base"], "v0.6.0", True, artifact)
    with pytest.raises(ValueError, match="only increment the patch"):
        release.publish("test/repo", "v0.7.0", host["head"], "v0.6.0", True, artifact)
    assert host["calls"] == []
