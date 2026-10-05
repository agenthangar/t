"""Update discovery uses disposable repositories and private cache state."""

import importlib.util
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("t_updates", ROOT / "libexec" / "t_updates.py")
updates = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(updates)


@pytest.fixture(autouse=True)
def isolated_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))


def git(repo, *args):
    result = subprocess.run(["git", "-C", str(repo), *args], check=True,
                            capture_output=True, text=True)
    return result.stdout.strip()


@pytest.fixture
def checkout(tmp_path):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    source = tmp_path / "source"
    subprocess.run(["git", "clone", "-q", str(remote), str(source)], check=True)
    git(source, "checkout", "-qb", "main")
    git(source, "config", "user.name", "Tester")
    git(source, "config", "user.email", "test@example.com")
    (source / "file").write_text("one\n")
    git(source, "add", "file")
    git(source, "commit", "-qm", "one")
    git(source, "push", "-q", "origin", "main")
    return {"kind": "git", "source": str(source), "current": git(source, "rev-parse", "HEAD"),
            "remote": str(remote)}


def advance_remote(checkout, tmp_path, content="two\n"):
    other = tmp_path / "other"
    subprocess.run(["git", "clone", "-q", checkout["remote"], str(other)], check=True)
    git(other, "checkout", "-q", "main")
    git(other, "config", "user.name", "Tester")
    git(other, "config", "user.email", "test@example.com")
    (other / "file").write_text(content)
    git(other, "add", "file")
    git(other, "commit", "-qm", "next")
    git(other, "push", "-q", "origin", "main")
    return git(other, "rev-parse", "HEAD")


def test_git_only_offers_newer_fast_forward(checkout, tmp_path):
    assert updates.check(checkout)["available"] is False
    newer = advance_remote(checkout, tmp_path)
    assert updates.check(checkout, force=True)["latest"] == newer
    assert updates.cached(checkout)["available"] is True
    (Path(checkout["source"]) / "scratch").write_text("dirty")
    assert updates.cached(checkout)["available"] is False
    assert updates.check(checkout, force=True)["error"] is True


def test_git_ahead_or_diverged_and_identity_change_are_silent(checkout, tmp_path):
    advance_remote(checkout, tmp_path)
    source = Path(checkout["source"])
    (source / "file").write_text("local\n")
    git(source, "add", "file")
    git(source, "commit", "-qm", "local")
    ahead = dict(checkout, current=git(source, "rev-parse", "HEAD"))
    assert updates.check(ahead, force=True)["available"] is False
    assert updates.cached(checkout)["available"] is False
    git(source, "remote", "set-url", "origin", str(tmp_path / "different.git"))
    assert updates.check(ahead, force=True)["error"] is True


def test_release_cache_snooze_and_stale_identity(tmp_path, monkeypatch):
    installation = {"kind": "release", "source": str(tmp_path / "release"), "current": "v1.2.3"}
    calls = []
    monkeypatch.setattr(updates, "_release", lambda identity: calls.append(1) or "v1.3.0")
    assert updates.check(installation)["available"] is True
    assert updates.check(installation)["available"] is True
    assert len(calls) == 1
    updates.snooze(installation, seconds=60)
    assert updates.cached(installation)["available"] is False
    assert updates.check(installation)["available"] is True
    newer = dict(installation, current="v1.2.4")
    assert updates.cached(newer)["available"] is False
    path = updates._cache_path(updates._identity(installation))
    path.write_text('{"identity":false}')
    assert updates.cached(installation)["available"] is False
    assert path.parent.stat().st_mode & 0o077 == 0


def test_skip_version_survives_refresh_and_new_release_reoffers(tmp_path, monkeypatch):
    installation = {"kind": "release", "source": str(tmp_path / "release"), "current": "v1.2.3"}
    latest = ["v1.2.4"]
    monkeypatch.setattr(updates, "_release", lambda identity: latest[0])
    assert updates.check(installation)["latest"] == "v1.2.4"
    updates.skip_version(installation, "v1.2.4")
    assert not updates.cached(installation)["available"]
    assert updates.check(installation, force=True)["available"]  # Explicit checks still report it.
    assert not updates.cached(installation)["available"]
    latest[0] = "v1.2.5"
    assert updates.check(installation, force=True)["latest"] == "v1.2.5"
    assert updates.cached(installation)["available"]
    updates.skip_version(installation, "v1.2.4")  # Stale prompt cannot hide a new version.
    assert updates.cached(installation)["available"]
    updates.skip_version(installation, "v1.2.5")
    updates.skip_version(installation, "v1.2.4")  # Nor undo a newer prompt's choice.
    assert not updates.cached(installation)["available"]


