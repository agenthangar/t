"""Checkout-free releases install safely and leave existing links on failure."""

import hashlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import subprocess
import tarfile

import pytest

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("t_release_installer", ROOT / "scripts/install-release.py")
release = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release)
build_spec = importlib.util.spec_from_file_location("t_release_builder", ROOT / "scripts/build-release.py")
builder = importlib.util.module_from_spec(build_spec)
build_spec.loader.exec_module(builder)


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("T_NO_MCP", "1")
    monkeypatch.setenv("T_NO_CODEX_HOOKS", "1")
    monkeypatch.setenv("T_NO_PERMISSIONS", "1")
    monkeypatch.setenv("T_NO_TRUST", "1")
    monkeypatch.setenv("TMUX", "")
    for name in ("T_LOCAL_RC", "T_INSTALL_DIR", "T_LINKS_ONLY", "T_LINK_DEV", "T_PERMISSIONS_DIR"):
        monkeypatch.delenv(name, raising=False)
    return home, tmp_path / "data/t"


def artifact(version="v1.2.3", changed=None, extra=None):
    changed = changed or {}
    files = {name: (ROOT / name).read_bytes() for name in release.REQUIRED if name != ".t-release-version"}
    files[".t-release-version"] = (version + "\n").encode()
    files.update(changed)
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as tar:
        for name, data in sorted(files.items()):
            info = tarfile.TarInfo("t/" + name)
            info.size = len(data)
            info.mode = 0o755 if os.access(ROOT / name, os.X_OK) else 0o644
            tar.addfile(info, io.BytesIO(data))
        if extra:
            tar.addfile(extra)
    data = stream.getvalue()
    digest = hashlib.sha256(data).hexdigest().encode() + b"  t.tar.gz\n"
    return data, digest


def test_release_installs_without_git_and_preserves_user_settings(sandbox):
    home, base = sandbox
    (home / ".zshrc").write_text("# mine\n")
    archive, sums = artifact()
    root = release.install(archive, sums, base, home)
    assert root == base / "releases/v1.2.3"
    assert not (root / ".git").exists()
    assert len([1 for _, dst in release.OWNED_LINKS if (home / dst).is_symlink()]) == 7
    assert (home / "bin/t").resolve() == root / "bin/t"
    assert (home / ".zshrc").read_text() == "# mine\n"
    assert (home / ".config/t/local.zsh").is_file()
    assert release.install(archive, sums, base, home) == root


def test_integrity_failure_and_unsafe_member_leave_home_unchanged(sandbox):
    home, base = sandbox
    archive, sums = artifact()
    with pytest.raises(ValueError, match="SHA256 mismatch"):
        release.install(archive + b"x", sums, base, home)
    assert not (home / "bin").exists()
    malicious = tarfile.TarInfo("t/../outside")
    malicious.size = 0
    bad, digest = artifact(extra=malicious)
    with pytest.raises(ValueError, match="unsafe release archive path"):
        release.install(bad, digest, base, home)
    assert not (home / "bin").exists()
    link = tarfile.TarInfo("t/escape")
    link.type = tarfile.SYMTYPE
    link.linkname = "/tmp/outside"
    bad, digest = artifact(extra=link)
    with pytest.raises(ValueError, match="link or special"):
        release.install(bad, digest, base, home)


def test_failed_update_restores_all_previous_links(sandbox, monkeypatch):
    home, base = sandbox
    old = release.install(*artifact("v1.2.3"), base, home)
    before = {dst: os.readlink(home / dst) for _, dst in release.OWNED_LINKS}
    monkeypatch.delenv("T_NO_PERMISSIONS")
    monkeypatch.setenv("T_PERMISSIONS_DIR", str(home / "missing-policy"))
    with pytest.raises(subprocess.CalledProcessError):
        release.install(*artifact("v1.2.4"), base, home)
    assert {dst: os.readlink(home / dst) for _, dst in release.OWNED_LINKS} == before
    assert (home / "bin/t").resolve() == old / "bin/t"
    assert not (base / "releases/v1.2.4").exists()


