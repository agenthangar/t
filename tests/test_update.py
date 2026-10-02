"""Canonical and session-worktree update behavior with disposable Git repositories."""

import argparse
import os
import pathlib
import subprocess
import pytest


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


def test_update_switches_between_canonical_and_dirty_dev(t_mod, tmp_path, monkeypatch):
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True)
    main = tmp_path / "t"
    subprocess.run(["git", "clone", "-q", str(remote), str(main)], check=True)
    git(main, "config", "user.email", "test@example.com")
    git(main, "config", "user.name", "Tester")
    git(main, "checkout", "-qb", "main")
    (main / ".t-install-version").write_text("1\n")
    (main / "install.sh").write_text("#!/bin/sh\nprintf '%s %s\\n' \"$(dirname \"$0\")\" \"${T_LINK_DEV:-0}\" >> \"$T_UPDATE_LOG\"\n")
    (main / "install.sh").chmod(0o755)
    (main / "bin").mkdir()
    (main / "bin" / "t").write_text("#!/bin/sh\n")
    git(main, "add", "-A")
    git(main, "commit", "-qm", "seed")
    git(main, "push", "-q", "-u", "origin", "main")
    dev = tmp_path / "dev"
    git(main, "worktree", "add", "-qb", "dev/t-1", str(dev))
    (dev / "dirty.txt").write_text("keep me")
    log = tmp_path / "selected.log"
    monkeypatch.setenv("T_UPDATE_LOG", str(log))
    monkeypatch.setattr(t_mod, "_t_source_root", lambda: str(dev))
    monkeypatch.chdir(dev)
    assert t_mod._t_canonical_root(str(dev)) == str(main)
    assert t_mod.cmd_update(None, argparse.Namespace(dev=True, relink=False)) == 0
    assert log.read_text().splitlines()[-1] == f"{dev} 1"
    assert t_mod.cmd_update(None, argparse.Namespace(dev=False, relink=True)) == 0
    assert log.read_text().splitlines()[-1] == f"{dev} 1"
    assert t_mod.cmd_update(None, argparse.Namespace(dev=False, relink=False)) == 0
    assert log.read_text().splitlines()[-1] == f"{main} 0"
    assert (dev / "dirty.txt").read_text() == "keep me"


def test_update_refuses_dirty_canonical_main(t_mod, tmp_path, monkeypatch, capsys):
    root = tmp_path / "t"
    git(root.parent, "init", "-q", "-b", "main", str(root))
    (root / "scratch").write_text("dirty")
    monkeypatch.setattr(t_mod, "_t_source_root", lambda: str(root))
    assert t_mod.cmd_update(None, argparse.Namespace(dev=False, relink=False)) == 1
    assert "local changes" in capsys.readouterr().err


def test_update_rejects_unrelated_dev_worktree(t_mod, tmp_path, monkeypatch, capsys):
    root = tmp_path / "t"
    git(root.parent, "init", "-q", "-b", "main", str(root))
    other = tmp_path / "other"
    git(other.parent, "init", "-q", "-b", "main", str(other))
    monkeypatch.setattr(t_mod, "_t_source_root", lambda: str(root))
    monkeypatch.chdir(other)
    assert t_mod.cmd_update(None, argparse.Namespace(dev=True, relink=False)) == 1
    assert "session worktree" in capsys.readouterr().err


def _release_tree(tmp_path, version="v1.2.3"):
    root = tmp_path / "custom" / "releases" / version
    (root / "scripts").mkdir(parents=True)
    (root / ".t-release-version").write_text(version + "\n")
    (root / ".t-install-version").write_text("1\n")
    (root / "install.sh").write_text(
        "#!/bin/sh\nprintf '%s,%s\\n' \"$T_LINKS_ONLY\" \"${T_LINK_DEV:-unset}\" >> \"$T_UPDATE_LOG\"\n")
    (root / "install.sh").chmod(0o755)
    return root