def test_skip_version_survives_pending_cache_refresh(tmp_path, monkeypatch):
    installation = {"kind": "release", "source": str(tmp_path / "release"), "current": "v1.2.3"}
    monkeypatch.setattr(updates, "_release", lambda identity: "v1.2.4")
    updates.check(installation)
    updates.skip_version(installation, "v1.2.4")
    path = updates._cache_path(updates._identity(installation))
    data = json.loads(path.read_text())
    data["expires_at"] = 0
    path.write_text(json.dumps(data))
    monkeypatch.setattr(updates.subprocess, "Popen", lambda *a, **kw: None)
    updates.schedule(installation, "/tmp/fake-t")
    assert updates.check(installation, force=True)["available"]
    assert not updates.cached(installation)["available"]


def test_skip_git_target_uses_full_commit_and_new_target_reoffers(checkout, monkeypatch):
    targets = ["a" * 40, "b" * 40]
    monkeypatch.setattr(updates, "_git", lambda identity: targets[0])
    monkeypatch.setattr(updates, "_git_offer_safe", lambda identity: True)
    updates.check(checkout)
    updates.skip_version(checkout, targets[0])
    assert not updates.cached(checkout)["available"]
    assert updates.check(checkout, force=True)["available"]
    monkeypatch.setattr(updates, "_git", lambda identity: targets[1])
    updates.check(checkout, force=True)
    assert updates.cached(checkout)["latest"] == targets[1]


def test_skip_choice_does_not_wait_for_busy_checker(tmp_path, monkeypatch):
    installation = {"kind": "release", "source": str(tmp_path / "release"), "current": "v1.2.3"}
    monkeypatch.setattr(updates, "_release", lambda identity: "v1.2.4")
    updates.check(installation)
    monkeypatch.setattr(updates.fcntl, "flock", lambda *a: pytest.fail("must not wait for checker lock"))
    updates.skip_version(installation, "v1.2.4")
    assert not updates.cached(installation)["available"]


def test_invalid_skip_sidecar_never_hides_release(tmp_path, monkeypatch):
    installation = {"kind": "release", "source": str(tmp_path / "release"), "current": "v1.2.3"}
    monkeypatch.setattr(updates, "_release", lambda identity: "v1.2.4")
    updates.check(installation)
    path = updates._skip_path(updates._identity(installation))
    for value in ({"identity": False, "version": "v1.2.4"},
                  {"identity": updates._identity(installation), "version": "v1.2.4-beta"},
                  {"identity": updates._identity(installation), "version": 42}):
        path.write_text(json.dumps(value))
        assert updates.cached(installation)["available"]


def test_release_network_payload_is_bounded_and_stable(tmp_path, monkeypatch):
    installation = {"kind": "release", "source": str(tmp_path), "current": "v1.2.3"}
    class Reply:
        def __init__(self, payload):
            self.payload = payload
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def read(self, limit):
            return self.payload[:limit]
    monkeypatch.setattr(updates.urllib.request, "urlopen", lambda *a, **k: Reply(b'{"tag_name":"v1.3.0","draft":false,"prerelease":false}'))
    assert updates.check(installation, force=True)["available"] is True
    monkeypatch.setattr(updates.urllib.request, "urlopen", lambda *a, **k: Reply(b'{"tag_name":"v1.3.0-beta"}'))
    assert updates.check(installation, force=True)["error"] is True
    monkeypatch.setattr(updates.urllib.request, "urlopen", lambda *a, **k: Reply(b"x" * 65537))
    assert updates.check(installation, force=True)["error"] is True


