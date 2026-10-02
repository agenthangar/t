"""Local GitHub checkout and registration, using disposable homes and Git repos."""

import argparse
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


def git(*args, cwd=None):
    return subprocess.run(["git", *map(str, args)], cwd=cwd, check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def box(t_mod, tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    local = home / ".config" / "t" / "local.zsh"
    local.parent.mkdir(parents=True)
    local.write_text("# private settings stay here\n")
    monkeypatch.setattr(t_mod, "HOME", str(home))
    monkeypatch.setattr(t_mod, "CONFIG", str(home / ".config" / "t" / "config.sh"))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("T_LOCAL_RC", str(local))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.delenv("T_AUTO_TRUST", raising=False)
    monkeypatch.delenv("T_NO_TRUST", raising=False)
    monkeypatch.setattr(t_mod, "_new_sh_whence", lambda alias: None)
    monkeypatch.setattr(t_mod, "_trust_step", lambda dirs: None)
    monkeypatch.setattr(t_mod, "_trust_auto", lambda dirs, say=print: None)
    monkeypatch.setattr(t_mod, "zsh_capture", lambda snippet: "")
    return SimpleNamespace(repos={}), home, local


def args(url="https://github.com/owner/repo.git", alias=None, path=None, dry_run=False,
         instructions=False, no_instructions=False):
    return argparse.Namespace(url=url, alias=alias, path=path, dry_run=dry_run,
                              instructions=instructions, no_instructions=no_instructions)


@pytest.mark.parametrize("url", [
    "https://github.com/Owner/repo.git", "https://github.com/Owner/repo",
    "git@github.com:Owner/repo.git", "ssh://git@github.com/Owner/repo.git",
])
def test_checkout_accepts_standard_github_urls(t_mod, url):
    assert t_mod._checkout_repo(url) == ("Owner", "repo")


@pytest.mark.parametrize("url", [
    "https://evil.example/owner/repo", "https://github.com/owner/repo/other",
    "https://github.com/owner/repo?x=1", "https://github.com/owner/repo#fragment",
    "https://github.com/owner/repo.git -c core.sshCommand=evil",
    "git@github.com:--upload-pack/repo", "git@github.com:owner/-repo",
    "https://github.com/owner/.git", "https://github.com/owner/repo\n--config=x",
    "https://user@github.com/owner/repo", "https://github.com:443/owner/repo",
])
def test_checkout_rejects_nonstandard_or_unsafe_urls(t_mod, box, url, capsys):
    cfg, home, local = box
    assert t_mod._checkout_repo(url) is None
    assert t_mod.cmd_checkout(cfg, args(url=url)) == 1
    assert "expected a github.com" in capsys.readouterr().err
    assert not (home / "code").exists()
    assert local.read_text() == "# private settings stay here\n"


@pytest.mark.parametrize("options", [
    {"alias": "bad;command"}, {"alias": "$(command)"},
    {"path": 'repo";command'}, {"path": "repo$(command)"},
])
def test_checkout_rejects_shell_syntax_in_registration(t_mod, box, options):
    cfg, home, local = box
    assert t_mod.cmd_checkout(cfg, args(**options)) == 1
    assert local.read_text() == "# private settings stay here\n"
    assert not (home / "code").exists()


def test_checkout_dry_run_is_read_only(t_mod, box, capsys):
    cfg, home, local = box
    assert t_mod.cmd_checkout(cfg, args(dry_run=True)) == 0
    out = capsys.readouterr().out
    assert "git clone" in out and "DEV_REPOS[repo]" in out
    assert not (home / "code").exists()
    assert local.read_text() == "# private settings stay here\n"


def test_checkout_clones_registers_and_reuses_dirty_clone(t_mod, box, tmp_path, monkeypatch, capsys):
    cfg, home, local = box
    bare = tmp_path / "remote.git"
    git("init", "--bare", "-q", bare)
    seed = tmp_path / "seed"
    git("init", "-q", "-b", "main", seed)
    git("config", "user.email", "test@example.invalid", cwd=seed)
    git("config", "user.name", "Tester", cwd=seed)
    (seed / "README.md").write_text("hello\n")
    git("add", "README.md", cwd=seed)
    git("commit", "-qm", "seed", cwd=seed)
    git("remote", "add", "origin", bare, cwd=seed)
    git("push", "-q", "origin", "main", cwd=seed)
    git("symbolic-ref", "HEAD", "refs/heads/main", cwd=bare)
    original_run = t_mod._run
    clones = []

    def local_clone(cmd, **kwargs):
        if cmd[:3] == ["git", "clone", "-q"]:
            clones.append(cmd)
            result = original_run(["git", "clone", "-q", str(bare), cmd[-1]], **kwargs)
            if result.returncode == 0:
                git("remote", "set-url", "origin", cmd[-2], cwd=cmd[-1])
            return result
        return original_run(cmd, **kwargs)

    monkeypatch.setattr(t_mod, "_run", local_clone)
    destination = home / "code" / "repo"
    assert t_mod.cmd_checkout(cfg, args()) == 0
    assert len(clones) == 1 and clones[0][-2:] == [args().url, str(destination)]
    assert (destination / "README.md").read_text() == "hello\n"
    assert "DEV_REPOS[repo]" in local.read_text()
    dirty = destination / "dirty.txt"
    dirty.write_text("keep\n")
    assert t_mod.cmd_checkout(cfg, args()) == 0
    assert len(clones) == 1 and dirty.read_text() == "keep\n"
    assert local.read_text().count("DEV_REPOS[repo]") == 1
    assert "already set up" in capsys.readouterr().out


def test_checkout_rejects_conflicting_destination_and_alias(t_mod, box, capsys):
    cfg, home, local = box
    destination = home / "code" / "repo"
    destination.mkdir(parents=True)
    (destination / "keep.txt").write_text("keep")
    assert t_mod.cmd_checkout(cfg, args()) == 1
    assert "not a git repo" in capsys.readouterr().err
    assert (destination / "keep.txt").read_text() == "keep"
    local.write_text('DEV_REPOS[repo]="$HOME/code/other"\n')
    assert t_mod.cmd_checkout(cfg, args(path=str(home / "elsewhere"))) == 1
    assert "already maps" in capsys.readouterr().err
    assert not (home / "elsewhere").exists()


def test_checkout_rejects_same_basename_as_another_registered_repo(t_mod, box, tmp_path, capsys):
    cfg, home, local = box
    other = tmp_path / "first" / "repo"
    other.mkdir(parents=True)
    cfg.repos["first"] = str(other)
    wanted = tmp_path / "second" / "repo"
    assert t_mod.cmd_checkout(cfg, args(alias="second", path=str(wanted))) == 1
    assert "worktree slots use repository directory names" in capsys.readouterr().err
    assert not wanted.exists()
    assert local.read_text() == "# private settings stay here\n"
    # A second alias of the SAME checkout remains valid.
    assert t_mod._repo_basename_conflict(str(other), [str(other)]) is None


def test_checkout_custom_path_alias_and_non_main_warning(t_mod, box, tmp_path, capsys):
    cfg, home, local = box
    destination = tmp_path / "different"
    git("init", "-q", "-b", "master", destination)
    git("config", "user.email", "test@example.invalid", cwd=destination)
    git("config", "user.name", "Tester", cwd=destination)
    git("commit", "-qm", "seed", "--allow-empty", cwd=destination)
    git("remote", "add", "origin", "git@github.com:owner/repo.git", cwd=destination)
    assert t_mod.cmd_checkout(cfg, args(alias="other", path=str(destination))) == 0
    assert "DEV_REPOS[other]" in local.read_text()
    assert "origin/main is missing" in capsys.readouterr().err
    assert git("branch", "--show-current", cwd=destination) == "master"


def test_checkout_failed_clone_does_not_register(t_mod, box, monkeypatch, capsys):
    cfg, home, local = box

    def fail_clone(cmd, **kwargs):
        assert cmd[:3] == ["git", "clone", "-q"]
        return subprocess.CompletedProcess(cmd, 128, "", "repository not found")

    monkeypatch.setattr(t_mod, "_run", fail_clone)
    assert t_mod.cmd_checkout(cfg, args()) == 1
    assert "repository not found" in capsys.readouterr().err
    assert local.read_text() == "# private settings stay here\n"


def test_checkout_without_tty_skips_existing_instruction_profile(t_mod, box):
    cfg, home, local = box
    repo = home / "code" / "repo"
    git("init", "-q", "-b", "main", repo)
    git("remote", "add", "origin", "https://github.com/owner/repo.git", cwd=repo)
    (home / ".config" / "t" / "instructions.md").write_text("My preferences.\n")
    assert t_mod.cmd_checkout(cfg, args()) == 0
    assert not (repo / "AGENTS.md").exists()


@pytest.mark.skipif(not __import__("shutil").which("zsh"), reason="zsh required")
def test_shell_checkout_reloads_alias_immediately(tmp_path):
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    (home / "bin" / "t").symlink_to(ROOT / "bin" / "t")
    repo = home / "code" / "repo"
    git("init", "-q", "-b", "main", repo)
    git("config", "user.email", "test@example.invalid", cwd=repo)
    git("config", "user.name", "Tester", cwd=repo)
    git("commit", "-qm", "seed", "--allow-empty", cwd=repo)
    git("remote", "add", "origin", "https://github.com/owner/repo.git", cwd=repo)
    git("update-ref", "refs/remotes/origin/main", "HEAD", cwd=repo)
    env = {k: v for k, v in os.environ.items() if not k.startswith("T_")}
    env.update(HOME=str(home), ZDOTDIR=str(home),
               XDG_CONFIG_HOME=str(home / ".config"),
               XDG_CACHE_HOME=str(home / ".cache"),
               PATH=f"{home / 'bin'}:{os.environ['PATH']}")
    code = f'source "{ROOT / "t.plugin.zsh"}"; t checkout https://github.com/owner/repo.git; t cd repo; pwd'
    result = subprocess.run(["zsh", "-f", "-c", code], env=env,
                            capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == str(repo)


def test_instructions_profile_and_managed_block_preserve_custom_text(t_mod, box, capsys):
    cfg, home, local = box
    repo = home / "code" / "repo"
    repo.mkdir(parents=True)
    cfg.repos["repo"] = str(repo)
    profile = home / ".config" / "t" / "instructions.md"
    command = argparse.Namespace(repo=None, init=True, edit=False, show=False, apply=False)
    assert t_mod.cmd_instructions(cfg, command) == 0
    assert profile.is_file() and "## Model" in profile.read_text()
    profile.write_text("## Model\nPrefer GPT-6.\n\n## Preview\nInspect the local UI.\n")
    agents = repo / "AGENTS.md"
    agents.write_text("# Team policy\n\nDo not remove this.\n")
    command = argparse.Namespace(repo="repo", init=False, edit=False, show=False, apply=True)
    assert t_mod.cmd_instructions(cfg, command) == 0
    first = agents.read_text()
    assert first.startswith("# Team policy\n\nDo not remove this.\n")
    assert "Prefer GPT-6." in first and first.count(t_mod._INSTRUCTIONS_BEGIN) == 1
    assert t_mod.cmd_instructions(cfg, command) == 0
    assert agents.read_text() == first
    profile.write_text("## Model\nPrefer another model.\n")
    assert t_mod.cmd_instructions(cfg, command) == 0
    second = agents.read_text()
    assert second.startswith("# Team policy\n\nDo not remove this.\n")
    assert "Prefer another model." in second and "Prefer GPT-6." not in second
    assert second.count(t_mod._INSTRUCTIONS_BEGIN) == 1
    assert "= current" in capsys.readouterr().out


@pytest.mark.parametrize("broken", [
    "<!-- t instructions: begin -->\nmissing end\n",
    "<!-- t instructions: end -->\n",
    "<!-- t instructions: end -->\n<!-- t instructions: begin -->\n",
    "<!-- t instructions: begin -->\n<!-- t instructions: begin -->\n<!-- t instructions: end -->\n",
])
def test_instructions_refuse_malformed_markers(t_mod, box, broken):
    cfg, home, local = box
    repo = home / "code" / "repo"
    repo.mkdir(parents=True)
    agents = repo / "AGENTS.md"
    agents.write_text(broken)
    with pytest.raises(ValueError, match="markers"):
        t_mod._instructions_seed(str(repo), "Preferred model: codex\n")
    assert agents.read_text() == broken


def test_instructions_refuse_agents_symlink(t_mod, box, tmp_path):
    cfg, home, local = box
    repo = home / "code" / "repo"
    repo.mkdir(parents=True)
    outside = tmp_path / "outside.md"
    outside.write_text("private instructions\n")
    (repo / "AGENTS.md").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        t_mod._instructions_seed(str(repo), "New instructions\n")
    assert outside.read_text() == "private instructions\n"


def test_instructions_failed_atomic_replace_keeps_previous_file(t_mod, box, monkeypatch):
    cfg, home, local = box
    repo = home / "code" / "repo"
    repo.mkdir(parents=True)
    agents = repo / "AGENTS.md"
    agents.write_text("Keep this policy.\n")

    def fail_replace(src, dst):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(t_mod.os, "replace", fail_replace)
    with pytest.raises(OSError, match="read-only"):
        t_mod._instructions_seed(str(repo), "New preferences.\n")
    assert agents.read_text() == "Keep this policy.\n"
    assert not list(repo.glob(".t-agents-*"))


def test_checkout_explicit_instructions_preflight_and_skip(t_mod, box, tmp_path, capsys):
    cfg, home, local = box
    repo = home / "code" / "repo"
    git("init", "-q", "-b", "main", repo)
    git("remote", "add", "origin", "https://github.com/owner/repo.git", cwd=repo)
    assert t_mod.cmd_checkout(cfg, args(instructions=True)) == 1
    assert "t instructions --init" in capsys.readouterr().err
    assert not (repo / "AGENTS.md").exists()
    profile = home / ".config" / "t" / "instructions.md"
    profile.write_text("Prefer my chosen model.\n")
    assert t_mod.cmd_checkout(cfg, args(no_instructions=True)) == 0
    assert not (repo / "AGENTS.md").exists()
    assert t_mod.cmd_checkout(cfg, args(instructions=True)) == 0
    assert "Prefer my chosen model." in (repo / "AGENTS.md").read_text()


def test_interactive_offer_creates_profile_then_can_decline_or_apply(t_mod, box, tmp_path, monkeypatch):
    cfg, home, local = box
    repo = home / "code" / "repo"
    repo.mkdir(parents=True)
    monkeypatch.setattr(t_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(t_mod.sys.stdout, "isatty", lambda: True)
    answers = iter([True, False, False])  # create, do not edit, decline apply
    monkeypatch.setattr(t_mod, "_confirm", lambda prompt: next(answers))
    assert t_mod._instructions_offer([str(repo)], "ask", "t checkout") == 0
    assert (home / ".config" / "t" / "instructions.md").exists()
    assert not (repo / "AGENTS.md").exists()
    monkeypatch.setattr(t_mod, "_confirm", lambda prompt: True)
    assert t_mod._instructions_offer([str(repo)], "ask", "t checkout") == 0
    assert "## Local preview" in (repo / "AGENTS.md").read_text()


def test_custom_profile_path_and_invalid_template(t_mod, box, tmp_path, monkeypatch, capsys):
    cfg, home, local = box
    profile = tmp_path / "personal" / "workflow.md"
    monkeypatch.setenv("T_AGENT_INSTRUCTIONS", str(profile))
    assert t_mod._instructions_path() == str(profile)
    assert t_mod._instructions_init()
    assert not t_mod._instructions_init()  # never overwrite an edited profile
    profile.write_text("<!-- t instructions: begin -->\n")
    assert not t_mod._instructions_preflight("apply", "t checkout")
    assert "managed markers" in capsys.readouterr().err
    profile.write_text("## My model\nUse my preferred model.\n")
    assert t_mod._instructions_template(required=True) == profile.read_text()


def test_instructions_command_reports_missing_repo_and_bad_agents(t_mod, box, capsys):
    cfg, home, local = box
    profile = home / ".config" / "t" / "instructions.md"
    profile.write_text("Team preferences.\n")
    command = argparse.Namespace(repo="missing", init=False, edit=False, show=False, apply=True)
    assert t_mod.cmd_instructions(cfg, command) == 1
    assert "unknown registered repo" in capsys.readouterr().err
    repo = home / "code" / "repo"
    repo.mkdir(parents=True)
    cfg.repos["repo"] = str(repo)
    (repo / "AGENTS.md").write_text("<!-- t instructions: begin -->\n")
    command.repo = "repo"
    assert t_mod.cmd_instructions(cfg, command) == 1
    assert "malformed" in capsys.readouterr().err


def test_setup_explicit_instructions_seeds_selected_repo(t_mod, box, monkeypatch):
    cfg, home, local = box
    repo = home / "code" / "repo"
    git("init", "-q", "-b", "main", repo)
    profile = home / ".config" / "t" / "instructions.md"
    profile.write_text("Prefer my team's model.\n")
    cfg.hosts = {}
    cfg.worktree_root = str(home / "code" / ".worktrees")
    monkeypatch.setattr(t_mod, "_parse_ssh_hosts", lambda path: [])
    monkeypatch.setattr(t_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(t_mod.sys.stdout, "isatty", lambda: True)

    def select_repo(items, taken, trailer):
        for item in items:
            if item.get("kind") == "repo" and item.get("t") == "toggle":
                item["checked"] = True
        return True

    monkeypatch.setattr(t_mod, "_setup_wizard", select_repo)
    setup_args = argparse.Namespace(dirs=[str(home / "code")], dry_run=False,
                                    hosts=None, no_hosts=True, instructions=True,
                                    no_instructions=False)
    assert t_mod.cmd_setup(cfg, setup_args) == 0
    assert "DEV_REPOS[repo]" in local.read_text()
    assert "Prefer my team's model." in (repo / "AGENTS.md").read_text()


def test_setup_rejects_second_repo_with_same_worktree_slot_name(t_mod, box, tmp_path, monkeypatch, capsys):
    cfg, home, local = box
    existing = tmp_path / "existing" / "repo"
    existing.mkdir(parents=True)
    candidate = home / "code" / "repo"
    git("init", "-q", "-b", "main", candidate)
    cfg.repos["existing"] = str(existing)
    cfg.hosts = {}
    cfg.worktree_root = str(home / "code" / ".worktrees")
    monkeypatch.setattr(t_mod, "_parse_ssh_hosts", lambda path: [])
    monkeypatch.setattr(t_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(t_mod.sys.stdout, "isatty", lambda: True)

    def select_repo(items, taken, trailer):
        for item in items:
            if item.get("kind") == "repo" and item.get("t") == "toggle":
                item["checked"] = True
        return True

    monkeypatch.setattr(t_mod, "_setup_wizard", select_repo)
    setup_args = argparse.Namespace(dirs=[str(home / "code")], dry_run=False,
                                    hosts=None, no_hosts=True, instructions=False,
                                    no_instructions=True)
    assert t_mod.cmd_setup(cfg, setup_args) == 1
    assert "worktree slots would collide" in capsys.readouterr().err
    assert local.read_text() == "# private settings stay here\n"
