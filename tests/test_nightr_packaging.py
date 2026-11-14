"""Package operations are verified with mocks; never install or change services."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import plistlib
from types import SimpleNamespace
import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("nightr_install", ROOT / "libexec/nightr_install.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)

@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    app = tmp_path / "Applications/Nightr.app"
    state = tmp_path / "state"
    monkeypatch.setattr(installer, "APP", app)
    monkeypatch.setattr(installer, "state_directory", lambda: state)
    monkeypatch.setattr(installer.sys, "platform", "darwin")
    return SimpleNamespace(root=tmp_path, app=app, state=state)

def manifest(version="0.1.0"):
    name = f"Nightr-{version}-universal.dmg"
    return {"schemaVersion": 1, "version": version, "platform": "macos", "minVersion": "13.0",
            "architectures": ["arm64", "x86_64"], "teamID": "ABCDE12345", "bundleID": "ai.nightr.app",
            "controllerID": "ai.nightr.controller", "helperID": "ai.nightr.powerhelper", "protocolVersion": 1,
            "asset": {"name": name, "url": f"https://github.com/agenthangar/nightr/releases/download/v{version}/{name}", "sha256": "a" * 64, "size": 42}}

def test_manifest_pins_release_identity_and_checksum():
    assert installer.validate_manifest(manifest())["version"] == "0.1.0"
    for key, value in [("teamID", ""), ("bundleID", "evil.app"), ("protocolVersion", 2), ("architectures", ["arm64"]), ("version", "../../evil")]:
        data = manifest(); data[key] = value
        with pytest.raises(installer.InstallError): installer.validate_manifest(data)
    for key, value in [("url", "https://example.com/file"), ("sha256", "bad"), ("size", 0)]:
        data = manifest(); data["asset"][key] = value
        with pytest.raises(installer.InstallError): installer.validate_manifest(data)

def test_latest_release_cannot_substitute_manifest(tmp_path, monkeypatch):
    def download(url, path, maximum):
        path.write_text(json.dumps({"assets": [{"name": "nightr-release.json", "browser_download_url": "https://evil.example/manifest"}], "tag_name": "v0.1.0"}))
    monkeypatch.setattr(installer, "download", download)
    with pytest.raises(installer.InstallError, match="Unexpected release"): installer.latest_manifest(tmp_path)

def test_check_does_not_create_persistent_state(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(installer, "latest_manifest", lambda _: manifest())
    assert installer.main(["check", "--json"]) == 0
    assert not sandbox.state.exists()
    result = json.loads(capsys.readouterr().out)
    assert result == {"installed": False, "owner": None, "version": None, "availableVersion": "0.1.0", "updateAvailable": False}

def test_lock_excludes_concurrent_update_and_rejects_symlink(sandbox):
    with installer.installation_lock():
        with pytest.raises(installer.InstallError, match="Another"): 
            with installer.installation_lock(): pass
    (sandbox.state / "package.lock").unlink()
    (sandbox.state / "package.lock").symlink_to(sandbox.root / "unrelated")
    with pytest.raises(OSError):
        with installer.installation_lock(): pass
    assert not (sandbox.root / "unrelated").exists()

def test_record_never_converts_owner_implicitly(sandbox, monkeypatch):
    installer.write_record("direct", "0.1.0", "ABCDE12345", "a" * 64)
    monkeypatch.setattr(installer, "latest_manifest", lambda _: manifest())
    monkeypatch.setattr(installer.shutil, "which", lambda _: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(installer, "run", lambda *a, **k: "nightr 0.1.0")
    assert installer.main(["adopt-homebrew", "--json"]) == 1
    assert installer.read_record()["owner"] == "direct"

def test_update_never_installs_absent_app(sandbox):
    with pytest.raises(installer.InstallError, match="never installs"):
        installer.perform("update", manifest(), sandbox.root)

def test_checksum_failure_precedes_mount_or_install(sandbox, monkeypatch):
    def fake_download(url, target, maximum): target.write_bytes(b"bad"); return 3
    monkeypatch.setattr(installer, "download", fake_download)
    with pytest.raises(installer.InstallError, match="checksum"):
        installer.perform("install", manifest(), sandbox.root)
    assert not sandbox.app.exists()

def test_noninteractive_install_never_silently_elevates(monkeypatch, tmp_path):
    monkeypatch.setattr(installer.os, "geteuid", lambda: 501)
    monkeypatch.setattr(installer.sys, "stdin", SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(installer.subprocess, "run", lambda *a, **k: pytest.fail("must not run sudo"))
    with pytest.raises(installer.InstallError, match="Administrator authorization"):
        installer.install_pkg(tmp_path / "Nightr.pkg")

def test_fixed_system_installer_is_only_elevation(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(installer.os, "geteuid", lambda: 501)
    monkeypatch.setattr(installer.sys, "stdin", SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(installer.subprocess, "run", lambda args, **kwargs: calls.append(args))
    path = tmp_path / "Nightr.pkg"; installer.install_pkg(path)
    assert calls == [["/usr/bin/sudo", "--", "/usr/sbin/installer", "-pkg", str(path), "-target", "/"]]

@pytest.fixture
def mocked_update(sandbox, monkeypatch):
    sandbox.app.mkdir(parents=True)
    sandbox.state.mkdir(parents=True)
    cache = sandbox.state / "packages"; cache.mkdir()
    old = cache / "Nightr-0.1.0-universal.dmg"; old.write_bytes(b"old signed image")
    installer.write_record("direct", "0.1.0", "ABCDE12345", installer._sha256(old))
    new = sandbox.root / "new.dmg"; new.write_bytes(b"new signed image")
    mount = sandbox.root / "mounted"; mount.mkdir()
    (mount / "Nightr.pkg").write_bytes(b"signed pkg")
    events = []
    @contextlib.contextmanager
    def mounted(path, directory):
        events.append(("mount", path.name)); yield mount
    monkeypatch.setattr(installer, "mounted_dmg", mounted)
    monkeypatch.setattr(installer, "download_verified_package", lambda *a: new)
    monkeypatch.setattr(installer, "verify_app", lambda *a, **k: events.append(("verify", a[-1])))
    monkeypatch.setattr(installer, "verify_installed_root", lambda: None)
    monkeypatch.setattr(installer, "verify_package", lambda *a: None)
    monkeypatch.setattr(installer, "check_owned_link", lambda: None)
    monkeypatch.setattr(installer, "coordinate", lambda action: events.append(("coordinate", action)))
    return events

def test_successful_update_coordinates_and_preserves_external_state(sandbox, mocked_update, monkeypatch):
    config = sandbox.state / "config.json"; config.write_text("settings and enabled intent")
    monkeypatch.setattr(installer, "install_pkg", lambda p: mocked_update.append(("install", str(p))))
    result = installer.perform("update", manifest("0.1.1"), sandbox.root)
    assert result["version"] == "0.1.1"
    assert [event[1] for event in mocked_update if event[0] == "coordinate"] == ["prepare-update", "finish-update"]
    assert config.read_text() == "settings and enabled intent"
    assert (sandbox.state / "packages/Nightr-0.1.0-universal.dmg").exists()

def test_failed_update_rolls_back_verified_prior_package(sandbox, mocked_update, monkeypatch):
    attempts = []
    def install(package):
        attempts.append(package)
        if len(attempts) == 1: raise installer.InstallError("simulated interruption")
    monkeypatch.setattr(installer, "install_pkg", install)
    with pytest.raises(installer.InstallError, match="previous signed version was restored"):
        installer.perform("update", manifest("0.1.1"), sandbox.root)
    assert len(attempts) == 2
    assert installer.read_record()["version"] == "0.1.0"
    assert ("coordinate", "finish-update") in mocked_update

def test_failed_rollback_reports_partial_failure_preserving_owner(sandbox, mocked_update, monkeypatch):
    monkeypatch.setattr(installer, "install_pkg", lambda p: (_ for _ in ()).throw(installer.InstallError("disk failure")))
    with pytest.raises(installer.InstallError, match="Update and rollback failed"):
        installer.perform("update", manifest("0.1.1"), sandbox.root)
    assert installer.read_record()["version"] == "0.1.0"
    assert ("coordinate", "finish-update") not in mocked_update

def test_tampered_rollback_prevents_stopping_or_installing(sandbox, mocked_update):
    (sandbox.state / "packages/Nightr-0.1.0-universal.dmg").write_bytes(b"tampered")
    with pytest.raises(installer.InstallError, match="rollback is unavailable"):
        installer.perform("update", manifest("0.1.1"), sandbox.root)
    assert ("coordinate", "prepare-update") not in mocked_update

def test_homebrew_update_coordinates_and_keeps_owner(sandbox, monkeypatch):
    sandbox.app.mkdir(parents=True)
    installer.write_record("homebrew", "0.1.0", "ABCDE12345")
    events = []
    monkeypatch.setattr(installer.shutil, "which", lambda _: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(installer, "verify_app", lambda *a, **k: "0.1.1")
    monkeypatch.setattr(installer, "verify_installed_root", lambda: None)
    monkeypatch.setattr(installer, "coordinate", lambda action: events.append(action))
    monkeypatch.setattr(installer, "run", lambda arguments, **kwargs: events.append(arguments))
    result = installer.perform("update", manifest("0.1.1"), sandbox.root)
    assert result["owner"] == "homebrew"
    assert events == ["prepare-update", ["/opt/homebrew/bin/brew", "upgrade", "--cask", "agenthangar/tap/nightr"], "finish-update"]