def test_brew_uses_outdated_formula_not_release(tmp_path, monkeypatch):
    installation = {"kind": "brew", "source": str(tmp_path), "current": "v1.2.3",
                    "brew": str(tmp_path / "brew"), "formula": "agenthangar/tap/t"}
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 1, json.dumps({"formulae": [{"name": "t",
            "installed_versions": ["1.2.3"], "current_version": "1.2.4"}]}), "")
    monkeypatch.setattr(updates, "_run", run)
    assert updates.check(installation)["latest"] == "v1.2.4"
    assert calls == [[installation["brew"], "outdated", "--json=v2", "--formula", installation["formula"]]]


def test_schedule_reserves_once_and_strips_coverage(tmp_path, monkeypatch):
    installation = {"kind": "release", "source": str(tmp_path), "current": "v1.2.3"}
    calls = []
    monkeypatch.setenv("COV_CORE_SOURCE", "secret")
    monkeypatch.setattr(updates.subprocess, "Popen", lambda *a, **k: calls.append((a, k)))
    updates.schedule(installation, "/tmp/fake-t")
    updates.schedule(installation, "/tmp/fake-t")
    assert len(calls) == 1
    args, kwargs = calls[0]
    assert args[0] == [sys.executable, "/tmp/fake-t", "__update-check"]
    assert "COV_CORE_SOURCE" not in kwargs["env"]
    assert kwargs["start_new_session"] is True
    assert updates.cached(installation)["checked"] is False
    monkeypatch.setattr(updates, "_release", lambda identity: "v1.2.4")
    assert updates.check(installation)["available"] is True


def test_timeout_is_silent_and_throttled(tmp_path, monkeypatch):
    installation = {"kind": "release", "source": str(tmp_path), "current": "v1.2.3"}
    calls = []
    def timeout(identity):
        calls.append(1)
        raise TimeoutError("offline")
    monkeypatch.setattr(updates, "_release", timeout)
    state = updates.check(installation)
    assert state["error"] is True and state["available"] is False
    assert updates.check(installation)["error"] is True
    assert calls == [1]


def test_concurrent_schedule_spawns_one_worker(tmp_path, monkeypatch):
    installation = {"kind": "release", "source": str(tmp_path), "current": "v1.2.3"}
    calls = []
    monkeypatch.setattr(updates.subprocess, "Popen", lambda *a, **k: calls.append(1))
    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(lambda _: updates.schedule(installation, "/tmp/fake-t"), range(20)))
    assert calls == [1]


@pytest.mark.parametrize("bad", [
    None, {"kind": "unknown", "source": "/tmp/x", "current": "v1.0.0"},
    {"kind": "release", "source": "relative", "current": "v1.0.0"},
    {"kind": "release", "source": "/tmp/x", "current": 1},
    {"kind": "release", "source": "/tmp/x", "current": "v1.0.0-beta"},
    {"kind": "git", "source": "/tmp/x", "current": "bad", "remote": "origin"},
    {"kind": "brew", "source": "/tmp/x", "current": "v1.0.0", "brew": "relative", "formula": "t"},
])
def test_bad_installation_is_inert(bad, monkeypatch):
    monkeypatch.setattr(updates.subprocess, "Popen", lambda *a, **k: pytest.fail("spawned"))
    assert updates.cached(bad)["available"] is False
    assert updates.check(bad)["checked"] is False
    updates.snooze(bad)
    updates.schedule(bad, "/tmp/t")


@pytest.mark.parametrize("change", [
    {"checked_at": "yesterday"}, {"expires_at": True}, {"available": 1},
    {"latest": 42}, {"snoozed_until": "later"}, {"pending": "yes"},
    {"error": 1}, {"latest": "v1.2.4-beta"}, {"expires_at": float("inf")},
])
def test_malformed_cache_fails_closed(tmp_path, change):
    installation = {"kind": "release", "source": str(tmp_path), "current": "v1.2.3"}
    identity = updates._identity(installation)
    data = {"identity": identity, "checked_at": 1, "expires_at": 9999999999,
            "available": True, "latest": "v1.2.4", "snoozed_until": 0,
            "pending": False, "error": False}
    data.update(change)
    path = updates._cache_path(identity)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(data))
    assert updates.cached(installation)["available"] is False


