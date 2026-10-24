"""Setup discovery uses private fixtures; it never contacts user accounts/hosts."""
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from conftest import REPO_ROOT, load_script


@pytest.fixture
def discovery(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    return load_script(REPO_ROOT / "libexec/t_discovery.py", "discovery_test")


def reply(data):
    return SimpleNamespace(returncode=0, stdout=json.dumps(data))


def checkout(path, origin="https://github.com/acme/api.git"):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True)
    if origin:
        subprocess.run(["git", "-C", str(path), "remote", "add", "origin", origin], check=True)
    return path


def test_local_discovery_is_bounded_deduplicated_and_excludes_worktrees(discovery, tmp_path):
    code = tmp_path / "code"
    api = checkout(code / "api")
    checkout(api / "nested")
    plain = checkout(code / "plain", origin=None)
    checkout(code / "bad\nname")
    checkout(code / ".hidden" / "hidden")
    checkout(code / "node_modules" / "dependency")
    checkout(code / "one" / "two" / "three" / "deep")
    excluded = checkout(code / "sessions" / "work")
    wt = code / "linked"
    wt.mkdir()
    (wt / ".git").write_text("gitdir: ../api/.git/worktrees/linked\n")
    (code / "alias").symlink_to(api, target_is_directory=True)
    root_link = tmp_path / "code-link"
    root_link.symlink_to(code, target_is_directory=True)
    rows = discovery.discover_local([code, root_link, tmp_path / "missing"], [excluded.parent])
    assert rows == [{"path": str(api), "name": "api", "identity": "acme/api"},
                    {"path": str(plain), "name": "plain", "identity": ""}]
    assert discovery.discover_local([code], max_directories=1) == []
    assert discovery.discover_local([code], max_depth=0) == []


def test_local_rejects_broken_gitdirs_and_git_directory_symlinks(discovery, tmp_path, monkeypatch):
    root = tmp_path / "code"
    broken = root / "broken"
    (broken / ".git").mkdir(parents=True)
    primary = checkout(root / "good")
    linked = root / "symlink-git"
    linked.mkdir()
    (linked / ".git").symlink_to(primary / ".git", target_is_directory=True)
    assert discovery.discover_local([root]) == [discovery._checkout(primary)]
    monkeypatch.setattr(discovery, "_run", lambda args, **kw: SimpleNamespace(stdout=str(tmp_path)))
    assert discovery._checkout(primary) is None


@pytest.mark.parametrize("remote,expected", [
    ("git@github.com:Acme/API.git", "acme/api"),
    ("https://github.com/Acme/API.git/", "acme/api"),
    ("ssh://git@github.com/acme/api", "acme/api"),
    ("git://github.com/acme/api", "acme/api"),
    ("https://user:secret@github.com/acme/api", "acme/api"),
    ("https://github.com/evil/../../api", ""),
    ("https://github.com/acme/api?token=secret", ""),
    ("https://example.com/acme/api", ""),
    ("https://[invalid/acme/api", ""),
    ("git@github.com:acme/api;echo token", ""),
    ("acme/api", ""), (None, ""),
])
def test_origin_identity_is_safe_and_credential_free(discovery, remote, expected):
    assert discovery.github_identity(remote) == expected


def test_subprocesses_are_noninteractive_scoped_and_suppress_sensitive_errors(discovery, monkeypatch):
    calls = []
    for key in ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE"):
        monkeypatch.setenv(key, "wrong checkout")

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout="ok")

    monkeypatch.setattr(discovery.subprocess, "run", run)
    assert discovery._run(["gh", "api", "user"]).stdout == "ok"
    args, kwargs = calls[0]
    assert args == ["gh", "api", "user"]
    assert kwargs["stdin"] == subprocess.DEVNULL
    assert kwargs["capture_output"] and kwargs["timeout"] == 15
    assert kwargs["env"]["GH_PROMPT_DISABLED"] == "1"
    assert kwargs["env"]["GH_TELEMETRY"] == "disabled"
    assert "GIT_DIR" not in kwargs["env"]
    monkeypatch.setattr(discovery.subprocess, "run", lambda *a, **kw: SimpleNamespace(returncode=1))
    assert discovery._run(["gh"]) is None
    for error in (OSError("token secret"), subprocess.TimeoutExpired("gh", 15), UnicodeError("bad")):
        def fail(*args, **kwargs):
            raise error
        monkeypatch.setattr(discovery.subprocess, "run", fail)
        assert discovery._run(["gh"]) is None


