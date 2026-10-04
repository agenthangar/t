"""Update offers preserve the original invocation and honor noninteractive use."""

import argparse
import io
from pathlib import Path
from types import SimpleNamespace

import pytest

from test_update import _brew_trees, _release_tree, git


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def terminal(t_mod, tmp_path, monkeypatch):
    for name in ("T_NO_UPDATE_CHECK", "T_UPDATE_PROMPTED", "CI", "CLAUDECODE", "CODEX_THREAD_ID"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(t_mod, "HOME", str(tmp_path))
    # Keep pytest's capture phase changes separate from the CLI's terminal.
    monkeypatch.setattr(t_mod, "sys", SimpleNamespace(**vars(t_mod.sys)))
    streams = [Terminal(), Terminal(), Terminal()]
    for name, stream in zip(("stdin", "stdout", "stderr"), streams):
        monkeypatch.setattr(t_mod.sys, name, stream)
    return streams


@pytest.fixture
def offer(t_mod, tmp_path, terminal, monkeypatch):
    installation = {"kind": "release", "source": str(tmp_path / "release"), "current": "v1.2.3"}
    calls = []
    helper = {
        "schedule": lambda source, executable: calls.append(("schedule", executable)),
        "cached": lambda source: {"available": True, "latest": "v1.2.4"},
        "snooze": lambda source: calls.append(("later", source)),
    }
    monkeypatch.setattr(t_mod, "_t_source_root", lambda: installation["source"])
    monkeypatch.setattr(t_mod, "_t_update_installation", lambda: installation)
    monkeypatch.setattr(t_mod, "_t_updates", lambda: helper)
    monkeypatch.setattr(t_mod, "cmd_update", lambda cfg, args: calls.append(("update", args)) or 0)
    return installation, calls, helper


def fail(*args, **kwargs):
    raise OSError("unavailable")


def test_installation_detects_release_and_homebrew(t_mod, tmp_path, monkeypatch):
    release = _release_tree(tmp_path)
    monkeypatch.setattr(t_mod, "_t_source_root", lambda: str(release))
    assert t_mod._t_update_installation() == {"kind": "release", "source": str(release), "current": "v1.2.3"}
    roots, opt, link, brew = _brew_trees(tmp_path)
    monkeypatch.setattr(t_mod, "_t_source_root", lambda: str(roots[0]))
    assert t_mod._t_update_installation() == {
        "kind": "brew", "source": str(opt), "current": "v1.2.3",
        "brew": str(brew), "formula": "agenthangar/tap/t",
    }
    link.unlink()
    assert t_mod._t_update_installation() is None


def test_git_installation_requires_clean_canonical_main(t_mod, tmp_path, monkeypatch):
    root = tmp_path / "t"
    git(tmp_path, "init", "-q", "-b", "main", str(root))
    git(root, "config", "user.email", "test@example.com")
    git(root, "config", "user.name", "Tester")
    git(root, "commit", "--allow-empty", "-qm", "seed")
    monkeypatch.setattr(t_mod, "_t_source_root", lambda: str(root))
    assert t_mod._t_update_installation() is None  # No update source.
    git(root, "remote", "add", "origin", str(tmp_path / "remote.git"))
    assert t_mod._t_update_installation()["current"] == git(root, "rev-parse", "HEAD")
    (root / "scratch").write_text("keep me")
    assert t_mod._t_update_installation() is None
    (root / "scratch").unlink()
    git(root, "checkout", "-qb", "feature")
    assert t_mod._t_update_installation() is None
    git(root, "checkout", "-q", "main")
    worktree = tmp_path / "worktree"
    git(root, "worktree", "add", "-qb", "dev/t-1", str(worktree))
    monkeypatch.setattr(t_mod, "_t_source_root", lambda: str(worktree))
    assert t_mod._t_update_installation() is None


@pytest.mark.parametrize("name", ["T_NO_UPDATE_CHECK", "CI", "CLAUDECODE", "CODEX_THREAD_ID"])
def test_suppressed_environments_never_offer(t_mod, terminal, monkeypatch, name):
    assert t_mod._t_update_interactive()
    monkeypatch.setenv(name, "1")
    monkeypatch.setattr(t_mod, "_t_update_installation", lambda: pytest.fail("must not inspect source"))
    assert t_mod._t_offer_update() == 0
    assert t_mod._t_auto_update(["ls"]) is None


def test_prompt_marker_only_applies_to_current_invocation(t_mod, terminal, monkeypatch):
    for pid in (t_mod.os.getpid(), t_mod.os.getppid()):
        monkeypatch.setenv("T_UPDATE_PROMPTED", str(pid))
        assert not t_mod._t_update_interactive()
    for stale in (str(t_mod.os.getpid() + t_mod.os.getppid() + 10000000), "old", ""):
        monkeypatch.setenv("T_UPDATE_PROMPTED", stale)
        assert t_mod._t_update_interactive()


@pytest.mark.parametrize("stream", ["stdin", "stdout", "stderr"])
def test_redirected_streams_never_prompt(t_mod, terminal, monkeypatch, stream):
    monkeypatch.setattr(t_mod.sys, stream, io.StringIO())
    assert not t_mod._t_update_interactive()


@pytest.mark.parametrize("answer", ["\n", "l\n", "later\n", "unexpected\n", ""])
def test_later_and_eof_snooze_without_installing(t_mod, terminal, offer, answer):
    installation, calls, helper = offer
    terminal[0].write(answer)
    terminal[0].seek(0)
    assert t_mod._t_offer_update() == 0
    assert calls == [("schedule", str(Path(installation["source"]) / "bin/t")), ("later", installation)]
    assert "v1.2.3 → v1.2.4" in terminal[2].getvalue()
    assert "Update now" in terminal[2].getvalue()


@pytest.mark.parametrize("answer", ["u\n", "YES\n", "1\n"])
def test_update_now_uses_existing_updater(t_mod, terminal, offer, answer):
    terminal[0].write(answer)
    terminal[0].seek(0)
    assert t_mod._t_offer_update() == 10
    args = offer[1][-1][1]
    assert vars(args) == {"dev": False, "relink": False, "check": False}
    assert t_mod.os.environ["T_UPDATE_PROMPTED"] == str(t_mod.os.getpid())
    assert "Continuing your command" in terminal[2].getvalue()


def test_update_failure_aborts_original_command(t_mod, terminal, offer, monkeypatch):
    terminal[0].write("u\n")
    terminal[0].seek(0)
    monkeypatch.setattr(t_mod, "cmd_update", lambda cfg, args: 1)
    monkeypatch.setattr(t_mod.os, "execv", lambda *args: pytest.fail("must not rerun after failure"))
    assert t_mod._t_auto_update(["ls"]) == 1
    assert "original command was not run" in terminal[2].getvalue()


def test_checker_failures_do_not_block_commands(t_mod, terminal, offer, monkeypatch):
    offer[2]["schedule"] = fail
    assert t_mod._t_offer_update() == 0
    offer[2]["schedule"] = lambda *args: None
    offer[2]["cached"] = lambda source: {"available": False}
    assert t_mod._t_offer_update() == 0
    offer[2]["cached"] = lambda source: {"available": True, "latest": "v1.2.4"}
    offer[2]["snooze"] = fail
    assert t_mod._t_offer_update() == 0
    monkeypatch.setattr(t_mod, "_t_update_installation", lambda: None)
    assert t_mod._t_offer_update() == 0
    assert t_mod._t_auto_update(["ls"]) is None


@pytest.mark.parametrize("argv", [[], ["help"], ["update"], ["on", "host", "t", "ls"],
                                      ["mcp"], ["__recovery"], ["app", "--dry-run"],
                                      ["ls", "--json"], ["config", "--dump"], ["ls", "--statusline"]])
def test_protocol_and_explicit_update_commands_skip_prompt(t_mod, terminal, monkeypatch, argv):
    monkeypatch.setattr(t_mod, "_t_update_installation", lambda: pytest.fail("must skip source detection"))
    assert t_mod._t_auto_update(argv) is None


@pytest.mark.parametrize("kind", ["git", "release", "brew"])
def test_success_reexecs_updated_cli_with_exact_arguments(t_mod, tmp_path, terminal, offer, monkeypatch, kind):
    installation = dict(offer[0], kind=kind)
    monkeypatch.setattr(t_mod, "_t_update_installation", lambda: installation)
    monkeypatch.setattr(t_mod, "_t_offer_update", lambda source: 10)
    calls = []
    monkeypatch.setattr(t_mod.os, "execv", lambda *args: calls.append(args))
    argv = ["open", "repo with spaces", "2", "--", "literal;$value"]
    assert t_mod._t_auto_update(argv) == 0
    executable = tmp_path / "bin/t" if kind == "release" else Path(installation["source"]) / "bin/t"
    assert calls == [(t_mod.sys.executable, [t_mod.sys.executable, str(executable), *argv])]
    assert t_mod.os.environ["T_UPDATE_PROMPTED"] == str(t_mod.os.getpid())


def test_reexec_failure_is_actionable(t_mod, terminal, offer, monkeypatch):
    monkeypatch.setattr(t_mod, "_t_offer_update", lambda source: 10)
    monkeypatch.setattr(t_mod.os, "execv", fail)
    assert t_mod._t_auto_update(["ls"]) == 1
    assert "could not continue" in terminal[2].getvalue()


def test_normal_command_continues_after_later(t_mod, terminal, offer):
    assert t_mod._t_auto_update(["ls"]) is None
    assert offer[1][-1][0] == "later"


def test_manual_check_uses_packaged_helper_without_installing(t_mod, tmp_path, monkeypatch, capsys):
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setattr(t_mod, "_t_source_root", lambda: str(root))
    helper = t_mod._t_updates()
    installation = {"kind": "release", "source": str(tmp_path / "release"), "current": "v1.2.3"}
    helper["check"].__globals__["_release"] = lambda identity: "v1.2.4"
    monkeypatch.setattr(t_mod, "_t_updates", lambda: helper)
    monkeypatch.setattr(t_mod, "_t_update_installation", lambda: installation)
    assert t_mod.cmd_update(None, argparse.Namespace(check=True)) == 0
    assert "v1.2.3 → v1.2.4" in capsys.readouterr().out
    helper["check"].__globals__["_release"] = lambda identity: ""
    assert t_mod.cmd_update(None, argparse.Namespace(check=True)) == 0
    assert "up to date" in capsys.readouterr().out
    helper["check"].__globals__["_release"] = fail
    assert t_mod.cmd_update(None, argparse.Namespace(check=True)) == 1
    assert "could not check" in capsys.readouterr().err
    monkeypatch.setattr(t_mod, "_t_update_installation", lambda: None)
    assert t_mod.cmd_update(None, argparse.Namespace(check=True)) == 1
    assert "requires a release" in capsys.readouterr().err
    monkeypatch.setattr(t_mod, "_t_check_update", fail)
    assert t_mod.cmd_update(None, argparse.Namespace(check=True)) == 1
    assert t_mod._t_update_label({"kind": "git"}, "123456789") == "1234567"


def test_main_help_and_internal_protocols_bypass_dispatch(t_mod, monkeypatch, capsys):
    monkeypatch.setattr(t_mod, "_t_auto_update", lambda argv: pytest.fail("help must not prompt"))
    for argv in ([], ["help"], ["repos"], ["cursor"], ["ls", "--help"]):
        assert t_mod.main(argv) == 0
    monkeypatch.setattr(t_mod, "_t_offer_update", lambda: 10)
    assert t_mod.main(["__update-prompt"]) == 10
    monkeypatch.setattr(t_mod, "_t_check_update", fail)
    assert t_mod.main(["__update-check"]) == 0
    assert t_mod.build_parser().parse_args(["update", "--check"]).check


@pytest.mark.parametrize("argv", [["ls"], ["cursor", "--ls"]])
def test_main_stops_dispatch_when_updater_takes_over(t_mod, monkeypatch, argv):
    monkeypatch.setattr(t_mod, "_t_auto_update", lambda words: 1)
    monkeypatch.setattr(t_mod, "Config", lambda: pytest.fail("must stop dispatch"))
    assert t_mod.main(argv) == 1