def test_symlink_and_large_cache_fail_closed(tmp_path):
    installation = {"kind": "release", "source": str(tmp_path), "current": "v1.2.3"}
    path = updates._cache_path(updates._identity(installation))
    path.parent.mkdir(parents=True)
    target = tmp_path / "target"
    target.write_text("{}")
    path.symlink_to(target)
    assert updates.cached(installation)["available"] is False
    path.unlink()
    path.write_text("x" * 9000)
    assert updates.cached(installation)["available"] is False


def test_nonabsolute_cache_home_is_ignored(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CACHE_HOME", "relative")
    monkeypatch.setenv("HOME", str(tmp_path))
    identity = updates._identity({"kind": "release", "source": str(tmp_path), "current": "v1.2.3"})
    assert updates._cache_path(identity).is_relative_to(tmp_path / ".cache")


@pytest.mark.parametrize("payload,expected", [
    ({"tag_name": "v1.2.3", "draft": False, "prerelease": False}, ""),
    ({"tag_name": "v1.2.4", "draft": True, "prerelease": False}, None),
    ({"tag_name": "v1.2.4", "draft": False, "prerelease": True}, None),
    ({"tag_name": "invalid", "draft": False, "prerelease": False}, None),
    ([], None),
])
def test_release_response_validation(tmp_path, monkeypatch, payload, expected):
    class Reply:
        def __enter__(self):
            return self
        def __exit__(self, *_):
            return False
        def read(self, limit):
            return json.dumps(payload).encode()
    monkeypatch.setattr(updates.urllib.request, "urlopen", lambda *a, **k: Reply())
    identity = updates._identity({"kind": "release", "source": str(tmp_path), "current": "v1.2.3"})
    assert updates._release(identity) == expected


@pytest.mark.parametrize("code,payload,expected", [
    (0, {"formulae": []}, ""),
    (2, {"formulae": []}, None),
    (1, {"formulae": [{}]}, None),
    (1, {"formulae": [{"name": "other", "installed_versions": ["1.2.3"], "current_version": "1.2.4"}]}, None),
    (1, {"formulae": [{"name": "t", "installed_versions": ["1.0.0"], "current_version": "1.2.4"}]}, None),
    (1, {"formulae": [{"name": "t", "installed_versions": ["1.2.3"], "current_version": "1.2.4-beta"}]}, None),
    (1, {"formulae": [{"name": "t", "installed_versions": ["1.2.3"], "current_version": "v1.2.5"}]}, "v1.2.5"),
    (1, {"formulae": [{"name": "t", "installed_versions": ["1.2.3"], "current_version": "1.2.2"}]}, None),
    (1, {}, None),
])
def test_brew_response_validation(tmp_path, monkeypatch, code, payload, expected):
    identity = updates._identity({"kind": "brew", "source": str(tmp_path), "current": "v1.2.3",
                                  "brew": str(tmp_path / "brew"), "formula": "agenthangar/tap/t"})
    monkeypatch.setattr(updates, "_run", lambda *a, **k: subprocess.CompletedProcess(a, code, json.dumps(payload), ""))
    assert updates._brew(identity) == expected


def test_git_source_race_and_detached_head_are_silent(checkout, tmp_path, monkeypatch):
    source = Path(checkout["source"])
    git(source, "checkout", "-q", "--detach")
    assert updates.check(checkout, force=True)["error"] is True
    git(source, "checkout", "-q", "main")
    newer = advance_remote(checkout, tmp_path)
    original = updates._run
    def race(args, **kwargs):
        result = original(args, **kwargs)
        if "fetch" in args and result.returncode == 0:
            git(source, "checkout", "-q", newer)
        return result
    monkeypatch.setattr(updates, "_run", race)
    assert updates.check(checkout, force=True)["error"] is True


def test_spawn_failure_is_silent_and_throttled(tmp_path, monkeypatch):
    installation = {"kind": "release", "source": str(tmp_path), "current": "v1.2.3"}
    calls = []
    def fail(*args, **kwargs):
        calls.append(1)
        raise OSError("cannot spawn")
    monkeypatch.setattr(updates.subprocess, "Popen", fail)
    updates.schedule(installation, "/tmp/t")
    updates.schedule(installation, "/tmp/t")
    assert calls == [1]


def test_git_head_fetch_and_status_changes_fail_closed(checkout, tmp_path, monkeypatch):
    source = Path(checkout["source"])
    (source / "file").write_text("local\n")
    git(source, "add", "file")
    git(source, "commit", "-qm", "local")
    assert updates.check(checkout, force=True)["error"] is True

    current = dict(checkout, current=git(source, "rev-parse", "HEAD"))
    git(source, "remote", "set-url", "origin", str(tmp_path / "gone.git"))
    current["remote"] = str(tmp_path / "gone.git")
    assert updates.check(current, force=True)["error"] is True

    git(source, "remote", "set-url", "origin", checkout["remote"])
    current["remote"] = checkout["remote"]
    advance_remote(current, tmp_path)
    original = updates._run
    def bad_ref(args, **kwargs):
        if "refs/remotes/origin/main" in args and "rev-parse" in args:
            return subprocess.CompletedProcess(args, 1, "", "bad ref")
        return original(args, **kwargs)
    monkeypatch.setattr(updates, "_run", bad_ref)
    assert updates.check(current, force=True)["error"] is True
    monkeypatch.setattr(updates, "_run", original)

    def dirty_after_fetch(args, **kwargs):
        result = original(args, **kwargs)
        if "fetch" in args and result.returncode == 0:
            (source / "scratch").write_text("changed during check")
        return result
    monkeypatch.setattr(updates, "_run", dirty_after_fetch)
    assert updates.check(current, force=True)["error"] is True


def test_brew_multiple_rows_fail_closed(tmp_path, monkeypatch):
    identity = updates._identity({"kind": "brew", "source": str(tmp_path), "current": "v1.2.3",
                                  "brew": str(tmp_path / "brew"), "formula": "agenthangar/tap/t"})
    rows = {"formulae": [{"name": "t"}, {"name": "other"}]}
    monkeypatch.setattr(updates, "_run", lambda *a, **k: subprocess.CompletedProcess(a, 1, json.dumps(rows), ""))
    assert updates._brew(identity) is None


def test_cached_git_subprocess_failure_does_not_offer(checkout, tmp_path, monkeypatch):
    advance_remote(checkout, tmp_path)
    assert updates.check(checkout, force=True)["available"] is True
    def fail(*args, **kwargs):
        raise OSError("git missing")
    monkeypatch.setattr(updates, "_run", fail)
    assert updates.cached(checkout)["available"] is False


def test_cache_write_failures_do_not_break_cli(tmp_path, monkeypatch):
    installation = {"kind": "release", "source": str(tmp_path), "current": "v1.2.3"}
    def fail(*args, **kwargs):
        raise OSError("read only")
    monkeypatch.setattr(updates, "_write", fail)
    monkeypatch.setattr(updates, "_release", lambda identity: "v1.2.4")
    assert updates.check(installation, force=True)["available"] is False
    updates.snooze(installation)


def test_snooze_without_prior_cache(tmp_path):
    installation = {"kind": "release", "source": str(tmp_path), "current": "v1.2.3"}
    updates.snooze(installation)
    assert updates.cached(installation)["available"] is False
    assert updates._snoozed_until(updates._identity(installation)) > updates.time.time()


@pytest.mark.parametrize("change", [
    {"latest": ""}, {"pending": True}, {"error": True},
])
def test_contradictory_available_cache_fails_closed(tmp_path, change):
    installation = {"kind": "release", "source": str(tmp_path), "current": "v1.2.3"}
    identity = updates._identity(installation)
    data = {"identity": identity, "checked_at": updates.time.time(),
            "expires_at": updates.time.time() + 100, "available": True,
            "latest": "v1.2.4", "snoozed_until": 0, "pending": False, "error": False}
    data.update(change)
    path = updates._cache_path(identity)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(data))
    assert updates.cached(installation)["available"] is False