def test_github_orgs_and_repos_flatten_pages_validate_and_label(discovery, monkeypatch):
    calls = []

    def run(args, **kwargs):
        calls.append((args, kwargs))
        if "user/orgs?per_page=100" in args:
            return reply([[{"login": "Zulu"}, {"login": "Acme"}, None],
                          [{"login": "acme"}, {"login": "../bad"}, {"login": 1}]])
        return reply([[{"full_name": "Acme/zeta", "archived": True, "private": True,
                        "description": "hello\nworld\x1b"}],
                      [{"full_name": "acme/api", "fork": True}, {"full_name": "acme/API"},
                       {"full_name": "other/no"}, {"full_name": "acme/../bad"}, {}]])

    monkeypatch.setattr(discovery, "_run", run)
    assert discovery.github_orgs() == (["Acme", "Zulu"], [])
    rows, warnings = discovery.github_repos("Acme")
    assert warnings == []
    assert [r["full_name"] for r in rows] == ["acme/api", "Acme/zeta"]
    assert rows[0]["fork"] and not rows[0]["archived"]
    assert rows[1] == {"full_name": "Acme/zeta", "name": "zeta", "description": "hello world",
                       "archived": True, "private": True, "fork": False}
    for args, kwargs in calls:
        assert args[:4] == ["gh", "api", "--hostname", "github.com"]
        assert args[-2:] == ["--paginate", "--slurp"]
        assert kwargs["timeout"] == 60
    assert discovery.github_repos("../not-org")[1]
    assert discovery.github_repos(None)[1]


@pytest.mark.parametrize("data", ["not-json", '{"message":"bad"}', '[{}]', '[[null],null]'])
def test_github_malformed_response_is_a_warning(discovery, monkeypatch, data):
    monkeypatch.setattr(discovery, "_run", lambda *a, **kw: SimpleNamespace(stdout=data))
    rows, warnings = discovery.github_orgs()
    assert not rows and "invalid" in warnings[0]


def test_github_unavailable_and_empty_are_distinct(discovery, monkeypatch):
    monkeypatch.setattr(discovery, "_run", lambda *a, **kw: None)
    assert discovery.github_orgs()[1]
    monkeypatch.setattr(discovery, "_run", lambda *a, **kw: reply([[]]))
    assert discovery.github_orgs() == ([], [])


def test_github_auth_check_never_refreshes_or_prints_credentials(discovery, monkeypatch, capsys):
    monkeypatch.setattr(discovery.shutil, "which", lambda tool: None)
    assert discovery.github_auth_state() == "missing"
    monkeypatch.setattr(discovery.shutil, "which", lambda tool: "/bin/gh")
    calls = []
    monkeypatch.setattr(discovery, "_run", lambda args: calls.append(args) or reply({"secret": "private"}))
    assert discovery.github_auth_state() == "authenticated"
    assert calls == [["gh", "api", "--hostname", "github.com", "user"]]
    monkeypatch.setattr(discovery, "_run", lambda args: None)
    assert discovery.github_auth_state() == "unavailable"
    assert capsys.readouterr().out == ""


def test_github_install_command_is_only_a_reviewable_homebrew_plan(discovery, monkeypatch):
    monkeypatch.setattr(discovery.shutil, "which", lambda tool: "/opt/homebrew/bin/brew")
    assert discovery.github_install_command() == ["/opt/homebrew/bin/brew", "install", "gh"]
    monkeypatch.setattr(discovery.shutil, "which", lambda tool: None)
    assert discovery.github_install_command() is None


def test_tailscale_only_offers_online_unix_peers_without_connecting(discovery, monkeypatch):
    monkeypatch.setattr(discovery.shutil, "which", lambda _: "tailscale")
    peer = {"ID": "peer", "HostName": "work machine", "DNSName": "work.tail.ts.net.",
            "OS": "macOS", "Online": True}
    status = {"BackendState": "Running", "Self": {"ID": "self"}, "Peer": {
        "self": {**peer, "ID": "self"}, "work": peer, "duplicate": peer,
        "phone": {**peer, "OS": "ios"}, "offline": {**peer, "Online": False},
        "windows": {**peer, "OS": "windows"}, "malformed": None,
        "ip": {**peer, "HostName": "", "DNSName": "bad host", "OS": "linux",
               "TailscaleIPs": [None, 1, "bad", "100.100.1.2"]},
        "unnamed": {**peer, "HostName": "!!!", "DNSName": "other.tail.ts.net"},
        "none": {**peer, "DNSName": None, "TailscaleIPs": ["bad"]},
        "badips": {**peer, "DNSName": None, "TailscaleIPs": "bad"},
    }}
    calls = []
    monkeypatch.setattr(discovery, "_run", lambda args: calls.append(args) or reply(status))
    hosts, warnings = discovery.tailscale_hosts()
    assert warnings == []
    assert hosts == [
        {"alias": "100", "target": "100.100.1.2", "online": True, "os": "linux"},
        {"alias": "host", "target": "other.tail.ts.net", "online": True, "os": "macOS"},
        {"alias": "work-machine", "target": "work.tail.ts.net", "online": True, "os": "macOS"},
    ]
    assert calls == [["tailscale", "status", "--json"]]


