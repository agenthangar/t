"""Package operations are verified with mocks; never install or change services."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import plistlib
import subprocess
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
    monkeypatch.setattr(installer.shutil, "which", lambda _: None)
    monkeypatch.setattr(installer, "current_process_identity", lambda: {"pid": os.getpid(), "uid": os.geteuid(), "processStartSeconds": 1, "processStartMicroseconds": 0, "bootID": "11111111-1111-4111-8111-111111111111"})
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
    sandbox.state.mkdir(parents=True, mode=0o700)
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
    def verify(path, team, version=None):
        events.append(("verify", version)); return version or "0.1.0"
    monkeypatch.setattr(installer, "verify_app", verify)
    monkeypatch.setattr(installer, "homebrew_receipt", lambda *a, **k: False)
    monkeypatch.setattr(installer, "global_installation_lock", lambda **kwargs: contextlib.nullcontext(True))
    monkeypatch.setattr(installer, "verify_installed_root", lambda: None)
    monkeypatch.setattr(installer, "verify_package", lambda *a: None)
    monkeypatch.setattr(installer, "check_owned_link", lambda: None)
    monkeypatch.setattr(installer, "coordinate", lambda action: events.append(("coordinate", action)))
    monkeypatch.setattr(installer, "finish_install_transaction", lambda transaction, team, **kwargs: events.append(("guard-" + kwargs.get("operation", "finish"), installer.read_record()["version"])))
    monkeypatch.setattr(installer, "run", lambda *a, **k: plistlib.dumps({"pkg-version": "0.1.1"}).decode())
    return events

def test_successful_update_coordinates_and_preserves_external_state(sandbox, mocked_update, monkeypatch):
    config = sandbox.state / "config.json"; config.write_text("settings and enabled intent")
    monkeypatch.setattr(installer, "install_pkg", lambda p: mocked_update.append(("install", str(p))))
    post = []
    result = installer.perform("update", manifest("0.1.1"), sandbox.root, resume_after_unlock=post)
    assert post == ["finish-update"]
    assert result["version"] == "0.1.1"
    assert [event[1] for event in mocked_update if event[0] == "coordinate"] == ["prepare-update"]
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
    assert ("coordinate", "finish-update") not in mocked_update

def test_failed_rollback_reports_partial_failure_preserving_owner(sandbox, mocked_update, monkeypatch):
    monkeypatch.setattr(installer, "install_pkg", lambda p: (_ for _ in ()).throw(installer.InstallError("disk failure")))
    with pytest.raises(installer.InstallError, match="Update could not roll back safely"):
        installer.perform("update", manifest("0.1.1"), sandbox.root)
    assert installer.read_record()["version"] == "0.1.0"
    assert ("coordinate", "finish-update") not in mocked_update

def test_tampered_rollback_prevents_stopping_or_installing(sandbox, mocked_update):
    (sandbox.state / "packages/Nightr-0.1.0-universal.dmg").write_bytes(b"tampered")
    with pytest.raises(installer.InstallError, match="rollback is unavailable"):
        installer.perform("update", manifest("0.1.1"), sandbox.root)
    assert ("coordinate", "prepare-update") not in mocked_update

def test_homebrew_update_coordinates_relinquishes_and_keeps_owner(sandbox, monkeypatch):
    sandbox.app.mkdir(parents=True)
    installer.write_record("homebrew", "0.1.0", "ABCDE12345")
    events = []
    monkeypatch.setattr(installer, "verify_app", lambda *a, **k: "0.1.0")
    monkeypatch.setattr(installer, "homebrew_receipt", lambda *a, **k: True)
    monkeypatch.setattr(installer, "verified_homebrew_contract", lambda *a, **k: "/opt/homebrew/bin/brew")
    monkeypatch.setattr(installer, "verify_installed_receipt", lambda team: "0.1.1")
    monkeypatch.setattr(installer, "global_installation_lock", lambda **kwargs: contextlib.nullcontext(True))
    monkeypatch.setattr(installer, "verify_installed_root", lambda: None)
    monkeypatch.setattr(installer, "assert_no_marker", lambda: events.append("no marker"))
    monkeypatch.setattr(installer, "coordinate", lambda action: events.append(action))
    monkeypatch.setattr(installer, "run_passthrough", lambda arguments, **kwargs: events.append(arguments))
    handle = installer.PackageLockHandle(-1, installer.GLOBAL_LOCK)
    monkeypatch.setattr(handle, "release", lambda: events.append("unlock update"))
    monkeypatch.setattr(handle, "acquire", lambda: events.append("lock update"))
    result = installer.perform("update", manifest("0.1.1"), sandbox.root, global_lock_held=handle)
    assert result["owner"] == "homebrew"
    assert events == ["prepare-update", "unlock update", ["/opt/homebrew/bin/brew", "upgrade", "--cask", "agenthangar/tap/nightr"], "lock update", "no marker"]


def test_main_resumes_only_after_update_lock_has_been_released(sandbox, monkeypatch):
    held = False
    events = []
    @contextlib.contextmanager
    def lock():
        nonlocal held
        held = True
        try: yield True
        finally: held = False; events.append("unlocked")
    def perform(*args, **kwargs):
        assert held
        events.append("metadata committed")
        kwargs["resume_after_unlock"].append("finish-update")
        return {"version": "0.1.1", "owner": "direct"}
    def coordinate(action):
        assert not held
        events.append(action)
    monkeypatch.setattr(installer, "installation_lock", lock)
    monkeypatch.setattr(installer, "latest_manifest", lambda _: manifest("0.1.1"))
    monkeypatch.setattr(installer, "perform", perform)
    monkeypatch.setattr(installer, "coordinate", coordinate)
    assert installer.main(["update", "--json"]) == 0
    assert events == ["metadata committed", "unlocked", "finish-update"]


def test_resume_failure_never_rolls_back_committed_new_version(sandbox, monkeypatch, capsys):
    calls = []
    def perform(*args, **kwargs):
        kwargs["resume_after_unlock"].append("finish-update")
        calls.append("committed")
        return {"version": "0.1.1", "owner": "direct"}
    def coordinate(action):
        calls.append(action); raise installer.InstallError("restart unavailable")
    monkeypatch.setattr(installer, "latest_manifest", lambda _: manifest("0.1.1"))
    monkeypatch.setattr(installer, "perform", perform)
    monkeypatch.setattr(installer, "coordinate", coordinate)
    assert installer.main(["update", "--json"]) == 1
    assert calls == ["committed", "finish-update"]
    assert "committed" in json.loads(capsys.readouterr().out)["error"]


def test_other_user_controller_prevents_replacement(sandbox, mocked_update, monkeypatch):
    @contextlib.contextmanager
    def occupied(**kwargs):
        raise installer.InstallError("Another user's controller is active")
        yield
    monkeypatch.setattr(installer, "global_installation_lock", occupied)
    monkeypatch.setattr(installer, "install_pkg", lambda _: pytest.fail("must preserve live controller's package"))
    post = []
    with pytest.raises(installer.InstallError, match="controller is active"):
        installer.perform("update", manifest("0.1.1"), sandbox.root, resume_after_unlock=post)
    assert post == ["finish-update"]
    assert installer.read_record()["version"] == "0.1.0"


def test_direct_update_refuses_detected_homebrew_owner(sandbox, mocked_update, monkeypatch):
    monkeypatch.setattr(installer, "homebrew_receipt", lambda *a, **k: True)
    with pytest.raises(installer.InstallError, match="Homebrew receipt"):
        installer.perform("update", manifest("0.1.1"), sandbox.root)
    assert ("coordinate", "prepare-update") not in mocked_update




def test_machine_lock_excludes_independent_descriptors_without_root_mutation(tmp_path, monkeypatch):
    """Use a synthetic root metadata view; exercise real OS flock in temp only."""
    path = tmp_path / "update.lock"; path.write_text(""); path.chmod(0o666)
    original_fstat = os.fstat
    monkeypatch.setattr(Path, "lstat", lambda *a, **k: SimpleNamespace(st_uid=0, st_mode=0o040755))
    def root_file_metadata(fd):
        info = original_fstat(fd)
        return SimpleNamespace(**{name: (0 if name == "st_uid" else getattr(info, name)) for name in dir(info) if name.startswith("st_")})
    monkeypatch.setattr(installer.os, "fstat", root_file_metadata)
    with installer.global_installation_lock(required=True, path=path):
        with pytest.raises(installer.InstallError, match="Another user's"):
            with installer.global_installation_lock(required=True, path=path): pass


def test_machine_lock_rejects_wrong_owner_mode_and_link_count(tmp_path, monkeypatch):
    path = tmp_path / "update.lock"; path.write_text("")
    monkeypatch.setattr(Path, "lstat", lambda *a, **k: SimpleNamespace(st_uid=0, st_mode=0o040755))
    original_fstat = os.fstat
    for owner, mode, links in [(501, 0o100666, 1), (0, 0o100600, 1), (0, 0o100666, 2)]:
        def metadata(fd, owner=owner, mode=mode, links=links):
            info = original_fstat(fd)
            overrides = {"st_uid": owner, "st_mode": mode, "st_nlink": links}
            return SimpleNamespace(**{name: overrides.get(name, getattr(info, name)) for name in dir(info) if name.startswith("st_")})
        monkeypatch.setattr(installer.os, "fstat", metadata)
        with pytest.raises(installer.InstallError, match="Unsafe machine package lock"):
            with installer.global_installation_lock(required=True, path=path): pass


def test_private_request_is_bound_to_process_uid_uuid_and_exact_package(sandbox):
    sandbox.state.mkdir(mode=0o700)
    transaction = "877dedda-245d-4c6c-83ab-f3932d3fc663"
    package = Path("/Volumes/VerifiedImage/Nightr.pkg")
    installer.write_install_request(transaction, package, "0.1.1", "0.1.0", "ABCDE12345")
    path = sandbox.state / "install-request.json"
    request = json.loads(path.read_text())
    assert request["owner"] == installer.current_process_identity()
    assert request["transaction"] == transaction
    assert request["packagePath"] == str(package)
    assert request["version"] == "0.1.1"
    assert path.stat().st_mode & 0o777 == 0o600


def test_marker_blocks_managed_transaction_before_quiescing(sandbox, monkeypatch):
    marker = sandbox.root / "root-marker.json"; marker.write_text("pending")
    monkeypatch.setattr(installer, "ROOT_MARKER", marker)
    monkeypatch.setattr(installer, "coordinate", lambda *a: pytest.fail("must not quiesce an unrelated transaction"))
    with pytest.raises(installer.InstallError, match="transaction is pending"):
        with installer.installation_lock(): pass


def test_committed_new_package_is_never_rolled_back_after_finish_failure(sandbox, mocked_update, monkeypatch):
    installs = []
    monkeypatch.setattr(installer, "install_pkg", lambda path: installs.append(path))
    monkeypatch.setattr(installer, "finish_install_transaction", lambda *a: (_ for _ in ()).throw(installer.InstallError("marker retained")))
    post = []
    with pytest.raises(installer.InstallError, match="were committed"):
        installer.perform("update", manifest("0.1.1"), sandbox.root, resume_after_unlock=post)
    assert len(installs) == 1
    assert installer.read_record()["version"] == "0.1.1"
    assert post == []


def test_runtime_homebrew_contract_requires_exact_audited_template(monkeypatch):
    monkeypatch.setattr(installer, "HOMEBREW_CONTRACT_VERIFIED", True)
    monkeypatch.setattr(installer.shutil, "which", lambda _: "/opt/homebrew/bin/brew")
    data = manifest("0.1.1")
    source = installer.AUDITED_CASK_TEMPLATE.replace("{{VERSION}}", data["version"]).replace("{{SHA256}}", data["asset"]["sha256"])
    monkeypatch.setattr(installer, "run", lambda arguments, **k: "agenthangar/tap\n" if arguments[1] == "tap" else source)
    assert installer.verified_homebrew_contract(data) == str(Path("/opt/homebrew/bin/brew").resolve())
    monkeypatch.setattr(installer, "run", lambda arguments, **k: "agenthangar/tap\n" if arguments[1] == "tap" else source + '\nuninstall quit: "ai.nightr.app"\n')
    with pytest.raises(installer.InstallError, match="unreviewed actions"):
        installer.verified_homebrew_contract(data)


def test_unknown_homebrew_contract_falls_back_only_for_fresh_install(monkeypatch):
    monkeypatch.setattr(installer, "HOMEBREW_CONTRACT_VERIFIED", False)
    assert installer.verified_homebrew_contract(manifest(), required=False) is None
    with pytest.raises(installer.InstallError, match="no approved Homebrew"):
        installer.verified_homebrew_contract(manifest())


def test_whole_file_posix_gate_excludes_another_process(tmp_path, monkeypatch):
    import subprocess
    import sys
    path = tmp_path / "update.lock"; path.write_text(""); path.chmod(0o666)
    original_fstat = os.fstat
    monkeypatch.setattr(installer, "GLOBAL_LOCK", path)
    monkeypatch.setattr(Path, "lstat", lambda *a, **k: SimpleNamespace(st_uid=0, st_mode=0o040755))
    def root_metadata(fd):
        info = original_fstat(fd)
        return SimpleNamespace(**{name: (0 if name == "st_uid" else getattr(info, name)) for name in dir(info) if name.startswith("st_")})
    monkeypatch.setattr(installer.os, "fstat", root_metadata)
    code = "import fcntl,os,sys; fd=os.open(sys.argv[1],os.O_RDWR);\ntry: fcntl.lockf(fd,fcntl.LOCK_EX|fcntl.LOCK_NB,0,0,os.SEEK_SET)\nexcept BlockingIOError: sys.exit(0)\nelse: sys.exit(1)"
    environment = {key: value for key, value in os.environ.items() if not key.startswith("COV_CORE_")}
    with installer.global_installation_lock(required=True, path=path):
        result = subprocess.run([sys.executable, "-c", code, str(path)], env=environment, check=False, timeout=5)
        assert result.returncode == 0


def test_generated_cask_template_matches_runtime_audit_contract():
    # Template file parity is checked in the upstream Nightr packaging suite.
    for action in ["uninstall", "quit", "launchctl", "pkgutil", "preflight", "postflight"]:
        assert action not in installer.AUDITED_CASK_TEMPLATE


@pytest.mark.skipif(installer.sys.platform != "darwin", reason="Darwin kernel identity API")
def test_darwin_process_identity_uses_kernel_birth_and_boot_session():
    identity = installer.current_process_identity()
    assert identity["pid"] == os.getpid()
    assert identity["uid"] == os.geteuid()
    assert identity["processStartSeconds"] > 0
    assert 0 <= identity["processStartMicroseconds"] < 1_000_000
    assert len(identity["bootID"]) == 36



@pytest.mark.parametrize("owned", [False, True])
def test_unknown_homebrew_query_never_adopts_or_replaces_existing_app(sandbox, monkeypatch, owned):
    sandbox.app.mkdir(parents=True)
    if owned: installer.write_record("direct", "0.1.0", "ABCDE12345", "a" * 64)
    monkeypatch.setattr(installer.shutil, "which", lambda _: "/broken/brew")
    monkeypatch.setattr(installer, "run", lambda *a, **k: (_ for _ in ()).throw(installer.InstallError("receipt unavailable")))
    monkeypatch.setattr(installer, "coordinate", lambda *a: pytest.fail("must not quiesce"))
    monkeypatch.setattr(installer, "install_pkg", lambda *a: pytest.fail("must not replace"))
    monkeypatch.setattr(installer, "write_record", lambda *a, **k: pytest.fail("must not adopt or change owner"))
    with pytest.raises(installer.InstallError, match="ownership could not be checked"):
        installer.perform("update", manifest("0.1.1"), sandbox.root)


def test_successful_empty_receipt_list_is_authoritative_absence(monkeypatch):
    monkeypatch.setattr(installer.shutil, "which", lambda _: "/brew")
    calls = []
    def query(arguments, **kwargs): calls.append(arguments); return "other-cask 1.0\n"
    monkeypatch.setattr(installer, "run", query)
    assert installer.homebrew_receipt() is False
    assert calls == [["/brew", "list", "--cask", "--versions"]]


def test_missing_brew_requires_known_direct_owner_and_absent_standard_receipts(monkeypatch):
    monkeypatch.setattr(installer.shutil, "which", lambda _: None)
    original = Path.lstat
    def attributes(path, *args, **kwargs):
        if str(path) in ("/opt/homebrew/Caskroom/nightr", "/usr/local/Caskroom/nightr"): raise FileNotFoundError
        return original(path, *args, **kwargs)
    monkeypatch.setattr(Path, "lstat", attributes)
    assert installer.homebrew_receipt(known_direct=True) is False
    with pytest.raises(installer.InstallError, match="ownership is unknown"):
        installer.homebrew_receipt()


def test_fresh_no_app_can_choose_direct_without_asserting_brew_receipt_absence(sandbox, monkeypatch):
    monkeypatch.setattr(installer, "homebrew_receipt", lambda *a, **k: pytest.fail("no existing app ownership claim is needed"))
    monkeypatch.setattr(installer, "download_verified_package", lambda *a: (_ for _ in ()).throw(installer.InstallError("direct release selected")))
    with pytest.raises(installer.InstallError, match="direct release selected"):
        installer.perform("install", manifest(), sandbox.root)


def test_pending_root_phase_prevents_starting_a_second_installer_for_rollback(sandbox, mocked_update, monkeypatch):
    installs = []
    def install(path): installs.append(path); raise installer.InstallError("client timed out while PackageKit may still be active")
    def guard(transaction, team, **kwargs):
        assert kwargs["operation"] == "rollback-ready"
        raise installer.InstallError("root phase is still pending; reboot required")
    monkeypatch.setattr(installer, "install_pkg", install)
    monkeypatch.setattr(installer, "finish_install_transaction", guard)
    post = []
    with pytest.raises(installer.InstallError, match="reboot"):
        installer.perform("update", manifest("0.1.1"), sandbox.root, resume_after_unlock=post)
    assert len(installs) == 1
    assert installer.read_record()["version"] == "0.1.0"
    assert post == []


def test_explicit_manual_direct_claim_verifies_package_when_brew_is_absent(sandbox, monkeypatch):
    sandbox.state.mkdir(mode=0o700)
    contents = sandbox.app / "Contents"; contents.mkdir(parents=True)
    (contents / "Info.plist").write_bytes(plistlib.dumps({"CFBundleShortVersionString": "0.1.0"}))
    image = sandbox.root / "verified.dmg"; image.write_bytes(b"verified release image")
    mount = sandbox.root / "mounted"; mount.mkdir()
    package = mount / "Nightr.pkg"; package.write_bytes(b"verified installer")
    verified = []
    original_lstat = Path.lstat
    def attributes(path, *args, **kwargs):
        if str(path) in ("/opt/homebrew/Caskroom/nightr", "/usr/local/Caskroom/nightr"): raise FileNotFoundError
        return original_lstat(path, *args, **kwargs)
    @contextlib.contextmanager
    def mounted(path, directory): yield mount
    monkeypatch.setattr(Path, "lstat", attributes)
    monkeypatch.setattr(installer, "verify_installed_root", lambda: verified.append("root ownership"))
    monkeypatch.setattr(installer, "run", lambda *a, **k: plistlib.dumps({"pkg-version": "0.1.0"}).decode())
    monkeypatch.setattr(installer, "verify_app", lambda path, team, version=None: verified.append(("app", path, team, version)))
    monkeypatch.setattr(installer, "download_verified_package", lambda *a: image)
    monkeypatch.setattr(installer, "mounted_dmg", mounted)
    monkeypatch.setattr(installer, "verify_package", lambda path, team: verified.append(("package", path, team)))
    with pytest.raises(installer.InstallError, match="ownership is unknown"):
        installer.adopt_direct(manifest(), sandbox.root)
    assert verified == []
    assert not (sandbox.state / "installation.json").exists()
    result = installer.adopt_direct(manifest(), sandbox.root, explicit_claim=True)
    assert result["owner"] == "direct" and result["version"] == "0.1.0"
    assert result["artifactSHA256"] == installer._sha256(image)
    expected = ["root ownership", ("app", sandbox.app, "ABCDE12345", "0.1.0"),
                ("app", mount / "Nightr.app", "ABCDE12345", "0.1.0"),
                ("package", package, "ABCDE12345")]
    assert verified == expected
    verified.clear()
    assert installer.adopt_direct(manifest(), sandbox.root, explicit_claim=True)["owner"] == "direct"
    assert verified == expected


def test_explicit_direct_claim_preserves_known_homebrew_owner(sandbox, monkeypatch):
    installer.write_record("homebrew", "0.1.0", "ABCDE12345")
    monkeypatch.setattr(installer, "homebrew_receipt", lambda **kwargs: pytest.fail("existing owner already forbids conversion"))
    with pytest.raises(installer.InstallError, match="Homebrew installation ownership was preserved"):
        installer.adopt_direct(manifest(), sandbox.root, explicit_claim=True)
    assert installer.read_record()["owner"] == "homebrew"


def test_explicit_claim_never_bypasses_unknown_brew_query(sandbox, monkeypatch):
    monkeypatch.setattr(installer.shutil, "which", lambda _: "/broken/brew")
    monkeypatch.setattr(installer, "run", lambda *a, **k: (_ for _ in ()).throw(installer.InstallError("query failed")))
    monkeypatch.setattr(installer, "verify_installed_root", lambda: pytest.fail("ownership must be known first"))
    with pytest.raises(installer.InstallError, match="ownership could not be checked"):
        installer.adopt_direct(manifest(), sandbox.root, explicit_claim=True)
    assert not (sandbox.state / "installation.json").exists()


def test_main_adopt_direct_makes_only_the_explicit_claim(sandbox, monkeypatch):
    claims = []
    monkeypatch.setattr(installer, "latest_manifest", lambda _: manifest())
    def adopt(data, directory, *, explicit_claim=False):
        claims.append(explicit_claim); return {"owner": "direct", "version": "0.1.0"}
    monkeypatch.setattr(installer, "adopt_direct", adopt)
    assert installer.main(["adopt-direct", "--json"]) == 0
    assert claims == [True]


def test_download_enforces_transport_and_stream_size_before_acceptance(tmp_path, monkeypatch):
    requests = []
    def open_response(request, timeout):
        requests.append((request, timeout)); return io.BytesIO(b"signed image")
    monkeypatch.setattr(installer.urllib.request, "build_opener", lambda handler: SimpleNamespace(open=open_response))
    target = tmp_path / "image"
    assert installer.download("https://example.test/image", target, 12) == 12
    assert target.read_bytes() == b"signed image"
    assert requests[0][0].get_header("User-agent") == "Nightr/1"
    assert requests[0][1] == 30
    with pytest.raises(installer.InstallError, match="size limit"):
        installer.download("https://example.test/image", target, 11)
    with pytest.raises(installer.InstallError, match="require HTTPS"):
        installer.download("http://example.test/image", target, 12)
    assert len(requests) == 2
    request = installer.urllib.request.Request("https://example.test/image")
    handler = installer.HTTPSOnly()
    with pytest.raises(installer.InstallError, match="redirected away"):
        handler.redirect_request(request, None, 302, "Found", {}, "http://example.test/image")
    redirected = handler.redirect_request(request, None, 302, "Found", {}, "https://cdn.example.test/image")
    assert redirected.full_url == "https://cdn.example.test/image"


@pytest.mark.parametrize("passthrough", [False, True])
def test_subprocess_wrappers_bound_execution_preserve_policy_and_report_failure(monkeypatch, passthrough):
    calls = []
    def command(arguments, **kwargs):
        calls.append((arguments, kwargs)); return SimpleNamespace(stdout="out", stderr="err")
    monkeypatch.setattr(installer.subprocess, "run", command)
    function = installer.run_passthrough if passthrough else installer.run
    result = function([Path("/fixed/tool"), "argument with spaces"], timeout=7, env_updates={"HOMEBREW_NO_AUTO_UPDATE": "1"})
    arguments, options = calls[0]
    assert arguments == ["/fixed/tool", "argument with spaces"]
    assert options["timeout"] == 7 and options["check"] is True
    assert options["env"]["LC_ALL"] == "C" and options["env"]["HOMEBREW_NO_AUTO_UPDATE"] == "1"
    if passthrough:
        assert options["stdout"] is installer.sys.stderr and options["stderr"] is installer.sys.stderr
    else:
        assert result == "outerr" and options["capture_output"] and options["text"]
    for failure in [OSError("unavailable"), subprocess.TimeoutExpired("tool", 7)]:
        def fail(*args, **kwargs): raise failure
        monkeypatch.setattr(installer.subprocess, "run", fail)
        with pytest.raises(installer.InstallError, match="Command failed: /fixed/tool"):
            function(["/fixed/tool"], timeout=7)


@pytest.mark.parametrize("change", ["list", "schema", "owner", "path", "version", "team", "symlink"])
def test_installation_record_rejects_untrusted_or_incompatible_metadata(sandbox, change):
    installer.write_record("direct", "0.1.0", "ABCDE12345")
    path = sandbox.state / "installation.json"
    data = json.loads(path.read_text())
    if change == "symlink":
        target = sandbox.root / "foreign.json"; path.rename(target); path.symlink_to(target)
    else:
        mutations = {"schema": ("schemaVersion", 2), "owner": ("owner", "unknown"), "path": ("appPath", "/tmp/Other.app"), "version": ("version", "1.0.0"), "team": ("teamID", "untrusted")}
        if change == "list": data = []
        else: key, value = mutations[change]; data[key] = value
        path.write_text(json.dumps(data))
    with pytest.raises(installer.InstallError): installer.read_record()


def test_private_state_symlinks_never_redirect_writes_or_locks(sandbox):
    target = sandbox.root / "unrelated"; target.mkdir()
    sandbox.state.symlink_to(target, target_is_directory=True)
    with pytest.raises(installer.InstallError, match="Unsafe installation state"):
        installer.write_record("direct", "0.1.0", "ABCDE12345")
    with pytest.raises(installer.InstallError, match="Unsafe installation state"):
        with installer.installation_lock(): pass
    with pytest.raises(installer.InstallError, match="private owned"):
        installer.write_install_request("uuid", Path("/Volumes/image/Nightr.pkg"), "0.1.0", None, "ABCDE12345")
    assert list(target.iterdir()) == []


def test_lock_rejects_public_local_lock_and_unsafe_machine_parent(sandbox, monkeypatch):
    sandbox.state.mkdir(mode=0o700)
    local = sandbox.state / "package.lock"; local.write_text(""); local.chmod(0o644)
    with pytest.raises(installer.InstallError, match="Unsafe package lock"):
        with installer.installation_lock(): pass
    missing = sandbox.root / "absent.lock"
    with pytest.raises(installer.InstallError, match="machine package lock is missing"):
        with installer.global_installation_lock(required=True, path=missing): pass
    monkeypatch.setattr(Path, "lstat", lambda *args: SimpleNamespace(st_uid=501, st_mode=0o040755))
    with pytest.raises(installer.InstallError, match="lock directory"):
        with installer.global_installation_lock(path=local): pass


@pytest.mark.parametrize("variant", ["valid", "draft", "prerelease", "no manifest", "tag mismatch"])
def test_latest_release_only_accepts_matching_published_manifest(tmp_path, monkeypatch, variant):
    release = {"tag_name": "v0.1.0", "assets": [{"name": "nightr-release.json", "browser_download_url": f"https://github.com/{installer.REPOSITORY}/releases/download/v0.1.0/nightr-release.json"}]}
    if variant in ("draft", "prerelease"): release[variant] = True
    if variant == "no manifest": release["assets"] = []
    data = manifest("0.1.1" if variant == "tag mismatch" else "0.1.0")
    calls = []
    def acquire(url, path, maximum):
        calls.append(url); path.write_text(json.dumps(release if url == installer.API else data))
    monkeypatch.setattr(installer, "download", acquire)
    if variant == "valid":
        assert installer.latest_manifest(tmp_path) == data
        assert len(calls) == 2
    else:
        with pytest.raises(installer.InstallError): installer.latest_manifest(tmp_path)
        assert len(calls) == (2 if variant == "tag mismatch" else 1)


def make_app(path, **overrides):
    contents = path / "Contents"; contents.mkdir(parents=True)
    info = {"CFBundleIdentifier": installer.BUNDLE_ID, "NightrTeamID": "ABCDE12345", "CFBundleShortVersionString": "0.1.0", **overrides}
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))


def test_app_verifies_nested_identities_and_propagates_signature_rejection(sandbox, monkeypatch):
    make_app(sandbox.app)
    calls = []
    monkeypatch.setattr(installer, "run", lambda arguments: calls.append(arguments))
    assert installer.verify_app(sandbox.app, "ABCDE12345", "0.1.0") == "0.1.0"
    assert len(calls) == 5
    for identifier, arguments in zip(["ai.nightr.app", "ai.nightr.controller", "ai.nightr.powerhelper", "ai.nightr.networkworker"], [calls[0], *calls[2:]]):
        assert f'identifier "{identifier}"' in arguments[arguments.index("-R") + 1]
        assert '"ABCDE12345"' in arguments[arguments.index("-R") + 1]
    assert calls[1][:4] == ["/usr/sbin/spctl", "--assess", "--type", "execute"]
    with pytest.raises(installer.InstallError, match="identity or version"):
        installer.verify_app(sandbox.app, "OTHER12345", "0.1.0")
    with pytest.raises(installer.InstallError, match="identity or version"):
        installer.verify_app(sandbox.app, "ABCDE12345", "0.1.1")
    monkeypatch.setattr(installer, "run", lambda *args: (_ for _ in ()).throw(installer.InstallError("signature rejected")))
    with pytest.raises(installer.InstallError, match="signature rejected"): installer.verify_app(sandbox.app, "ABCDE12345")
    with pytest.raises(installer.InstallError, match="missing"): installer.verify_app(sandbox.root / "missing", "ABCDE12345")


def test_package_requires_installer_certificate_and_notarization(tmp_path, monkeypatch):
    package = tmp_path / "Nightr.pkg"; package.write_bytes(b"package")
    calls = []
    def verify(arguments):
        calls.append(arguments); return "Developer ID Installer: Publisher (ABCDE12345)\n"
    monkeypatch.setattr(installer, "run", verify)
    installer.verify_package(package, "ABCDE12345")
    assert calls[1][0:4] == ["/usr/sbin/spctl", "--assess", "--type", "install"]
    calls.clear()
    with pytest.raises(installer.InstallError, match="release team"): installer.verify_package(package, "OTHER12345")
    assert len(calls) == 1
    link = tmp_path / "alias.pkg"; link.symlink_to(package)
    with pytest.raises(installer.InstallError, match="regular file"): installer.verify_package(link, "ABCDE12345")


@pytest.mark.parametrize("read_only", [True, False])
def test_read_only_mount_is_verified_and_detached_even_after_client_failure(tmp_path, monkeypatch, read_only):
    calls = []
    monkeypatch.setattr(Path, "lstat", lambda *args: SimpleNamespace(st_uid=0, st_mode=0o040755))
    monkeypatch.setattr(installer, "run", lambda arguments, **kwargs: calls.append((arguments, kwargs)))
    monkeypatch.setattr(installer.os.path, "ismount", lambda _: True)
    monkeypatch.setattr(installer.os, "statvfs", lambda _: SimpleNamespace(f_flag=1 if read_only else 0))
    with pytest.raises((installer.InstallError, RuntimeError)):
        with installer.mounted_dmg(tmp_path / "release.dmg", tmp_path) as mount:
            assert read_only and mount.parent == Path("/Volumes")
            raise RuntimeError("verification failed")
    assert "-readonly" in calls[0][0] and calls[0][1]["timeout"] == 60
    assert calls[-1][0][1] == "detach"
    monkeypatch.setattr(Path, "lstat", lambda *args: SimpleNamespace(st_uid=501, st_mode=0o040755))
    with pytest.raises(installer.InstallError, match="protected system mount"):
        with installer.mounted_dmg(tmp_path / "release.dmg", tmp_path): pass
    assert len(calls) == 2


def test_installed_root_validation_rejects_writable_or_replaced_trust_paths(sandbox, monkeypatch):
    unsafe = None
    def metadata(path): return SimpleNamespace(st_uid=0, st_mode=0o040777 if path == unsafe else 0o040755)
    monkeypatch.setattr(Path, "lstat", metadata)
    installer.verify_installed_root()
    unsafe = sandbox.app / "Contents"
    with pytest.raises(installer.InstallError, match="root-owned"): installer.verify_installed_root()
    unsafe = installer.INSTALL_GUARD
    with pytest.raises(installer.InstallError, match="installer guard"): installer.verify_installed_root()


def test_download_checksum_and_cache_are_bound_to_actual_bytes(sandbox, monkeypatch):
    sandbox.state.mkdir(mode=0o700)
    data = manifest(); data["asset"].update(size=12, sha256=installer.hashlib.sha256(b"signed image").hexdigest())
    def acquire(url, path, maximum): path.write_bytes(b"signed image"); return 12
    monkeypatch.setattr(installer, "download", acquire)
    image = installer.download_verified_package(data, sandbox.root)
    cached = installer.cache_artifact(image, "0.1.0")
    assert cached.read_bytes() == image.read_bytes()
    cached.unlink(); cached.symlink_to(image)
    with pytest.raises(installer.InstallError, match="Unsafe cached package"): installer.cache_artifact(image, "0.1.0")
    cache = sandbox.state / "packages"; cached.unlink(); cache.rmdir(); cache.symlink_to(sandbox.root)
    with pytest.raises(installer.InstallError, match="Unsafe package cache"): installer.cache_artifact(image, "0.1.0")


@pytest.mark.parametrize("failure", ["ownership", "receipt", "manifest", "valid"])
def test_adoption_of_older_release_binds_receipt_and_published_version(sandbox, monkeypatch, failure):
    sandbox.state.mkdir(mode=0o700); make_app(sandbox.app)
    monkeypatch.setattr(installer, "homebrew_receipt", lambda **kwargs: failure == "ownership")
    monkeypatch.setattr(installer, "verify_installed_root", lambda: None)
    monkeypatch.setattr(installer, "run", lambda *args: plistlib.dumps({"pkg-version": "0.1.9" if failure == "receipt" else "0.1.0"}).decode())
    monkeypatch.setattr(installer, "download", lambda url, path, maximum: path.write_text(json.dumps(manifest("0.1.9" if failure == "manifest" else "0.1.0"))))
    image = sandbox.root / "old.dmg"; image.write_bytes(b"old signed image")
    monkeypatch.setattr(installer, "download_verified_package", lambda *args: image)
    monkeypatch.setattr(installer, "mounted_dmg", lambda *args: contextlib.nullcontext(sandbox.root))
    monkeypatch.setattr(installer, "verify_app", lambda *args: None)
    monkeypatch.setattr(installer, "verify_package", lambda *args: None)
    if failure == "valid": assert installer.adopt_direct(manifest("0.1.1"), sandbox.root)["version"] == "0.1.0"
    else:
        with pytest.raises(installer.InstallError): installer.adopt_direct(manifest("0.1.1"), sandbox.root)
        assert installer.read_record() is None


@pytest.mark.parametrize("query", ["installed", "unreadable"])
def test_missing_brew_never_discards_receipt_evidence(monkeypatch, query):
    monkeypatch.setattr(installer.shutil, "which", lambda _: None)
    def receipt(path):
        if query == "unreadable": raise PermissionError("receipt unreadable")
        return SimpleNamespace()
    monkeypatch.setattr(Path, "lstat", receipt)
    if query == "installed": assert installer.homebrew_receipt(known_direct=True)
    else:
        with pytest.raises(installer.InstallError, match="ownership remains unknown"):
            installer.homebrew_receipt(known_direct=True)


@pytest.mark.parametrize("variant", ["missing tap", "wrong version", "query failure"])
def test_brew_contract_failure_never_authorizes_execution(monkeypatch, variant):
    monkeypatch.setattr(installer.shutil, "which", lambda _: "/brew")
    def query(arguments, **kwargs):
        if variant == "query failure": raise installer.InstallError("brew failed")
        if arguments[1] == "tap": return "other/tap" if variant == "missing tap" else "agenthangar/tap"
        return installer.AUDITED_CASK_TEMPLATE.replace("{{VERSION}}", "0.1.2").replace("{{SHA256}}", "a" * 64)
    monkeypatch.setattr(installer, "run", query)
    with pytest.raises(installer.InstallError): installer.verified_homebrew_contract(manifest())
    assert installer.verified_homebrew_contract(manifest(), required=False) is None


def test_receipt_verification_includes_root_guard_and_matching_app_version(sandbox, monkeypatch):
    calls = []
    monkeypatch.setattr(installer, "verify_app", lambda *args: "0.1.0")
    monkeypatch.setattr(installer, "verify_installed_root", lambda: calls.append("root"))
    def query(arguments):
        calls.append(arguments)
        return plistlib.dumps({"pkg-version": "0.1.0"}).decode()
    monkeypatch.setattr(installer, "run", query)
    assert installer.verify_installed_receipt("ABCDE12345") == "0.1.0"
    assert calls[0] == "root" and calls[1][-1] == installer.INSTALL_GUARD
    assert 'identifier "ai.nightr.installguard"' in calls[1][calls[1].index("-R") + 1]
    monkeypatch.setattr(installer, "run", lambda *args: plistlib.dumps({"pkg-version": "0.1.1"}).decode())
    with pytest.raises(installer.InstallError, match="receipt differs"): installer.verify_installed_receipt("ABCDE12345")


@pytest.mark.parametrize("outcome", ["failure", "interrupt", "marker", "receipt", "old version"])
def test_brew_every_exit_reacquires_and_verifies_before_adoption(sandbox, monkeypatch, outcome):
    events = []
    monkeypatch.setattr(installer, "verified_homebrew_contract", lambda *args: "/brew")
    monkeypatch.setattr(installer, "coordinate", lambda action: events.append(action))
    monkeypatch.setattr(installer, "global_installation_lock", lambda **kwargs: contextlib.nullcontext(True))
    handle = installer.PackageLockHandle(-1, installer.GLOBAL_LOCK)
    monkeypatch.setattr(handle, "release", lambda: events.append("released"))
    monkeypatch.setattr(handle, "acquire", lambda: events.append("reacquired"))
    def execute(*args, **kwargs):
        events.append("brew")
        if outcome == "failure": raise installer.InstallError("brew failed")
        if outcome == "interrupt": raise KeyboardInterrupt
    def marker():
        events.append("marker checked")
        if outcome == "marker": raise installer.InstallError("pending transaction")
    monkeypatch.setattr(installer, "run_passthrough", execute)
    monkeypatch.setattr(installer, "assert_no_marker", marker)
    monkeypatch.setattr(installer, "verify_installed_receipt", lambda team: "0.1.0" if outcome == "old version" else "0.1.1")
    monkeypatch.setattr(installer, "homebrew_receipt", lambda *args: outcome != "receipt")
    post = []
    with pytest.raises(installer.InstallError):
        installer.brew_transaction("update", manifest("0.1.1"), handle, post, True)
    assert events[:5] == ["prepare-update", "released", "brew", "reacquired", "marker checked"]
    if outcome in ("marker", "receipt", "interrupt"): assert post == []
    else: assert post == ["finish-update"]
    if outcome in ("marker", "receipt"): assert installer.read_record() is None


@pytest.mark.parametrize("owner,receipt", [("direct", "valid"), ("direct", "wrong"), ("direct", "wrong manifest"), ("homebrew", "valid"), ("homebrew", "missing")])
def test_stale_per_user_metadata_is_reconciled_from_actual_signed_package(sandbox, monkeypatch, owner, receipt):
    sandbox.state.mkdir(mode=0o700)
    record = installer.write_record(owner, "0.1.0", "ABCDE12345")
    monkeypatch.setattr(installer, "verify_app", lambda *args: "0.1.1")
    monkeypatch.setattr(installer, "verify_installed_root", lambda: None)
    monkeypatch.setattr(installer, "homebrew_receipt", lambda: receipt != "missing")
    monkeypatch.setattr(installer, "run", lambda *args: plistlib.dumps({"pkg-version": "0.1.0" if receipt == "wrong" else "0.1.1"}).decode())
    def download(url, path, maximum):
        data = manifest("0.1.1")
        if receipt == "wrong manifest": data["teamID"] = "OTHER12345"
        path.write_text(json.dumps(data))
    monkeypatch.setattr(installer, "download", download)
    image = sandbox.root / "installed.dmg"; image.write_bytes(b"actual signed image")
    monkeypatch.setattr(installer, "download_verified_package", lambda *args: image)
    monkeypatch.setattr(installer, "mounted_dmg", lambda *args: contextlib.nullcontext(sandbox.root))
    monkeypatch.setattr(installer, "verify_package", lambda *args: None)
    if receipt == "valid":
        result = installer.refreshed_record(record, manifest("0.1.2"), sandbox.root)
        assert result["version"] == "0.1.1" and result["owner"] == owner
        if owner == "direct": assert result["artifactSHA256"] == installer._sha256(image)
    else:
        with pytest.raises(installer.InstallError): installer.refreshed_record(record, manifest("0.1.2"), sandbox.root)
        assert installer.read_record() == record


@pytest.mark.parametrize("outcome", ["success", "noninteractive", "failure", "unsafe"])
def test_fixed_root_guard_finish_keeps_request_until_success(sandbox, monkeypatch, outcome):
    sandbox.state.mkdir(mode=0o700)
    request = sandbox.state / "install-request.json"; request.write_text("bound request")
    monkeypatch.setattr(Path, "lstat", lambda path: SimpleNamespace(st_uid=501 if outcome == "unsafe" else 0, st_mode=0o100755))
    verified = []
    monkeypatch.setattr(installer, "run", lambda args: verified.append(args))
    monkeypatch.setattr(installer.os, "geteuid", lambda: 501)
    monkeypatch.setattr(installer.sys, "stdin", SimpleNamespace(isatty=lambda: outcome != "noninteractive"))
    calls = []
    def execute(arguments, **kwargs):
        calls.append(arguments)
        if outcome == "failure": raise subprocess.TimeoutExpired(arguments[0], 30)
    monkeypatch.setattr(installer.subprocess, "run", execute)
    if outcome == "success":
        installer.finish_install_transaction("transaction", "ABCDE12345", operation="rollback-ready")
        assert request.exists()
        installer.finish_install_transaction("transaction", "ABCDE12345")
        assert not request.exists()
        assert calls[-1] == ["/usr/bin/sudo", "--", str(installer.INSTALL_GUARD), "finish", "transaction"]
    else:
        with pytest.raises(installer.InstallError): installer.finish_install_transaction("transaction", "ABCDE12345")
        assert request.exists()
        if outcome in ("unsafe", "noninteractive"): assert calls == []


def test_first_install_reacquires_new_machine_gate_before_committing(sandbox, monkeypatch):
    sandbox.state.mkdir(mode=0o700)
    image = sandbox.root / "image.dmg"; image.write_bytes(b"verified image")
    events = []
    monkeypatch.setattr(installer, "verified_homebrew_contract", lambda *args, **kwargs: None)
    monkeypatch.setattr(installer, "check_owned_link", lambda: None)
    monkeypatch.setattr(installer, "download_verified_package", lambda *args: image)
    monkeypatch.setattr(installer, "mounted_dmg", lambda *args: contextlib.nullcontext(sandbox.root))
    monkeypatch.setattr(installer, "verify_app", lambda *args: events.append("verify app"))
    monkeypatch.setattr(installer, "verify_package", lambda *args: None)
    monkeypatch.setattr(installer, "verify_installed_root", lambda: events.append("verify root"))
    monkeypatch.setattr(installer, "assert_no_marker", lambda: events.append("no marker"))
    monkeypatch.setattr(installer, "install_pkg", lambda *args: events.append("install"))
    @contextlib.contextmanager
    def machine(**kwargs):
        assert kwargs["required"]
        events.append("lock"); yield True; events.append("unlock")
    monkeypatch.setattr(installer, "global_installation_lock", machine)
    monkeypatch.setattr(installer, "run", lambda *args: plistlib.dumps({"pkg-version": "0.1.0"}).decode())
    assert installer.perform("install", manifest(), sandbox.root, global_lock_held=False)["version"] == "0.1.0"
    assert events == ["verify app", "install", "lock", "verify app", "verify root", "no marker", "unlock"]


def test_first_install_failure_leaves_no_owner_and_cannot_resume(sandbox, monkeypatch):
    sandbox.state.mkdir(mode=0o700)
    image = sandbox.root / "image.dmg"; image.write_bytes(b"verified image")
    monkeypatch.setattr(installer, "verified_homebrew_contract", lambda *args, **kwargs: None)
    monkeypatch.setattr(installer, "check_owned_link", lambda: None)
    monkeypatch.setattr(installer, "download_verified_package", lambda *args: image)
    monkeypatch.setattr(installer, "mounted_dmg", lambda *args: contextlib.nullcontext(sandbox.root))
    monkeypatch.setattr(installer, "verify_app", lambda *args: None)
    monkeypatch.setattr(installer, "verify_package", lambda *args: None)
    monkeypatch.setattr(installer, "global_installation_lock", lambda **kwargs: contextlib.nullcontext(True))
    monkeypatch.setattr(installer, "install_pkg", lambda *args: (_ for _ in ()).throw(installer.InstallError("PackageKit failed")))
    post = ["unexpected"]
    with pytest.raises(installer.InstallError, match="PackageKit failed"):
        installer.perform("install", manifest(), sandbox.root, global_lock_held=False, resume_after_unlock=post)
    assert post == [] and installer.read_record() is None


def test_installer_failure_is_reported_without_swallowing_timeout(monkeypatch):
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(installer.subprocess, "run", lambda *args, **kwargs: (_ for _ in ()).throw(subprocess.TimeoutExpired("installer", 180)))
    with pytest.raises(installer.InstallError, match="installation failed"): installer.install_pkg(Path("/Volumes/image/Nightr.pkg"))


def test_controller_coordination_invokes_only_fixed_installed_cli(sandbox, monkeypatch):
    calls = []
    monkeypatch.setattr(installer, "run", lambda args, **kwargs: calls.append((args, kwargs)))
    installer.coordinate("prepare-update")
    assert calls == [([sandbox.app / "Contents/MacOS/nightr-native", "package", "prepare-update"], {"timeout": 30})]


@pytest.mark.parametrize("action,variant", [("adopt-homebrew", "valid"), ("adopt-homebrew", "unavailable"), ("adopt-homebrew", "no receipt"), ("adopt-homebrew", "direct owner"), ("check", "not macOS"), ("install", "resume failure")])
def test_cli_reports_ownership_and_recovery_errors_as_json(sandbox, monkeypatch, capsys, action, variant):
    monkeypatch.setattr(installer, "latest_manifest", lambda *args: manifest())
    monkeypatch.setattr(installer, "verified_homebrew_contract", lambda *args: None if variant == "unavailable" else "/brew")
    monkeypatch.setattr(installer, "run", lambda *args: "" if variant == "no receipt" else "nightr 0.1.0")
    monkeypatch.setattr(installer, "verify_installed_receipt", lambda team: "0.1.0")
    if variant == "direct owner": installer.write_record("direct", "0.1.0", "ABCDE12345")
    if variant == "not macOS": monkeypatch.setattr(installer.sys, "platform", "linux")
    if variant == "resume failure":
        def perform(*args, **kwargs):
            kwargs["resume_after_unlock"].append("finish-update")
            raise installer.InstallError("failed before commit")
        monkeypatch.setattr(installer, "perform", perform)
        monkeypatch.setattr(installer, "coordinate", lambda *args: (_ for _ in ()).throw(installer.InstallError("could not restart")))
    result = installer.main([action, "--json"])
    output = json.loads(capsys.readouterr().out)
    assert result == (0 if variant == "valid" else 1)
    if variant == "valid": assert output["owner"] == "homebrew"
    else: assert "error" in output
    if variant == "resume failure": assert "failed before commit" in output["error"] and "could not restart" in output["error"]


def test_cli_human_error_and_status_remain_separate_from_progress(sandbox, monkeypatch, capsys):
    monkeypatch.setattr(installer, "latest_manifest", lambda *args: manifest())
    assert installer.main(["check"]) == 0
    output = capsys.readouterr()
    assert output.out.strip() == "Nightr not installed · unmanaged"
    assert "Checking Nightr release" in output.err
    monkeypatch.setattr(installer, "latest_manifest", lambda *args: (_ for _ in ()).throw(ValueError("malformed manifest")))
    assert installer.main(["check"]) == 1
    output = capsys.readouterr()
    assert not output.out and "nightr: malformed manifest" in output.err


def test_process_identity_fails_closed_on_missing_birth_or_boot(monkeypatch):
    """Exercise Darwin ABI handling on every host without querying a real kernel."""
    outcome = "valid"
    def process_info(pid, flavor, argument, pointer, size):
        info = pointer._obj
        info.pid = pid; info.uid = os.geteuid()
        info.startSeconds = 123; info.startMicroseconds = 456
        return 0 if outcome == "process failure" else size
    def boot_info(name, buffer, size, new, new_size):
        assert name == b"kern.bootsessionuuid"
        buffer.value = b"12345678-abcd-4abc-8abc-123456789abc"
        return -1 if outcome == "boot failure" else 0
    def library(name, **kwargs):
        return SimpleNamespace(proc_pidinfo=process_info) if name.endswith("libproc.dylib") else SimpleNamespace(sysctlbyname=boot_info)
    monkeypatch.setattr(installer.ctypes, "CDLL", library)
    assert installer.current_process_identity() == {"pid": os.getpid(), "uid": os.geteuid(), "processStartSeconds": 123,
                                                    "processStartMicroseconds": 456, "bootID": "12345678-ABCD-4ABC-8ABC-123456789ABC"}
    outcome = "process failure"
    with pytest.raises(installer.InstallError, match="process identity"): installer.current_process_identity()
    outcome = "boot failure"
    with pytest.raises(installer.InstallError, match="boot session"): installer.current_process_identity()


def test_state_uses_current_home_and_unrelated_public_command_is_preserved(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert installer.state_directory() == tmp_path / "Library/Application Support/Nightr"
    unrelated = tmp_path / "nightr"; unrelated.write_text("unrelated executable")
    original_path = installer.Path
    monkeypatch.setattr(installer, "Path", lambda value: unrelated if str(value) == "/usr/local/bin/nightr" else original_path(value))
    with pytest.raises(installer.InstallError, match="belongs to another installation"): installer.check_owned_link()
    assert unrelated.read_text() == "unrelated executable"
    unrelated.unlink(); unrelated.symlink_to("/Applications/Nightr.app/Contents/MacOS/nightr")
    installer.check_owned_link()


def test_fresh_install_delegates_to_brew_only_after_verified_contract(sandbox, monkeypatch):
    monkeypatch.setattr(installer, "verified_homebrew_contract", lambda *args, **kwargs: "/brew")
    calls = []
    def brew(mode, data, handle, post, existing):
        calls.append((mode, handle, existing)); return {"owner": "homebrew", "version": data["version"]}
    monkeypatch.setattr(installer, "brew_transaction", brew)
    assert installer.perform("install", manifest(), sandbox.root, global_lock_held=False)["owner"] == "homebrew"
    assert calls == [("install", False, False)]