def test_snooze_does_not_wait_for_busy_checker(tmp_path, monkeypatch):
    installation = {"kind": "release", "source": str(tmp_path), "current": "v1.2.3"}
    monkeypatch.setattr(updates, "_release", lambda identity: "v1.2.4")
    assert updates.check(installation)["available"] is True
    # Snooze while discovery holds the real lock, then let it replace the cache.
    def refresh(identity):
        updates.snooze(installation)
        assert not updates.cached(installation)["available"]
        return "v1.2.4"
    monkeypatch.setattr(updates, "_release", refresh)
    assert updates.check(installation, force=True)["available"] is True
    assert not updates.cached(installation)["available"]


def test_homebrew_snooze_survives_main_update_and_pending_refresh(tmp_path, monkeypatch):
    installation = {"kind": "git", "source": str(tmp_path), "current": "a" * 40,
                    "remote": "https://github.com/agenthangar/t.git"}
    now = [1000]
    monkeypatch.setattr(updates.time, "time", lambda: now[0])
    monkeypatch.setattr(updates, "_git", lambda identity: "")
    monkeypatch.setattr(updates, "_git_offer_safe", lambda identity: True)
    gap = {"version": "v0.4.2", "count": 11}
    monkeypatch.setattr(updates, "_homebrew_gap", lambda identity: gap)
    monkeypatch.setattr(updates.subprocess, "Popen", lambda *a, **kw: None)
    updates.check(installation)
    assert updates.cached(installation)["unreleased"] == gap
    updates.snooze(installation)
    installation["current"] = "b" * 40
    gap["count"] = 12
    now[0] += 60
    updates.schedule(installation, "/tmp/fake-t")
    assert updates.check(installation)["unreleased"] == gap
    assert "unreleased" not in updates.cached(installation)
    now[0] = 1000 + updates.INTERVAL - 1
    assert "unreleased" not in updates.cached(installation)
    now[0] += 1
    assert updates.cached(installation)["unreleased"] == gap