@pytest.mark.parametrize("data", ["bad", "[]", '{"BackendState":"Running","Peer":[1]}'])
def test_tailscale_invalid_and_absent_is_optional(discovery, monkeypatch, data):
    monkeypatch.setattr(discovery, "_run", lambda args: SimpleNamespace(stdout=data))
    assert "invalid" in discovery.tailscale_hosts()[1][0]
    monkeypatch.setattr(discovery, "_run", lambda args: None)
    assert "unavailable" in discovery.tailscale_hosts()[1][0]
    monkeypatch.setattr(discovery, "_run", lambda args: reply({"BackendState": "NeedsLogin"}))
    assert "not connected" in discovery.tailscale_hosts()[1][0]
    monkeypatch.setattr(discovery, "_run", lambda args: reply({"BackendState": "Running", "Peer": None}))
    assert discovery.tailscale_hosts() == ([], [])


def test_clone_destination_namespaces_org_and_validates(discovery, tmp_path):
    assert discovery.clone_destination("acme/api") == str(tmp_path / "code" / "acme-api")
    assert discovery.clone_destination("other/api", tmp_path) == str(tmp_path / "other-api")
    for bad in ("../api", "acme/..", "--flag/x", "acme/a;echo", "acme/a/b", None):
        with pytest.raises(ValueError):
            discovery.clone_destination(bad)
        assert not discovery.clone_repo(bad, tmp_path / "target")[0]
    assert not (tmp_path / "target").exists()


def test_clone_reuses_only_matching_primary_checkout(discovery, tmp_path, monkeypatch):
    repo = checkout(tmp_path / "existing")
    (repo / "uncommitted").write_text("preserve")
    assert discovery.clone_repo("ACME/API", repo)[0]
    assert not discovery.clone_repo("other/api", repo)[0]
    assert (repo / "uncommitted").read_text() == "preserve"
    link = tmp_path / "link"
    link.symlink_to(repo, target_is_directory=True)
    assert not discovery.clone_repo("acme/api", link)[0]
    empty = tmp_path / "empty"
    empty.mkdir()
    assert not discovery.clone_repo("acme/api", empty)[0]
    assert not discovery.clone_repo("acme/api", tmp_path / "bad\npath")[0]
    file = tmp_path / "file"
    file.write_text("preserve")
    assert not discovery.clone_repo("acme/api", file / "api")[0]


def test_clone_reserves_destination_and_verifies_result(discovery, tmp_path, monkeypatch):
    destination = tmp_path / "code" / "acme-api"
    original_run = discovery._run
    calls = []

    def run(args, **kwargs):
        if args[0] == "gh":
            calls.append((args, kwargs))
            assert destination.is_dir() and not list(destination.iterdir())
            checkout(destination)
            return reply({})
        return original_run(args, **kwargs)

    monkeypatch.setattr(discovery, "_run", run)
    success, message = discovery.clone_repo("acme/api", destination)
    assert success and "Cloned acme/api" in message
    assert calls == [(["gh", "repo", "clone", "https://github.com/acme/api", str(destination)],
                      {"timeout": 360})]
    assert discovery.discover_local([destination.parent])[0]["identity"] == "acme/api"


def test_failed_clone_keeps_partial_files_and_removes_only_empty_reservation(discovery, tmp_path, monkeypatch):
    destination = tmp_path / "api"
    monkeypatch.setattr(discovery, "_run", lambda *a, **kw: None)
    assert not discovery.clone_repo("acme/api", destination)[0]
    assert not destination.exists()

    def fail(*args, **kwargs):
        (destination / "partial").write_text("keep this")
        return None

    monkeypatch.setattr(discovery, "_run", fail)
    assert not discovery.clone_repo("acme/api", destination)[0]
    assert (destination / "partial").read_text() == "keep this"


def test_clone_race_never_overwrites_a_destination(discovery, tmp_path, monkeypatch):
    destination = tmp_path / "api"
    original = discovery.Path.mkdir

    def racing_mkdir(path, *args, **kwargs):
        if path == destination:
            original(path)
            (path / "other-work").write_text("keep")
            raise FileExistsError(str(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(discovery.Path, "mkdir", racing_mkdir)
    assert not discovery.clone_repo("acme/api", destination)[0]
    assert (destination / "other-work").read_text() == "keep"


def test_tailscale_macos_bundle_runs_as_cli_without_opening_windows(discovery, monkeypatch):
    monkeypatch.setattr(discovery.shutil, 'which', lambda _: None)
    monkeypatch.setattr(discovery.os, 'access', lambda path, mode: path.startswith('/Applications/'))
    calls = []
    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return reply({'BackendState': 'Running', 'Peer': {}})
    monkeypatch.setattr(discovery.subprocess, 'run', run)
    assert discovery.tailscale_hosts() == ([], [])
    assert calls[0][0] == ['/Applications/Tailscale.app/Contents/MacOS/Tailscale', 'status', '--json']
    assert calls[0][1]['env']['TAILSCALE_BE_CLI'] == '1'
