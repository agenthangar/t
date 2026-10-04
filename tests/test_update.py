"""Canonical and session-worktree update behavior with disposable Git repositories."""

import argparse
import json
import os
import pathlib
import subprocess
import pytest


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def local_checkout(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    for key, value in {"HOME": home, "XDG_CONFIG_HOME": home / ".config",
                       "XDG_CACHE_HOME": home / ".cache",
                       "XDG_STATE_HOME": home / ".local/state"}.items():
        monkeypatch.setenv(key, str(value))
    remote = tmp_path / "remote.git"
    git(tmp_path, "init", "--bare", "-q", str(remote))
    main = tmp_path / "local t"
    git(tmp_path, "clone", "-q", str(remote), str(main))
    git(main, "config", "user.email", "test@example.invalid")
    git(main, "config", "user.name", "Tester")
    git(main, "checkout", "-qb", "main")
    (main / ".t-install-version").write_text("1\n")
    (main / "t.plugin.zsh").write_text("# disposable\n")
    (main / "bin").mkdir()
    (main / "bin/t").write_text("#!/bin/sh\n")
    (main / "install.sh").write_text(
        '#!/bin/sh\n[ "$T_LINKS_ONLY" = 1 ] && [ -z "${T_LINK_DEV:-}" ] || exit 9\n'
        'mkdir -p "$HOME/bin"\nln -sfn "$(dirname "$0")/bin/t" "$HOME/bin/t"\n')
    (main / "install.sh").chmod(0o755)
    git(main, "add", "-A")
    git(main, "commit", "-qm", "seed")
    git(main, "push", "-q", "-u", "origin", "main")
    monkeypatch.chdir(tmp_path)
    return main, home


@pytest.mark.parametrize("source", ["brew", "release"])
@pytest.mark.parametrize("explicit", [False, True])
def test_update_local_switches_packaged_install_to_updated_main(
        t_mod, tmp_path, monkeypatch, local_checkout, source, explicit):
    main, home = local_checkout
    active = _brew_trees(tmp_path)[0][0] if source == "brew" else _release_tree(tmp_path)
    (home / "bin").mkdir()
    (home / "bin/t").symlink_to(active / "bin/t")
    monkeypatch.setattr(t_mod, "_t_source_root", lambda: str(active))
    # A newer remote commit must land before the user links switch sources.
    other = tmp_path / "other"
    git(tmp_path, "clone", "-q", "-b", "main", str(tmp_path / "remote.git"), str(other))
    (other / "new").write_text("merged fix")
    git(other, "add", "new")
    git(other, "-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-qm", "fix")
    git(other, "push", "-q", "origin", "main")
    cfg = argparse.Namespace(repos={} if explicit else {"t": str(main)})
    args = t_mod.build_parser().parse_args(["update", "--local"] + ([str(main)] if explicit else []))
    assert t_mod.cmd_update(cfg, args) == 0
    assert (main / "new").read_text() == "merged fix"
    assert (home / "bin/t").resolve() == main / "bin/t"
    assert active.exists()
    monkeypatch.setattr(t_mod, "_t_source_root", lambda: str(main))
    assert t_mod.cmd_update(cfg, t_mod.build_parser().parse_args(["update"])) == 0


@pytest.mark.parametrize("kind", ["dirty", "branch", "diverged"])
def test_update_local_preserves_links_when_checkout_is_unsafe(
        t_mod, tmp_path, monkeypatch, local_checkout, kind, capsys):
    main, home = local_checkout
    active = _release_tree(tmp_path)
    (home / "bin").mkdir()
    link = home / "bin/t"
    link.symlink_to(active / "bin/t")
    monkeypatch.setattr(t_mod, "_t_source_root", lambda: str(active))
    if kind == "branch":
        git(main, "checkout", "-qb", "cursor/feature")
    else:
        (main / "scratch").write_text("keep me")
        if kind == "diverged":
            git(main, "add", "scratch")
            git(main, "commit", "-qm", "local")
            git(main, "checkout", "-qb", "cursor/other", "HEAD~1")
            (main / "remote").write_text("remote")
            git(main, "add", "remote")
            git(main, "commit", "-qm", "remote")
            git(main, "push", "-q", "origin", "HEAD:main")
            git(main, "checkout", "-q", "main")
    args = t_mod.build_parser().parse_args(["update", "--local", str(main)])
    assert t_mod.cmd_update(None, args) != 0
    assert os.readlink(link) == str(active / "bin/t")
    expected = {"dirty": "local changes", "branch": "switch it to main", "diverged": "diverged"}
    assert expected[kind] in capsys.readouterr().err


def test_local_checkout_discovery_and_invalid_explicit_path(t_mod, tmp_path, monkeypatch, local_checkout):
    main, home = local_checkout
    dev = tmp_path / "dev"
    git(main, "worktree", "add", "-qb", "dev/t-1", str(dev))
    monkeypatch.chdir(dev)
    assert t_mod._t_local_checkout(None, "", "/missing") == str(main)
    monkeypatch.chdir(tmp_path)
    assert t_mod._t_local_checkout(None, "", str(dev)) == str(main)
    cfg = argparse.Namespace(repos={"t": str(main)})
    assert t_mod._t_local_checkout(cfg, "", "/missing") == str(main)
    assert t_mod._t_local_checkout(cfg, str(dev), "/missing") == str(main)
    invalid = tmp_path / "unrelated"
    git(tmp_path, "init", "-q", str(invalid))
    for marker in (None, b"2\n", b"\xff", b"1\n"):
        if marker is not None:
            (invalid / ".t-install-version").write_bytes(marker)
        with pytest.raises(ValueError, match="no valid local t checkout"):
            t_mod._t_local_checkout(cfg, str(invalid), str(main))
    with pytest.raises(ValueError, match="--local /path/to/t"):
        t_mod._t_local_checkout(cfg, str(tmp_path / "missing"), str(main))


@pytest.mark.parametrize("flag", ["--dev", "--relink", "--check"])
def test_local_update_flag_conflicts(t_mod, flag):
    with pytest.raises(SystemExit) as error:
        t_mod.build_parser().parse_args(["update", "--local", flag])
    assert error.value.code == 2


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


def _brew_trees(tmp_path):
    opt_link = tmp_path / "opt/t"
    opt_link.parent.mkdir()
    opt = opt_link / "libexec"
    brew = tmp_path / "bin/brew"
    brew.parent.mkdir()
    brew.write_text("#!/bin/sh\nprintf 'brew %s %s\\n' \"$1\" \"$2\" >> \"$T_UPDATE_LOG\"\n"
                    "rm \"$T_BREW_OPT_LINK\"\nln -s \"$T_BREW_NEXT\" \"$T_BREW_OPT_LINK\"\n")
    brew.chmod(0o755)
    roots = []
    for version in ("v1.2.3", "v1.2.4"):
        root = tmp_path / "Cellar/t" / version / "libexec"
        (root / "bin").mkdir(parents=True)
        (root / "bin/t").write_text("#!/bin/sh\n")
        (root / ".t-release-version").write_text(version + "\n")
        (root / ".t-install-version").write_text("1\n")
        (root / ".t-homebrew").write_text(json.dumps({
            "formula": "agenthangar/tap/t", "opt_libexec": str(opt), "brew": str(brew),
        }))
        (root / "install.sh").write_text(
            "#!/bin/sh\nprintf 'relink %s %s\\n' \"${T_LINKS_ONLY:-unset}\" "
            "\"${T_LINK_DEV:-unset}\" >> \"$T_UPDATE_LOG\"\n")
        (root / "install.sh").chmod(0o755)
        roots.append(root)
    opt_link.symlink_to(roots[0].parent, target_is_directory=True)
    return roots, opt, opt_link, brew


def test_homebrew_update_keeps_integrated_opt_links(t_mod, tmp_path, monkeypatch):
    roots, opt, opt_link, brew = _brew_trees(tmp_path)
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    (home / "bin/t").symlink_to(opt / "bin/t")
    log = tmp_path / "update.log"
    monkeypatch.setenv("T_UPDATE_LOG", str(log))
    monkeypatch.setenv("T_BREW_OPT_LINK", str(opt_link))
    monkeypatch.setenv("T_BREW_NEXT", str(roots[1].parent))
    monkeypatch.setattr(t_mod, "HOME", str(home))
    monkeypatch.setattr(t_mod, "_t_source_root", lambda: str(roots[0]))
    assert t_mod.cmd_update(None, argparse.Namespace(dev=False, relink=False)) == 0
    assert log.read_text().splitlines() == ["brew upgrade agenthangar/tap/t", "relink 1 unset"]
    assert os.readlink(home / "bin/t") == str(opt / "bin/t")
    assert (home / "bin/t").resolve() == roots[1] / "bin/t"
    monkeypatch.setattr(t_mod, "_t_source_root", lambda: str(roots[1]))
    assert t_mod.cmd_update(None, argparse.Namespace(dev=False, relink=True)) == 0
    assert log.read_text().splitlines()[-1] == "relink 1 unset"


def test_homebrew_unintegrated_update_does_not_create_links(t_mod, tmp_path, monkeypatch):
    roots, opt, opt_link, brew = _brew_trees(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    log = tmp_path / "update.log"
    monkeypatch.setenv("T_UPDATE_LOG", str(log))
    monkeypatch.setenv("T_BREW_OPT_LINK", str(opt_link))
    monkeypatch.setenv("T_BREW_NEXT", str(roots[1].parent))
    monkeypatch.setattr(t_mod, "HOME", str(home))
    monkeypatch.setattr(t_mod, "_t_source_root", lambda: str(roots[0]))
    assert t_mod.cmd_update(None, argparse.Namespace(dev=False, relink=False)) == 0
    assert log.read_text().splitlines() == ["brew upgrade agenthangar/tap/t"]
    assert not (home / "bin").exists()


def test_homebrew_integrate_is_explicit_and_marker_is_validated(t_mod, tmp_path, monkeypatch, capsys):
    roots, opt, opt_link, brew = _brew_trees(tmp_path)
    home = tmp_path / "home"
    home.mkdir()
    log = tmp_path / "update.log"
    monkeypatch.setenv("T_UPDATE_LOG", str(log))
    monkeypatch.setattr(t_mod, "HOME", str(home))
    monkeypatch.setattr(t_mod, "_t_source_root", lambda: str(roots[0]))
    assert t_mod.cmd_integrate(None, argparse.Namespace()) == 0
    assert log.read_text().splitlines() == ["relink unset unset"]
    assert t_mod.cmd_update(None, argparse.Namespace(dev=True, relink=False)) == 1
    assert "Homebrew owns" in capsys.readouterr().err
    opt_link.unlink()
    assert t_mod.cmd_integrate(None, argparse.Namespace()) == 1
    assert "invalid .t-homebrew marker" in capsys.readouterr().err


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


def test_integrate_runs_full_release_installer(t_mod, tmp_path, monkeypatch):
    root = _release_tree(tmp_path)
    (root / "install.sh").write_text(
        "#!/bin/sh\nprintf '%s,%s\\n' \"${T_LINKS_ONLY:-unset}\" "
        "\"${T_LINK_DEV:-unset}\" >> \"$T_UPDATE_LOG\"\n")
    log = tmp_path / "integrate.log"
    monkeypatch.setenv("T_UPDATE_LOG", str(log))
    monkeypatch.setenv("T_LINKS_ONLY", "1")
    monkeypatch.setenv("T_LINK_DEV", "1")
    monkeypatch.setattr(t_mod, "_t_source_root", lambda: str(root))
    assert t_mod.cmd_integrate(None, argparse.Namespace()) == 0
    assert log.read_text().strip() == "unset,unset"


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