def test_snooze_is_scoped_to_installation(tmp_path):
    installation = {"kind": "git", "source": str(tmp_path), "current": "a" * 40,
                    "remote": "https://github.com/agenthangar/t.git"}
    updates.snooze(installation)
    assert updates._snoozed_until(updates._identity(dict(installation, current="b" * 40))) > 0
    for change in ({"remote": "https://example.com/t.git"}, {"source": str(tmp_path / "other")},
                   {"kind": "release", "current": "v0.4.2"}):
        assert updates._snoozed_until(updates._identity(dict(installation, **change))) == 0


@pytest.mark.parametrize("value", [None, "tomorrow", True, float("nan"), float("inf")])
def test_invalid_snooze_timestamp_never_hides_offer(tmp_path, monkeypatch, value):
    installation = {"kind": "release", "source": str(tmp_path), "current": "v1.2.3"}
    monkeypatch.setattr(updates, "_release", lambda identity: "v1.2.4")
    updates.check(installation)
    updates.snooze(installation)
    path = updates._cache_path(updates._identity(installation)).with_suffix(".snooze.json")
    data = json.loads(path.read_text())
    data["snoozed_until"] = value
    path.write_text(json.dumps(data))
    assert updates.cached(installation)["available"]


@pytest.mark.parametrize("contents", ["[]", "{", "x" * 8193, '\ufffd'])
def test_invalid_snooze_file_is_ignored(tmp_path, contents):
    installation = {"kind": "release", "source": str(tmp_path), "current": "v1.2.3"}
    updates.snooze(installation)
    identity = updates._identity(installation)
    path = updates._cache_path(identity).with_suffix(".snooze.json")
    path.write_text(contents)
    assert updates._snoozed_until(identity) == 0


def test_snooze_symlink_is_ignored(tmp_path):
    installation = {"kind": "release", "source": str(tmp_path), "current": "v1.2.3"}
    updates.snooze(installation)
    identity = updates._identity(installation)
    path = updates._cache_path(identity).with_suffix(".snooze.json")
    target = path.with_suffix(".saved")
    path.rename(target)
    path.symlink_to(target)
    assert updates._snoozed_until(identity) == 0


def test_legacy_cache_snooze_still_hides_offer(tmp_path, monkeypatch):
    installation = {"kind": "release", "source": str(tmp_path), "current": "v1.2.3"}
    monkeypatch.setattr(updates, "_release", lambda identity: "v1.2.4")
    updates.check(installation)
    path = updates._cache_path(updates._identity(installation))
    data = json.loads(path.read_text())
    data["snoozed_until"] = updates.time.time() + 60
    path.write_text(json.dumps(data))
    assert not updates.cached(installation)["available"]