def test_corrupted_existing_version_is_retried_when_not_in_use(sandbox):
    home, base = sandbox
    archive, sums = artifact()
    root = base / "releases/v1.2.3"
    root.mkdir(parents=True)
    (root / ".t-release-version").write_text("bad\n")
    release.install(archive, sums, base, home)
    assert release.validate_tree(root) == "v1.2.3"
    assert any(base.glob("releases/.corrupt-v1.2.3-*"))
    (root / "zsh/config.zsh").write_text("corrupt\n")
    with pytest.raises(ValueError, match="still linked"):
        release.install(archive, sums, base, home)


def test_symlinked_install_base_never_replaces_an_active_version(sandbox):
    home, physical_base = sandbox
    physical_base.mkdir(parents=True)
    alias = physical_base.parent / "share-alias"
    alias.symlink_to(physical_base, target_is_directory=True)
    archive, sums = artifact()
    root = release.install(archive, sums, alias, home)
    assert root == physical_base / "releases/v1.2.3"
    assert release.in_use(alias / "releases/v1.2.3", home)
    (root / "zsh/config.zsh").write_text("corrupt\n")
    with pytest.raises(ValueError, match="still linked"):
        release.install(archive, sums, alias, home)
    assert (home / "bin/t").resolve() == root / "bin/t"


def test_custom_install_base_inferred_from_bundled_updater(sandbox, monkeypatch):
    home, base = sandbox
    root = release.install(*artifact(), base, home)
    spec = importlib.util.spec_from_file_location("bundled_updater", root / "scripts/install-release.py")
    bundled = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bundled)
    monkeypatch.delenv("T_INSTALL_DIR", raising=False)
    assert bundled.install_base() == base


def test_release_tree_beneath_unrelated_git_checkout_is_not_misidentified(sandbox):
    home, base = sandbox
    base.parent.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(base.parent)], check=True)
    root = release.install(*artifact(), base, home)
    assert (home / "bin/t").resolve() == root / "bin/t"
    assert not (base.parent / ".git/config").read_text().find("hooksPath") >= 0


def test_build_archive_is_deterministic_and_runtime_only(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    files = set(builder.EXACT)
    files.update(str(path.relative_to(ROOT)) for prefix in builder.PREFIXES
                 for path in (ROOT / prefix).rglob("*") if path.is_file())
    for name in files:
        destination = source / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ROOT / name, destination)
    (source / "private.txt").write_text("never ship\n")
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(["git", "-C", str(source), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(source), "-c", "user.name=T", "-c",
                    "user.email=t@example.invalid", "commit", "-qm", "source"], check=True)
    one = builder.build("v2.0.0", source, tmp_path / "one")
    two = builder.build("v2.0.0", source, tmp_path / "two")
    assert one.read_bytes() == two.read_bytes()
    with tarfile.open(one, "r:gz") as tar:
        names = {member.name for member in tar}
    assert "t/LICENSE" in names
    assert "t/libexec/vendor/mistune/LICENSE" in names
    assert "t/.t-release-version" in names
    assert "t/private.txt" not in names
    assert not any(name.startswith("t/tests/") or name.startswith("t/docs/") for name in names)


def test_main_downloads_selected_version_and_reports_network_error(sandbox, monkeypatch, capsys):
    home, base = sandbox
    archive, sums = artifact()
    urls = []

    def fetched(url):
        urls.append(url)
        return archive if url.endswith("t.tar.gz") else sums

    monkeypatch.setattr(release, "download", fetched)
    assert release.main(["--version", "v1.2.3", "--install-dir", str(base)]) == 0
    assert urls == [
        "https://github.com/agenthangar/t/releases/download/v1.2.3/t.tar.gz",
        "https://github.com/agenthangar/t/releases/download/v1.2.3/SHA256SUMS",
    ]
    urls.clear()
    assert release.main(["--install-dir", str(base)]) == 0
    assert urls[0].endswith("/releases/latest/download/t.tar.gz")

    def unavailable(url):
        raise OSError("offline")

    monkeypatch.setattr(release, "download", unavailable)
    assert release.main(["--install-dir", str(base)]) == 1
    assert "offline" in capsys.readouterr().err
