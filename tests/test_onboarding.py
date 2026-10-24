"""First-run setup stages choices before touching the account or cloning anything."""

from contextlib import contextmanager
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from conftest import REPO_ROOT, load_script


@pytest.fixture
def onboarding():
    return load_script(REPO_ROOT / "libexec/t_onboarding.py", "t_onboarding_tests")


@pytest.fixture
def account(t_mod, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setattr(t_mod, "HOME", str(home))
    monkeypatch.setattr(t_mod, "CONFIG", str(home / "cache"))
    cfg = t_mod.Config()
    local = home / "local.zsh"
    events = []
    api = dict(vars(t_mod))
    api.update(HOME=str(home), Config=lambda: cfg,
               _current_local_rc=lambda: str(local),
               _shell_setup_helpers=lambda: {"state": lambda *_: (home / ".zshrc", "missing")},
               _host_refresh_config=lambda: events.append("refresh"),
               _run=lambda *args, **kwargs: SimpleNamespace(returncode=1, stdout=""),
               _install_probe=lambda: {
                   "claude": {"version": "1", "logged_in": True},
                   "codex": {"version": "", "logged_in": False},
                   "cursor": {"version": "", "logged_in": False}},
               _install_walk=lambda steps: events.append(("install", steps)) or 0,
               cmd_integrate=lambda *_: events.append("integrate") or 0,
               _repo_add_target=lambda cfg, path, alias: (alias, path),
               _onboarding_start=lambda cfg, agent: events.append(("start", agent)) or 0)

    def add(cfg, args):
        events.append(("register", args.path))
        cfg.repos[args.alias] = args.path
        return 0

    api["cmd_repo_add"] = add
    return SimpleNamespace(home=home, cfg=cfg, api=api, events=events, local=local)


@pytest.fixture
def discovery():
    return {"discover_local": lambda *_: [], "github_auth_state": lambda: "authenticated",
            "github_orgs": lambda: ([], []), "github_repos": lambda org: ([], []),
            "github_install_command": lambda: None, "tailscale_hosts": lambda: ([], []),
            "clone_destination": lambda name: "/clones/" + name.replace("/", "-"),
            "clone_repo": lambda *args: (True, "Cloned " + args[0])}


class Choices:
    def __init__(self, answers=(), reviews=(True,)):
        self.answers = iter(answers)
        self.reviews = iter(reviews)
        self.labels = []
        self.notices = []
        self.reviewed = []
        self.closed = False
        self.suspensions = 0

    def choose(self, label, rows, **kwargs):
        self.labels.append((label, rows, kwargs))
        if not rows:
            return [] if kwargs.get("multi") else None
        answer = next(self.answers)
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def review(self, lines):
        self.reviewed.append(lines)
        return next(self.reviews)

    def notice(self, text):
        self.notices.append(text)

    @contextmanager
    def suspended(self):
        self.suspensions += 1
        yield

    def close(self):
        self.closed = True


def interactive(monkeypatch, onboarding, dialog, discovery):
    monkeypatch.setattr(onboarding.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(onboarding.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(onboarding, "Dialog", lambda _: dialog)
    monkeypatch.setattr(onboarding, "_discovery", lambda _: discovery)


def test_needed_marker_existing_accounts_and_bad_marker(onboarding, account):
    assert onboarding.needed(account.cfg, account.api)
    marker = onboarding._marker()
    marker.parent.mkdir(parents=True)
    for text in ("broken", "[]", '{"version": 2}'):
        marker.write_text(text)
        assert onboarding.needed(account.cfg, account.api)
    marker.unlink()
    account.cfg.repos["api"] = "/api"
    account.api["_shell_setup_helpers"] = lambda: {"state": lambda *_: (None, "configured")}
    assert not onboarding.needed(account.cfg, account.api)
    account.cfg.repos.clear()
    onboarding._complete("codex")
    assert json.loads(marker.read_text()) == {"version": 1, "agent": "codex"}
    assert marker.stat().st_mode & 0o777 == 0o600
    assert not onboarding.needed(account.cfg, account.api)


def test_marker_failed_replace_cleans_temporary_file(onboarding, account, monkeypatch):
    monkeypatch.setattr(onboarding.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("failed")))
    with pytest.raises(OSError):
        onboarding._complete(None)
    assert list(onboarding._marker().parent.iterdir()) == []


def test_no_terminal_no_discovery_or_mutations(onboarding, account, monkeypatch):
    monkeypatch.setattr(onboarding.sys.stdin, "isatty", lambda: False)
    assert onboarding.run(account.cfg, account.api) == 1
    assert account.events == []
    assert not onboarding._marker().exists()


def test_existing_account_goes_to_launcher(onboarding, account, monkeypatch, discovery):
    onboarding._complete("claude")
    interactive(monkeypatch, onboarding, Choices(), discovery)
    assert onboarding.run(account.cfg, account.api) == 0
    assert account.events == [("start", None)]


@pytest.mark.parametrize("answer,result", [("later", 0), (EOFError(), 130), (KeyboardInterrupt(), 130)])
def test_welcome_skip_and_cancel_leave_account_untouched(onboarding, account, monkeypatch, discovery, answer, result):
    dialog = Choices([answer])
    interactive(monkeypatch, onboarding, dialog, discovery)
    assert onboarding.run(account.cfg, account.api) == result
    assert account.events == []
    assert dialog.closed
    assert not onboarding._marker().exists()


def test_complete_flow_reviews_before_cloning_and_registers_selection(onboarding, account, monkeypatch, discovery):
    path = str(account.home / "code" / "api")
    discovery.update(discover_local=lambda *_: [{"path": path, "identity": "acme/api"}],
                     github_orgs=lambda: (["acme"], []),
                     github_repos=lambda _: ([{"full_name": "acme/web"}], []),
                     tailscale_hosts=lambda: ([{"alias": "mini", "target": "mini.tail.test"}], []))
    dialog = Choices(["setup", ["claude", "codex"], "codex", [0], ["acme"], [0], [0], "start"])
    interactive(monkeypatch, onboarding, dialog, discovery)

    def clone(name, target):
        assert dialog.reviewed
        account.events.append(("clone", name, target))
        return True, "Cloned repository"

    discovery["clone_repo"] = clone
    assert onboarding.run(account.cfg, account.api) == 0
    assert account.cfg.agent_default == "codex"
    assert account.cfg.hosts == {"mini": "mini.tail.test"}
    assert list(account.cfg.repos.values()) == [path, "/clones/acme-web"]
    assert account.events[-1] == ("start", "codex")
    review = "\n".join(dialog.reviewed[0])
    assert "Clone acme/web → /clones/acme-web" in review
    assert "no remote commands" in review
    assert "public-repository workflow instructions" in review
    assert "codex" in account.local.read_text()
    assert json.loads(onboarding._marker().read_text())["agent"] == "codex"
    assert dialog.closed


def test_empty_selection_can_finish_and_review_can_return_to_choices(onboarding, account, monkeypatch, discovery):
    dialog = Choices(["setup", [], []], reviews=[False, True])
    interactive(monkeypatch, onboarding, dialog, discovery)
    assert onboarding.run(account.cfg, account.api, force=True) == 0
    assert len(dialog.reviewed) == 2
    assert "integrate" in account.events
    assert not any(isinstance(event, tuple) and event[0] == "start" for event in account.events)


def test_review_cancellation_never_installs_or_writes(onboarding, account, monkeypatch, discovery):
    dialog = Choices(["setup", ["codex"]])
    dialog.review = lambda _: (_ for _ in ()).throw(onboarding.Aborted())
    interactive(monkeypatch, onboarding, dialog, discovery)
    assert onboarding.run(account.cfg, account.api) == 130
    assert account.events == []


@pytest.mark.parametrize("failure", ["install", "integrate", "clone", "register", "exception"])
def test_partial_failure_keeps_completed_steps_and_no_completion_marker(onboarding, account, monkeypatch, discovery, failure):
    discovery.update(github_orgs=lambda: (["acme"], []),
                     github_repos=lambda _: ([{"full_name": "acme/web"}], []))
    dialog = Choices(["setup", ["codex"], ["acme"], [0]])
    interactive(monkeypatch, onboarding, dialog, discovery)
    if failure == "install":
        account.api["_install_walk"] = lambda _: 1
    elif failure == "integrate":
        account.api["cmd_integrate"] = lambda *_: 1
    elif failure == "clone":
        discovery["clone_repo"] = lambda *_: (False, "clone failed")
    elif failure == "register":
        account.api["cmd_repo_add"] = lambda *_: 1
    else:
        account.api["cmd_integrate"] = lambda *_: (_ for _ in ()).throw(OSError("unavailable"))
    assert onboarding.run(account.cfg, account.api) == 1
    assert json.loads(onboarding._marker().read_text())["version"] == 0
    account.cfg.repos["partial"] = "/partial"
    account.api["_shell_setup_helpers"] = lambda: {"state": lambda *_: (None, "configured")}
    assert onboarding.needed(account.cfg, account.api)
    assert dialog.closed
    assert any("incomplete" in text or "could not finish" in text for text in dialog.notices)


def test_existing_clones_and_hosts_are_deduplicated(onboarding, account, discovery):
    account.cfg.repos["api"] = "/existing/api"
    account.cfg.hosts = {"mini": "mini.tail.test", "taken": "old.tail.test"}
    discovery.update(discover_local=lambda *_: [
        {"path": "/existing/api", "identity": "acme/api"},
        {"path": "/other/web", "identity": "acme/web"}],
        github_orgs=lambda: (["acme"], []),
        github_repos=lambda _: ([{"full_name": "acme/api"}, {"full_name": "acme/web"},
                                 {"full_name": "acme/archive", "archived": True},
                                 {"full_name": "acme/fork", "fork": True}], ["limited access"]),
        tailscale_hosts=lambda: ([{"alias": "mini", "target": "mini.tail.test"},
                                 {"alias": "taken", "target": "other.tail.test"},
                                 {"alias": "taken", "target": "other.tail.test"}], ["discovery notice"]))
    dialog = Choices([["claude"], [], ["acme"], [], [0]])
    plan = onboarding._collect(account.cfg, account.api, dialog, discovery)
    assert plan["clones"] == []
    assert plan["repos"] == []
    assert plan["hosts"] == [{"alias": "other.tail.test", "target": "other.tail.test"}]
    assert any("Already cloned: acme/web" in notice for notice in dialog.notices)
    assert "limited access" in dialog.notices and "discovery notice" in dialog.notices
    labels = [row[1] for label, rows, _ in dialog.labels if label.startswith("Clone") for row in rows]
    assert any("archived" in label for label in labels) and any("fork" in label for label in labels)


def test_local_basename_collision_fails_before_any_mutations(onboarding, account, discovery):
    account.cfg.repos["api"] = "/existing/api"
    plan = {"repos": [{"path": "/different/api"}], "clones": [],
            "aliases": {"/different/api": "different-api"}}
    with pytest.raises(ValueError, match="collide"):
        onboarding._apply(account.cfg, account.api, discovery, plan)
    assert account.events == []


@pytest.mark.parametrize("state,command,answers,codes,expected", [
    ("missing", None, [], [], []),
    ("missing", ["brew", "install", "gh"], ["skip"], [], []),
    ("missing", ["brew", "install", "gh"], ["install"], [1], []),
    ("missing", ["brew", "install", "gh"], ["install", "login"], [0, 0], ["acme"]),
    ("unavailable", None, ["skip"], [], []),
    ("unavailable", None, ["login"], [1], []),
    ("unavailable", None, ["login"], [0], ["acme"]),
    ("authenticated", None, [], [], ["acme"]),
])
def test_optional_github_install_and_login(onboarding, discovery, monkeypatch, state, command, answers, codes, expected):
    states = iter([state, "unavailable"])
    discovery["github_auth_state"] = lambda: next(states)
    discovery["github_install_command"] = lambda: command
    discovery["github_orgs"] = lambda: (["acme"], ["some orgs unavailable"])
    calls = []
    codes = iter(codes)

    def run(argv):
        calls.append(argv)
        return SimpleNamespace(returncode=next(codes))

    monkeypatch.setattr(onboarding.subprocess, "run", run)
    dialog = Choices(answers)
    assert onboarding._organizations(dialog, discovery) == expected
    if "login" in answers and calls[-1][0] == "gh":
        assert calls[-1] == ["gh", "auth", "login", "--hostname", "github.com"]
    assert dialog.suspensions == len(calls)


@pytest.mark.parametrize("state", ["missing", "unavailable"])
def test_github_install_login_os_errors_do_not_abort_local_setup(onboarding, discovery, monkeypatch, state):
    discovery["github_auth_state"] = lambda: state
    discovery["github_install_command"] = lambda: ["brew", "install", "gh"]
    monkeypatch.setattr(onboarding.subprocess, "run", lambda _: (_ for _ in ()).throw(OSError()))
    dialog = Choices(["install" if state == "missing" else "login"])
    assert onboarding._organizations(dialog, discovery) == []
    assert "continuing with local setup" in dialog.notices[-1]


def test_installer_success_without_binary_offers_guidance(onboarding, discovery, monkeypatch):
    discovery["github_auth_state"] = lambda: "missing"
    discovery["github_install_command"] = lambda: ["brew", "install", "gh"]
    monkeypatch.setattr(onboarding.subprocess, "run", lambda _: SimpleNamespace(returncode=0))
    dialog = Choices(["install"])
    assert onboarding._organizations(dialog, discovery) == []
    assert "https://cli.github.com" in dialog.notices[-1]


def test_plain_dialog_selection_review_and_abort(onboarding, monkeypatch, capsys):
    dialog = onboarding.Dialog({"_RailUI": lambda: (_ for _ in ()).throw(OSError())})
    responses = iter(["bogus", "0", "1,2", "2", "", "-", "y", "n", "q", "q"])
    monkeypatch.setattr("builtins.input", lambda _: next(responses))
    rows = [("a", "Alpha"), ("b", "Beta")]
    assert dialog.choose("Single", rows) == "b"
    assert dialog.choose("Multi", rows, multi=True, marked=[0]) == ["a"]
    assert dialog.choose("Multi", rows, multi=True) == []
    assert dialog.review(["changes"])
    assert not dialog.review(["changes"])
    with pytest.raises(onboarding.Aborted):
        dialog.review([])
    with pytest.raises(onboarding.Aborted):
        dialog.choose("Single", rows)
    assert dialog.choose("Empty", [], multi=True) == []
    assert dialog.choose("Empty", []) is None
    dialog.notice("notice")
    with dialog.suspended():
        pass
    dialog.close()
    assert "Choose one of the displayed numbers" in capsys.readouterr().out


def test_rail_dialog_restores_and_uses_shared_ui(onboarding):
    events = []
    rail = SimpleNamespace(raw=lambda: events.append("raw"), restore=lambda: events.append("restore"),
                           pick=lambda *a, **kw: "a", done=lambda *args: events.append(args),
                           commit=lambda lines: events.append(("commit", lines)),
                           page=lambda *args, **kwargs: "y", cols=lambda: 80)
    dialog = onboarding.Dialog({"_RailUI": lambda: rail})
    assert dialog.choose("Single", [("a", "Alpha")]) == "a"
    assert dialog.review(["change"])
    rail.pick = lambda *a, **kw: None
    with pytest.raises(onboarding.Aborted):
        dialog.choose("Single", [("a", "Alpha")])
    dialog.notice("done")
    with dialog.suspended():
        events.append("operation")
    dialog.close()
    assert events == ["raw", ("setup", "done"), ("commit", []), "restore", "operation", "raw", "restore"]


def test_raw_failure_restores_before_plain_fallback(onboarding):
    events = []
    rail = SimpleNamespace(raw=lambda: (_ for _ in ()).throw(OSError()), restore=lambda: events.append("restore"))
    dialog = onboarding.Dialog({"_RailUI": lambda: rail})
    assert dialog.ui is None and events == ["restore"]


def test_aliases_normalize_paths_and_registered_roots_are_discovered(onboarding, account, discovery):
    roots = []
    account.cfg.repos['outside'] = '/somewhere/else/outside'
    discovery['discover_local'] = lambda paths, _: roots.extend(paths) or []
    onboarding._collect(account.cfg, account.api, Choices([[]]), discovery)
    assert '/somewhere/else/outside' in roots
    aliases = onboarding._repo_aliases(account.api, ['/a/$special', '/b/$special', '/c/__add__'], {'-special'})
    assert len(set(aliases.values())) == 3
    assert all(account.api['_CONFIG_ALIAS'].fullmatch(alias) for alias in aliases.values())
    assert '__add__' not in aliases.values()


def test_duplicate_ipv6_host_alias_is_disambiguated(onboarding, account, discovery):
    account.cfg.hosts = {'mini': 'old.tail.test', 'mini-2': 'another.tail.test'}
    discovery['tailscale_hosts'] = lambda: ([{'alias': 'mini', 'target': 'fd7a:115c:a1e0::1'}], [])
    plan = onboarding._collect(account.cfg, account.api, Choices([[], [0]]), discovery)
    assert plan['hosts'] == [{'alias': 'mini-3', 'target': 'fd7a:115c:a1e0::1'}]