def gap_responses(monkeypatch, formula, comparison):
    calls = []
    replies = iter([formula, comparison])
    class Reply:
        def __init__(self, data):
            self.data = data
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, size):
            return self.data
    def urlopen(request, **kwargs):
        calls.append(request.full_url)
        return Reply(next(replies))
    monkeypatch.setattr(updates.urllib.request, "urlopen", urlopen)
    return calls


@pytest.mark.parametrize("formula,comparison,expected", [
    (b'url "https://github.com/agenthangar/t/releases/download/v0.4.0/t.tar.gz"',
     b'{"status":"ahead","ahead_by":3}', {"version": "v0.4.0", "count": 3}),
    (b'x' * 65537, b'', None),
    (b'url "https://example.com/v0.4.0/t.tar.gz"', b'', None),
    (b'url "https://github.com/agenthangar/t/releases/download/v01.4.0/t.tar.gz"', b'', None),
    (b'url "https://github.com/agenthangar/t/releases/download/v0.4.0/t.tar.gz"', b'x' * 65537, None),
    (b'url "https://github.com/agenthangar/t/releases/download/v0.4.0/t.tar.gz"', b'[]', None),
    (b'url "https://github.com/agenthangar/t/releases/download/v0.4.0/t.tar.gz"',
     b'{"status":"identical","ahead_by":0}', None),
    (b'url "https://github.com/agenthangar/t/releases/download/v0.4.0/t.tar.gz"',
     b'{"status":"diverged","ahead_by":3}', None),
    (b'url "https://github.com/agenthangar/t/releases/download/v0.4.0/t.tar.gz"',
     b'{"status":"ahead","ahead_by":true}', None),
])
def test_homebrew_gap_reads_actual_tap_version(monkeypatch, formula, comparison, expected):
    calls = gap_responses(monkeypatch, formula, comparison)
    identity = {"kind": "git", "remote": "git@github.com:agenthangar/t.git"}
    assert updates._homebrew_gap(identity) == expected
    assert calls[0] == updates.TAP_FORMULA
    if expected:
        assert calls[1].endswith('/compare/v0.4.0...main?per_page=1&page=2')


def test_unreleased_state_cache_snooze_and_network_failure(checkout, monkeypatch):
    installation = dict(checkout, remote="https://github.com/agenthangar/t.git")
    monkeypatch.setattr(updates, "_git", lambda identity: "")
    monkeypatch.setattr(updates, "_git_offer_safe", lambda identity: True)
    gap = {"version": "v0.4.0", "count": 3}
    monkeypatch.setattr(updates, "_homebrew_gap", lambda identity: gap)
    assert updates.check(installation, force=True)["unreleased"] == gap
    assert updates.cached(installation)["unreleased"] == gap
    updates.snooze(installation)
    assert "unreleased" not in updates.cached(installation)
    assert updates.check(installation)["unreleased"] == gap
    path = updates._cache_path(updates._identity(installation))
    data = json.loads(path.read_text())
    data["unreleased"]["version"] = "bad\nversion"
    path.write_text(json.dumps(data))
    assert updates._read(path, updates._identity(installation)) == {}
    def fail(identity):
        raise OSError("network unavailable")
    monkeypatch.setattr(updates, "_homebrew_gap", fail)
    state = updates.check(installation, force=True)
    assert state["checked"] and not state["error"] and "unreleased" not in state


def test_skipped_git_update_does_not_turn_into_homebrew_reminder(checkout, monkeypatch):
    installation = dict(checkout, remote="https://github.com/agenthangar/t.git")
    target = "a" * 40
    monkeypatch.setattr(updates, "_git", lambda identity: target)
    monkeypatch.setattr(updates, "_git_offer_safe", lambda identity: True)
    monkeypatch.setattr(updates, "_homebrew_gap", lambda identity: {"version": "v0.4.0", "count": 3})
    updates.check(installation)
    updates.skip_version(installation, target)
    state = updates.cached(installation)
    assert not state["available"]
    assert "unreleased" not in state


def test_homebrew_gap_skips_other_installations(checkout, monkeypatch):
    monkeypatch.setattr(updates.urllib.request, "urlopen", lambda *a, **k: pytest.fail("no network"))
    assert updates._homebrew_gap(checkout) is None
    assert updates._homebrew_gap({"kind": "brew"}) is None
