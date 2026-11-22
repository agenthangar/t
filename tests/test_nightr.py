import argparse
import contextlib
import json
from pathlib import Path
import runpy
import subprocess
from types import SimpleNamespace

import pytest


@pytest.fixture
def apps(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    module = runpy.run_path(str(Path(__file__).parents[1] / "libexec/t_nightr.py"))
    monkeypatch.setitem(module["command"].__globals__, "assert_migration_safe", lambda **_: None)
    return module


def patch(apps, monkeypatch, **values):
    namespace = apps["command"].__globals__
    for name, value in values.items():
        monkeypatch.setitem(namespace, name, value)


def args(action="install", **kwargs):
    return argparse.Namespace(action=action, app="nightr", yes=False, dry_run=False, json=False, **kwargs)


def test_app_command_routes_and_readonly(t_mod):
    for words in (["system", "apps", "install", "nightr"], ["install", "nightr"]):
        parsed = t_mod.build_parser().parse_args(words)
        assert (parsed.verb, parsed.system_resource, parsed.action, parsed.app) == ("system", "apps", "install", "nightr")
    for words in (["system", "apps", "status", "nightr", "--json"],
                  ["system", "apps", "install", "nightr", "--dry-run"], ["install", "nightr", "--dry-run"]):
        assert t_mod._command_readonly(words)
    assert not t_mod._command_readonly(["system", "apps", "install", "nightr", "--yes"])
    assert t_mod.build_parser().parse_args(["agent", "install", "codex"]).agents == ["codex"]


def test_status_never_installs_or_probes_network(apps, monkeypatch, capsys):
    patch(apps, monkeypatch, installed=lambda: "0.1.0", package=lambda *a: pytest.fail("package called"))
    monkeypatch.setattr(apps["sys"], "platform", "darwin")
    request = args("status")
    request.json = True
    assert apps["command"](request) == 0
    assert json.loads(capsys.readouterr().out)["installed"] is True
    assert not apps["state_path"]().exists()


def test_install_dry_run_and_linux_are_safe(apps, monkeypatch, capsys):
    patch(apps, monkeypatch, installed=lambda: None, install=lambda: pytest.fail("install called"))
    monkeypatch.setattr(apps["sys"], "platform", "linux")
    assert apps["command"](args()) == 1
    assert "requires macOS" in capsys.readouterr().err
    monkeypatch.setattr(apps["sys"], "platform", "darwin")
    request = args()
    request.dry_run = True
    assert apps["command"](request) == 0
    assert not apps["state_path"]().exists()


def test_managed_update_never_installs_absent_app(apps, monkeypatch):
    monkeypatch.setattr(apps["sys"], "platform", "darwin")
    patch(apps, monkeypatch, installed=lambda: None, package=lambda *a: pytest.fail("package called"))
    assert apps["update_managed"]() == 0
    apps["record"]("direct")
    assert apps["update_managed"]() == 0


def test_direct_and_homebrew_updates_preserve_owner(apps, monkeypatch):
    monkeypatch.setattr(apps["sys"], "platform", "darwin")
    calls = []
    patch(apps, monkeypatch, installed=lambda: "0.1.0", package=lambda action: calls.append(action) or 0)
    apps["record"]("direct")
    assert apps["update_managed"](check=True) == 0
    assert apps["update_managed"]() == 0
    assert calls == ["check", "update"]
    apps["record"]("homebrew")
    assert apps["update_managed"]() == 0
    assert calls == ["check", "update", "update"]


def test_install_records_only_successful_verified_package(apps, monkeypatch, tmp_path):
    monkeypatch.setattr(apps["sys"], "platform", "darwin")
    monkeypatch.setattr(apps["shutil"], "which", lambda name: None)
    patch(apps, monkeypatch, APP=tmp_path / "Nightr.app", installed=lambda: None, package=lambda action: 5)
    assert apps["install"]() == 5
    assert apps["integration"]() is None
    versions = iter([None, "0.1.0"])
    patch(apps, monkeypatch, installed=lambda: next(versions), package=lambda action: 0, package_owner=lambda: "direct")
    assert apps["install"]() == 0
    assert apps["integration"]()["owner"] == "direct"


def test_existing_install_opt_in_does_not_reinstall(apps, monkeypatch):
    calls = []
    monkeypatch.setattr(apps["sys"], "platform", "darwin")
    monkeypatch.setattr(apps["shutil"], "which", lambda name: "/test/brew")
    patch(apps, monkeypatch, installed=lambda: "0.1.0", brew_owned=lambda brew: True,
          package=lambda action: calls.append(action) or 0)
    assert apps["install"]() == 0
    assert calls == ["adopt-homebrew"]
    assert apps["integration"]()["owner"] == "homebrew"


def test_invalid_integration_and_symlink_refused(apps, tmp_path):
    path = apps["state_path"]()
    path.parent.mkdir(parents=True)
    path.write_text('{"schemaVersion":2}')
    with pytest.raises(ValueError):
        apps["integration"]()
    path.unlink()
    other = tmp_path / "other"
    other.write_text("untouched")
    path.symlink_to(other)
    with pytest.raises(ValueError):
        apps["record"]("direct")
    assert other.read_text() == "untouched"


def test_update_reports_app_failure_and_skips_dev(t_mod, monkeypatch):
    calls = []
    monkeypatch.setattr(t_mod, "_cmd_update_t", lambda cfg, args: 0)
    monkeypatch.setattr(t_mod, "_nightr_module", lambda: {"update_managed": lambda **kw: calls.append(kw) or 4})
    request = argparse.Namespace(all=False, check=False, dev=False, relink=False)
    assert t_mod.cmd_update(None, request) == 4
    request.dev = True
    assert t_mod.cmd_update(None, request) == 0
    assert calls == [{"check": False}]


@pytest.fixture
def guided_wizard(t_mod, monkeypatch):
    monkeypatch.setattr(t_mod, "_install_probe", lambda: {
        agent: {"version": "1.0", "logged_in": True} for agent in t_mod.INSTALL_AGENTS})
    monkeypatch.setattr(t_mod, "_install_relink", lambda: 0)
    monkeypatch.setattr(t_mod, "_repair_shell_setup", lambda: 0)
    monkeypatch.setattr(t_mod, "_install_then_setup", lambda cfg, args, rc, **kw: rc)
    return t_mod


@pytest.mark.parametrize("selection, checked, confirmed, expected", [
    (("install", None), True, True, 0),
    (("update", "0.1.0"), True, True, 0),
    (("install", None), False, True, 0),
    (("install", None), True, False, 130),
    (("unknown", None), True, True, 1),
])
def test_guided_nightr_review_runs_only_the_confirmed_action(guided_wizard, monkeypatch, selection,
                                                             checked, confirmed, expected):
    t_mod = guided_wizard
    monkeypatch.setattr(t_mod.sys, "platform", "darwin")
    monkeypatch.setattr(t_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(t_mod.sys.stdout, "isatty", lambda: True)
    request = t_mod.build_parser().parse_args(["install", "--no-hosts"])
    calls = []

    def review(items, plan):
        app = next(item for item in items if item.get("kind") == "app")
        assert app["alias"] == "Nightr" and not app["checked"]
        app["checked"] = checked
        steps = plan()
        lines = t_mod._install_render(steps, t_mod.Style())
        if not checked:
            assert not any(step["step"] == "app:nightr" for step in steps)
        elif selection[0] == "update":
            assert any("nightr update" in line for line in lines)
            assert not any("brew" in line for line in lines)
        elif selection[0] == "install":
            tap = next(i for i, line in enumerate(lines) if "brew tap agenthangar/tap  # if needed" in line)
            assert "brew install --cask agenthangar/tap/nightr" in lines[tap + 1]
        else:
            assert any("repair the unrecognized installation" in line for line in lines)
        assert not calls
        # Applying the review uses its steps even if the live checklist differs.
        app["checked"] = not checked
        return confirmed

    monkeypatch.setattr(t_mod, "_install_wizard", review)
    monkeypatch.setattr(t_mod, "_nightr_module", lambda: {
        "guided_selection": lambda: selection, "guided_install": lambda *a: calls.append(a) or 0})
    assert t_mod.cmd_install(SimpleNamespace(hosts={}), request) == expected
    assert calls == ([selection] if checked and confirmed and expected == 0 else [])
    assert "nightr" not in t_mod.INSTALL_AGENTS


@pytest.mark.parametrize("platform, terminal, flags", [
    ("linux", True, []),
    ("darwin", False, []),
    ("darwin", True, ["--yes"]),
    ("darwin", True, ["--dry-run"]),
    ("darwin", True, ["codex"]),
    ("darwin", True, ["codex", "--yes"]),
])
def test_guided_nightr_stays_out_of_agent_only_modes(guided_wizard, monkeypatch, platform, terminal, flags):
    t_mod = guided_wizard
    monkeypatch.setattr(t_mod.sys, "platform", platform)
    monkeypatch.setattr(t_mod.sys.stdin, "isatty", lambda: terminal)
    monkeypatch.setattr(t_mod.sys.stdout, "isatty", lambda: terminal)
    request = t_mod.build_parser().parse_args(["install", "--no-hosts", *flags])
    monkeypatch.setattr(t_mod, "_nightr_module", lambda: pytest.fail("agent-only mode queried Nightr"))

    def review(items, _):
        assert not any(item.get("sec") == "apps" for item in items)
        return False

    monkeypatch.setattr(t_mod, "_install_wizard", review)
    assert t_mod.cmd_install(SimpleNamespace(hosts={}), request) in (0, 1, 130)


@pytest.mark.parametrize("result, owner, expected", [(0, "homebrew", 0), (7, None, 7), (0, "direct", 1), (0, None, 1)])
def test_guided_fresh_install_requires_verified_homebrew(apps, monkeypatch, result, owner, expected):
    monkeypatch.setattr(apps["sys"], "platform", "darwin")
    monkeypatch.setattr(apps["shutil"], "which", lambda name: "/test/brew")
    events = []
    team = "ABCDE12345"

    def execute(argv):
        events.append(argv)
        return result

    patch(apps, monkeypatch, guided_selection=lambda: ("install", None),
          assert_migration_safe=lambda **kw: events.append(kw), package_installer=lambda: {
              "InstallError": RuntimeError, "main": execute,
              "latest_manifest": lambda _: {"teamID": team}, "installation_lock": contextlib.nullcontext,
              "verify_installed_receipt": lambda expected: events.append(("verify", expected)) or "0.1.0",
              "verify_homebrew_receipt": lambda brew, version: events.append((brew, version)),
              "read_record": lambda: {"owner": owner, "teamID": team, "version": "0.1.0"} if owner else None})
    assert apps["guided_install"]("install", None) == expected
    assert events[:2] == [{"verified_app": False}, ["install", "--homebrew-only"]]
    assert events[2:] == ([] if result else [("verify", team)] + ([('/test/brew', '0.1.0')] if not expected else []))
    saved = apps["integration"]()
    assert (saved["owner"] if saved else None) == (owner if expected == 0 else None)


@pytest.mark.parametrize("selection, current", [(("install", None), ("update", "0.1.0")),
                                                (("update", "0.1.0"), ("install", None))])
def test_guided_install_does_not_switch_actions(apps, monkeypatch, selection, current, capsys):
    monkeypatch.setattr(apps["sys"], "platform", "darwin")
    patch(apps, monkeypatch, guided_selection=lambda: current,
          package_installer=lambda: {"InstallError": RuntimeError})
    assert apps["guided_install"](*selection) == 1
    assert "changed after review" in capsys.readouterr().err
    assert apps["integration"]() is None


@pytest.mark.parametrize("owner, adoption, failure", [
    ("direct", False, None), ("homebrew", True, None), ("homebrew", False, None), ("direct", True, None),
    ("direct", False, "verification"), ("homebrew", True, "adoption"), ("direct", False, "update"),
    ("homebrew", False, "post-update verification"), ("direct", False, "changed owner"),
    ("homebrew", False, "receipt"), ("homebrew", False, "post-update receipt"), ("homebrew", False, "ahead"),
])
def test_guided_existing_update_verifies_before_native_execution(apps, monkeypatch, owner, adoption, failure):
    monkeypatch.setattr(apps["sys"], "platform", "darwin")
    monkeypatch.setattr(apps["shutil"], "which", lambda name: "/test/brew")
    events = []
    team = "ABCDE12345"
    package = None if adoption else {"owner": owner, "teamID": team, "version": "0.1.0"}
    bundle_version = "0.1.0"

    def verify(expected_team):
        events.append(("verify", expected_team))
        if failure == "verification" or (failure == "post-update verification" and bundle_version == "0.1.1"):
            raise RuntimeError("signature or package receipt could not be verified")
        return bundle_version

    def adopt(argv):
        nonlocal package
        events.append(argv)
        if failure == "adoption":
            return 7
        package = {"owner": owner, "teamID": team, "version": bundle_version}
        return 0

    def verify_homebrew(brew, version):
        events.append((brew, version))
        if failure == "receipt" or (failure == "post-update receipt" and version == "0.1.1"):
            raise RuntimeError("the exact Homebrew receipt could not be verified")

    def update(argv):
        nonlocal bundle_version
        events.append(argv)
        if failure == "update":
            return 8
        bundle_version = package["version"] = "0.1.1"
        if failure == "changed owner":
            package["owner"] = "homebrew"
        return 0

    patch(apps, monkeypatch, guided_selection=lambda: ("update", "0.1.0"),
          assert_migration_safe=lambda **kw: events.append(kw), package_installer=lambda: {
              "InstallError": RuntimeError, "latest_manifest": lambda _: {
                  "teamID": team, "version": "0.0.9" if failure == "ahead" else "0.1.1"},
              "version_key": lambda version: tuple(map(int, version.split("."))),
              "installation_lock": contextlib.nullcontext, "verify_installed_receipt": verify,
              "verify_homebrew_receipt": verify_homebrew, "read_record": lambda: package,
              "homebrew_receipt": lambda *a, **kw: owner == "homebrew", "main": adopt})
    monkeypatch.setattr(apps["subprocess"], "call", update)
    result = apps["guided_install"]("update", "0.1.0")
    assert events.pop(0) == ("verify", team)
    if failure not in ("verification", "ahead") and owner == "homebrew":
        assert events.pop(0) == ("/test/brew", "0.1.0")
    if failure not in ("verification", "ahead", "receipt"):
        assert events.pop(0) == {"verified_app": True}
        if adoption:
            assert events.pop(0) == ["adopt-" + owner]
        if failure != "adoption":
            assert events.pop(0) == ("verify", team)
            if owner == "homebrew":
                assert events.pop(0) == ("/test/brew", "0.1.0")
            assert events.pop(0) == [str(apps["CLI"]), "update"]
            if failure != "update":
                assert events.pop(0) == ("verify", team)
                if owner == "homebrew" and failure != "post-update verification":
                    assert events.pop(0) == ("/test/brew", "0.1.1")
    assert not events
    assert result == {None: 0, "adoption": 7, "update": 8}.get(failure, 1)
    saved = apps["integration"]()
    assert (saved["owner"] if saved else None) == (owner if not failure else None)


def test_nosleep_delegates_installed_app_before_legacy_monitor(tmp_path):
    executable = tmp_path / "nightr-native"
    executable.write_text('#!/bin/sh\nprintf "native:%s\\n" "$@"\nexit 7\n')
    executable.chmod(0o700)
    source = Path(__file__).parents[1] / "zsh/nosleep.zsh"
    isolated = tmp_path / "nosleep.zsh"
    isolated.write_text(source.read_text().replace('/Applications/Nightr.app/Contents/MacOS/nightr-native', str(executable)))
    result = subprocess.run(['zsh', '-f', '-c', 'source "$1"; nosleep --forever', 'test', str(isolated)],
                            capture_output=True, text=True, timeout=5,
                            env={"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"})
    assert result.returncode == 7
    assert result.stdout == 'native:--forever\n'


def test_migration_refuses_unowned_hold_without_reset(apps, monkeypatch):
    calls = []
    patch(apps, monkeypatch, installed=lambda: None)
    monkeypatch.setattr(apps["subprocess"], "run", lambda argv, **kw: calls.append(argv) or SimpleNamespace(returncode=0, stdout="System-wide power settings:\n SleepDisabled 1\n"))
    with pytest.raises(ValueError, match="Stop the existing nosleep"):
        apps["assert_migration_safe"]()
    assert calls == [["/usr/bin/pmset", "-g"]]


def test_migration_allows_native_owned_hold(apps, monkeypatch):
    patch(apps, monkeypatch, installed=lambda: "0.1.0")
    results = iter([SimpleNamespace(returncode=0, stdout=" SleepDisabled 1\n"),
                    SimpleNamespace(returncode=0, stdout='{"version":1,"status":{"holding":true}}')])
    monkeypatch.setattr(apps["subprocess"], "run", lambda *a, **kw: next(results))
    apps["assert_migration_safe"]()


@pytest.mark.parametrize("choice, expected", [("cancel", 130), ("install", 7)])
def test_install_review_restores_terminal_and_respects_choice(apps, monkeypatch, choice, expected):
    calls = []
    monkeypatch.setattr(apps["sys"], "platform", "darwin")
    monkeypatch.setattr(apps["sys"].stdin, "isatty", lambda: True)
    monkeypatch.setattr(apps["sys"].stdout, "isatty", lambda: True)
    patch(apps, monkeypatch, installed=lambda: None, install=lambda: calls.append("install") or 7)
    class UI:
        def raw(self): calls.append("raw")
        def pick(self, *args): return choice
        def restore(self): calls.append("restore")
    assert apps["command"](args(), UI) == expected
    assert calls == (["raw", "restore", "install"] if choice == "install" else ["raw", "restore"])


def test_concurrent_install_does_not_enter_installer(apps, monkeypatch):
    import fcntl
    monkeypatch.setattr(apps["sys"], "platform", "darwin")
    patch(apps, monkeypatch, installed=lambda: None, install=lambda: pytest.fail("concurrent install"))
    path = apps["state_path"]().parent
    path.mkdir(parents=True)
    with (path / ".nightr.lock").open("w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        request = args()
        request.yes = True
        assert apps["command"](request) == 1


@pytest.mark.parametrize("returncode, stdout", [(1, ""), (0, "invalid json"),
    (0, '{"version":1,"status":{"holding":false}}'), (0, '{"holding":true}'),
    (0, '{"version":2,"status":{"holding":true}}'),
    (0, '{"version":1,"error":"unknown owner","status":{"holding":true}}')])
def test_migration_never_accepts_unverified_native_owner(apps, monkeypatch, returncode, stdout):
    patch(apps, monkeypatch, installed=lambda: "0.1.0")
    results = iter([SimpleNamespace(returncode=0, stdout=" SleepDisabled 1\n"),
                    SimpleNamespace(returncode=returncode, stdout=stdout)])
    monkeypatch.setattr(apps["subprocess"], "run", lambda *a, **kw: next(results))
    with pytest.raises(ValueError, match="another sleep utility"):
        apps["assert_migration_safe"]()


def test_migration_probe_failure_preserves_system(apps, monkeypatch):
    monkeypatch.setattr(apps["subprocess"], "run", lambda *a, **kw: SimpleNamespace(returncode=1, stdout=""))
    with pytest.raises(ValueError, match="could not check"):
        apps["assert_migration_safe"]()


def test_installed_requires_expected_bundle_and_cli(apps, monkeypatch, tmp_path):
    import plistlib
    app = tmp_path / "Nightr.app"
    cli = app / "Contents/MacOS/nightr-native"
    patch(apps, monkeypatch, APP=app, CLI=cli)
    assert apps["installed"]() is None
    cli.parent.mkdir(parents=True)
    info = app / "Contents/Info.plist"
    info.write_bytes(plistlib.dumps({"CFBundleIdentifier": "ai.nightr.app", "CFBundleShortVersionString": "0.1.0"}))
    assert apps["installed"]() is None
    cli.write_text("placeholder")
    assert apps["installed"]() == "0.1.0"
    info.write_bytes(plistlib.dumps({"CFBundleIdentifier": "other.app"}))
    assert apps["installed"]() is None


def test_update_error_is_reported_without_hiding_t_failure(t_mod, monkeypatch, capsys):
    monkeypatch.setattr(t_mod, "_cmd_update_t", lambda *a: 3)
    def fail(**kwargs): raise ValueError("receipt mismatch")
    monkeypatch.setattr(t_mod, "_nightr_module", lambda: {"update_managed": fail})
    request = argparse.Namespace(all=False, check=False, dev=False, relink=False)
    assert t_mod.cmd_update(None, request) == 3
    assert "receipt mismatch" in capsys.readouterr().err


@pytest.mark.parametrize("owner", ["direct", "homebrew"])
def test_fresh_install_delegates_package_choice_and_records_verified_owner(apps, monkeypatch, tmp_path, owner):
    monkeypatch.setattr(apps["sys"], "platform", "darwin")
    versions = iter([None, "0.1.0"])
    calls = []
    patch(apps, monkeypatch, APP=tmp_path / "Nightr.app", installed=lambda: next(versions),
          package=lambda action: calls.append(action) or 0, package_owner=lambda: owner)
    monkeypatch.setattr(apps["subprocess"], "run", lambda *a, **kw: pytest.fail("t must not launch an unaudited cask"))
    assert apps["install"]() == 0
    assert calls == ["install"]
    assert apps["integration"]()["owner"] == owner


@pytest.mark.parametrize("record", [None, {}, {"owner": "unknown"}])
def test_missing_package_ownership_cannot_opt_in(apps, monkeypatch, record):
    monkeypatch.setattr(apps["runpy"], "run_path", lambda path: {"read_record": lambda: record})
    with pytest.raises(ValueError, match="ownership is missing"):
        apps["package_owner"]()


@pytest.mark.parametrize("owner", ["direct", "homebrew"])
@pytest.mark.parametrize("result", [0, 7])
def test_existing_app_adoption_records_only_verified_success(apps, monkeypatch, owner, result):
    monkeypatch.setattr(apps["sys"], "platform", "darwin")
    monkeypatch.setattr(apps["shutil"], "which", lambda name: None if owner == "direct" else "/test/brew")
    calls = []
    patch(apps, monkeypatch, installed=lambda: "0.1.0",
          brew_owned=lambda brew: owner == "homebrew",
          package=lambda action: calls.append(action) or result)
    assert apps["install"]() == result
    assert calls == ["adopt-" + owner]
    saved = apps["integration"]()
    assert (saved["owner"] if saved else None) == (owner if result == 0 else None)
    if result == 0:
        assert apps["install"]() == 0
        assert len(calls) == 1


def test_install_rejects_unknown_bundle_and_missing_postinstall_app(apps, monkeypatch, tmp_path):
    monkeypatch.setattr(apps["sys"], "platform", "darwin")
    app = tmp_path / "Nightr.app"
    app.mkdir()
    patch(apps, monkeypatch, APP=app, installed=lambda: None,
          package=lambda action: pytest.fail("unknown bundle overwritten"))
    with pytest.raises(ValueError, match="unrecognized"):
        apps["install"]()
    app.rmdir()
    patch(apps, monkeypatch, package=lambda action: 0)
    with pytest.raises(ValueError, match="did not produce"):
        apps["install"]()
    assert apps["integration"]() is None
    monkeypatch.setattr(apps["sys"], "platform", "linux")
    with pytest.raises(ValueError, match="requires macOS"):
        apps["install"]()


def test_package_dispatch_uses_bundled_helper(apps, monkeypatch):
    calls = []
    def load(path):
        calls.append(Path(path))
        return {"main": lambda argv: calls.append(argv) or 6,
                "read_record": lambda: {"owner": "direct"}}
    monkeypatch.setattr(apps["runpy"], "run_path", load)
    assert apps["package"]("check") == 6
    assert apps["package_owner"]() == "direct"
    assert calls == [Path(__file__).parents[1] / "libexec/nightr_install.py", ["check"],
                     Path(__file__).parents[1] / "libexec/nightr_install.py"]


def test_brew_probe_and_clear_power_check_are_readonly(apps, monkeypatch):
    calls = []
    monkeypatch.setattr(apps["subprocess"], "run", lambda argv, **kw:
                        calls.append(argv) or SimpleNamespace(returncode=0, stdout="SleepDisabled 0\n"))
    assert apps["brew_owned"](None) is False
    assert apps["brew_owned"]("/test/brew") is True
    apps["assert_migration_safe"]()
    assert calls == [["/test/brew", "list", "--cask", "agenthangar/tap/nightr"], ["/usr/bin/pmset", "-g"]]


def test_status_text_and_existing_dry_run_do_not_mutate(apps, monkeypatch, capsys):
    monkeypatch.setattr(apps["sys"], "platform", "darwin")
    apps["record"]("direct")
    patch(apps, monkeypatch, installed=lambda: "0.1.0", install=lambda: pytest.fail("dry run installed"))
    assert apps["command"](args("status")) == 0
    assert "0.1.0 · updates managed by t" in capsys.readouterr().out
    request = args()
    request.dry_run = True
    assert apps["command"](request) == 0
    assert "existing Nightr installation" in capsys.readouterr().out


def test_invalid_lock_owner_prevents_install(apps, monkeypatch, capsys):
    monkeypatch.setattr(apps["sys"], "platform", "darwin")
    patch(apps, monkeypatch, installed=lambda: None, install=lambda: pytest.fail("unsafe lock accepted"))
    original = apps["os"].fstat
    def foreign_owner(fd):
        info = list(original(fd))
        info[4] = apps["os"].getuid() + 1
        return apps["os"].stat_result(info)
    monkeypatch.setattr(apps["os"], "fstat", foreign_owner)
    request = args()
    request.yes = True
    assert apps["command"](request) == 1
    assert "invalid Nightr integration lock" in capsys.readouterr().err
