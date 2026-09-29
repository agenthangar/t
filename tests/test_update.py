"""Canonical and session-worktree update behavior with disposable Git repositories."""

import argparse
import os
import pathlib
import subprocess


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
