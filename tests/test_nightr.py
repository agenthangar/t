import argparse
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
    monkeypatch.setitem(module["command"].__globals__, "assert_migration_safe", lambda: None)
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


def test_installer_nightr_selection_is_optional(t_mod):
    selected = t_mod._install_result([{"t": "toggle", "kind": "app", "alias": "Nightr", "checked": True}])
    steps = t_mod._install_plan(selected, {}, "darwin", False, False, {})
    app = next(step for step in steps if step["step"] == "app:nightr")
    assert app["cmd"][-5:] == ["system", "apps", "install", "nightr", "--yes"]
    assert "nightr" not in t_mod.INSTALL_AGENTS


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
                    SimpleNamespace(returncode=0, stdout='{"holding":true}')])
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


@pytest.mark.parametrize("returncode, stdout", [(1, ""), (0, "invalid json"), (0, '{"holding":false}')])
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