def test_release_marker_and_version(t_mod, tmp_path, monkeypatch, capsys):
    root = _release_tree(tmp_path)
    monkeypatch.setattr(t_mod, "_t_source_root", lambda: str(root))
    assert t_mod._t_release_version(str(root)) == "v1.2.3"
    with pytest.raises(SystemExit) as error:
        t_mod.build_parser().parse_args(["--version"])
    assert error.value.code == 0
    assert capsys.readouterr().out.strip() == "t v1.2.3"
    (root / ".t-install-version").write_text("2\n")
    assert t_mod._t_release_version(str(root)) is None
    (root / ".t-install-version").write_text("1\n")
    (root / ".git").mkdir()
    assert t_mod._t_release_version(str(root)) is None


def test_checkout_version_is_short_sha(t_mod, monkeypatch, capsys):
    monkeypatch.setattr(t_mod, "_t_release_version", lambda root: None)
    monkeypatch.setattr(t_mod, "_t_git", lambda root, *args: "abc1234")
    with pytest.raises(SystemExit) as error:
        t_mod.build_parser().parse_args(["--version"])
    assert error.value.code == 0
    assert capsys.readouterr().out.strip() == "t abc1234 (checkout)"


def test_release_update_and_relink_route_to_the_right_installer(t_mod, tmp_path, monkeypatch, capsys):
    root = _release_tree(tmp_path)
    updater = root / "scripts" / "install-release.py"
    updater.write_text(
        "import os, sys\n"
        "with open(os.environ['T_UPDATE_LOG'], 'a') as file: file.write('release updater\\n')\n"
        "sys.exit(17)\n")
    log = tmp_path / "update.log"
    monkeypatch.setenv("T_UPDATE_LOG", str(log))
    monkeypatch.setattr(t_mod, "_t_source_root", lambda: str(root))
    monkeypatch.setattr(t_mod, "_t_canonical_root", lambda source: pytest.fail("release must not inspect Git"))
    monkeypatch.setenv("T_LINK_DEV", "1")
    assert t_mod.cmd_update(None, argparse.Namespace(dev=False, relink=False)) == 17
    assert log.read_text() == "release updater\n"
    assert t_mod.cmd_update(None, argparse.Namespace(dev=False, relink=True)) == 0
    assert log.read_text().splitlines()[-1] == "1,unset"
    assert t_mod._install_relink() == 0
    assert log.read_text().splitlines()[-1] == "1,unset"
    assert t_mod.cmd_update(None, argparse.Namespace(dev=True, relink=False)) == 1
    assert "release install" in capsys.readouterr().err
    assert log.read_text().splitlines() == ["release updater", "1,unset", "1,unset"]


def test_release_update_missing_script_is_actionable(t_mod, tmp_path, monkeypatch, capsys):
    root = _release_tree(tmp_path)
    monkeypatch.setattr(t_mod, "_t_source_root", lambda: str(root))
    assert t_mod.cmd_update(None, argparse.Namespace(dev=False, relink=False)) == 1
    assert "release updater missing" in capsys.readouterr().err


def test_doctor_reports_archive_release_without_checkout_probe(t_mod, tmp_path, monkeypatch, capsys):
    root = _release_tree(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(t_mod, "HOME", str(home))
    monkeypatch.setattr(t_mod, "_t_source_root", lambda: str(root))
    monkeypatch.setattr(t_mod, "_t_canonical_root", lambda source: pytest.fail("release must not inspect Git"))
    monkeypatch.setattr(t_mod, "_install_probe", lambda: {
        name: {"version": None, "logged_in": None} for name in t_mod.INSTALL_AGENTS
    })
    monkeypatch.setattr(t_mod.shutil, "which", lambda name: None)
    def no_checkout_git(args, **kwargs):
        assert not (args[:2] == ["git", "-C"]), args
        return subprocess.CompletedProcess(args, 1, "", "")
    monkeypatch.setattr(t_mod.subprocess, "run", no_checkout_git)
    cfg = argparse.Namespace(repos={}, worktree_root=str(tmp_path / "worktrees"))
    assert t_mod.cmd_doctor(cfg, argparse.Namespace()) == 0
    output = capsys.readouterr().out
    assert "release        v1.2.3" in output
    assert "source type    versioned archive" in output
    assert "git checkout   unavailable" not in output
