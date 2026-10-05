"""Host lifecycle, local persistence, aliases, and the remote shell contract."""

import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def host_cli(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("COV_CORE_") and key not in ("T_LOCAL_RC", "ZDOTDIR")}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"),
               XDG_CACHE_HOME=str(home / ".cache"), XDG_STATE_HOME=str(home / ".local/state"),
               T_NO_UPDATE_CHECK="1", T_RECOVERY_DISABLE="1", T_AUTO_TRUST="0")

    def run(*words):
        return subprocess.run([sys.executable, str(ROOT / "bin/t"), *words], env=env,
                              capture_output=True, text=True, timeout=20)

    return run, home / ".config/t/local.zsh", home


def test_host_lifecycle_without_dotfiles(host_cli):
    run, local, home = host_cli
    assert "No remote hosts" in run("hosts", "list").stdout
    assert not local.exists()
    assert run("hosts", "--json").stdout == '{"default": null, "hosts": {}}\n'
    assert run("hosts", "add", "mini", "chris@mini.local", "--default").returncode == 0
    assert json.loads(run("hosts", "list", "--json").stdout) == {
        "hosts": {"mini": "chris@mini.local"}, "default": "chris@mini.local"}
    assert "default for session move" in run("hosts", "list").stdout
    shown = run("hosts", "show", "mini")
    assert shown.returncode == 0 and "SSH target: chris@mini.local" in shown.stdout
    assert "Default for session move: yes" in shown.stdout
    assert run("hosts", "edit", "mini", "studio").returncode == 0
    assert run("hosts", "default").stdout == "mini  studio\n"
    assert run("hosts", "default", "--clear").returncode == 0
    assert "No default" in run("hosts", "default").stdout
    assert run("hosts", "default", "mini").returncode == 0
    assert run("hosts", "delete", "mini").returncode == 0
    assert json.loads(run("hosts", "--json").stdout) == {"hosts": {}, "default": None}
    assert "unset 'REMOTE_HOSTS[mini]'" in local.read_text()
    assert not (home / ".zshrc").exists()


def test_host_edits_preserve_shell_ssh_and_other_settings(host_cli):
    run, local, home = host_cli
    local.parent.mkdir(parents=True)
    original = "# personal settings\nDEV_MODEL[codex]=example\nREMOTE_HOSTS[old]=old.local\nMINI_HOST=old.local\n"
    local.write_text(original)
    ssh = home / ".ssh/config"
    ssh.parent.mkdir()
    ssh.write_text("Host lab\n  HostName lab.example\n  Port 2222\n")
    assert run("hosts", "edit", "old", "new.local").returncode == 0
    assert local.read_text().startswith(original)
    assert "MINI_HOST=new.local" in local.read_text()
    assert run("hosts", "add", "lab", "lab").returncode == 0
    assert run("hosts", "rm", "old").returncode == 0
    assert "unset MINI_HOST" in local.read_text()
    assert json.loads(run("hosts", "--json").stdout)["hosts"] == {"lab": "lab"}
    assert ssh.read_text() == "Host lab\n  HostName lab.example\n  Port 2222\n"
    cache = (home / ".config/t/config.sh").read_text()
    assert "DEV_MODEL[codex]=example" in cache


@pytest.mark.parametrize("words", [
    ("add", "bad]key", "mini"), ("add", "__add__", "mini"),
    ("add", "mini", "bad target"), ("add", "mini", "$(touch nope)"),
    ("add", "mini", "--", "-oProxyCommand=id"),
    ("edit", "missing", "mini"), ("remove", "missing"),
    ("default", "missing"), ("show", "missing"),
])
def test_invalid_hosts_never_write(host_cli, words):
    run, local, _ = host_cli
    result = run("hosts", *words)
    assert result.returncode == 1, result.stderr
    assert "t hosts" in result.stderr and not local.exists()


def test_duplicate_and_unchanged_hosts(host_cli):
    run, local, _ = host_cli
    assert run("hosts", "add", "lab", "lab.local").returncode == 0
    before = local.read_bytes()
    assert run("hosts", "add", "lab", "other.local").returncode == 1
    assert local.read_bytes() == before
    result = run("hosts", "edit", "lab", "lab.local")
    assert result.returncode == 0 and "unchanged" in result.stdout
    assert local.read_bytes() == before


def test_default_rejects_alias_with_clear(host_cli):
    run, local, _ = host_cli
    assert run("hosts", "default", "lab", "--clear").returncode == 2
    assert not local.exists()


@pytest.mark.parametrize("words", [
    ("hosts",), ("hosts", "-h"), ("help", "hosts"), ("help", "hosts", "delete"),
    ("hosts", "rm", "--help"),
])
def test_host_alias_help_has_no_side_effects(t_mod, monkeypatch, capsys, words):
    monkeypatch.setattr(t_mod, "Config", lambda: pytest.fail("help loaded config"))
    monkeypatch.setattr(t_mod, "_t_auto_update", lambda argv: pytest.fail("help checked updates"))
    assert t_mod.main(list(words)) == 0
    assert "t hosts" in capsys.readouterr().out


@pytest.mark.parametrize("words", [("host",), ("host", "list"), ("host", "--help"), ("help", "host")])
def test_singular_host_is_not_a_command(t_mod, monkeypatch, capsys, words):
    monkeypatch.setattr(t_mod, "Config", lambda: pytest.fail("unknown command loaded config"))
    assert t_mod.main(list(words)) == 2
    assert "unknown command" in capsys.readouterr().err


def test_explicit_hosts_list_dispatches_the_list_action(t_mod, monkeypatch):
    seen = []
    monkeypatch.setattr(t_mod, "Config", lambda: object())
    monkeypatch.setattr(t_mod, "_t_auto_update", lambda argv: None)
    monkeypatch.setitem(t_mod.IMPLEMENTED, "hosts", lambda cfg, args: seen.append(args.action) or 0)
    assert t_mod.main(["hosts", "list"]) == 0
    assert seen == ["list"]


def test_host_change_updates_defaults_and_rejects_option_targets(t_mod):
    cfg = SimpleNamespace(hosts={"lab": "old"}, beam_host="old", mini_host="old")
    t_mod._host_change(cfg, "edit", "lab", "new")
    assert cfg.beam_host == cfg.mini_host == "new"
    t_mod._host_change(cfg, "add", "cloud", "u@cloud", default=True)
    assert cfg.beam_host == "u@cloud"
    t_mod._host_change(cfg, "default", "lab")
    assert cfg.beam_host == "new"
    t_mod._host_change(cfg, "remove", "lab")
    assert cfg.hosts == {"cloud": "u@cloud"} and not cfg.beam_host and not cfg.mini_host
    t_mod._host_change(cfg, "default", clear=True)
    for action, alias, target in (("add", "cloud", "other"), ("edit", "missing", "other"),
                                  ("add", "bad]", "other"), ("add", "x", "-option")):
        with pytest.raises(ValueError):
            t_mod._host_change(cfg, action, alias, target)


def test_host_refresh_reports_failure(t_mod, monkeypatch):
    monkeypatch.setattr(t_mod, "_run", lambda *a, **kw: subprocess.CompletedProcess([], 1, "", "bad config"))
    with pytest.raises(ValueError, match="bad config"):
        t_mod._host_refresh_config()


def test_plural_run_keeps_login_shell_and_remote_arguments(t_mod, monkeypatch):
    seen = []
    monkeypatch.setattr(t_mod, "Config", lambda: SimpleNamespace(hosts={"lab": "u@lab"}))
    monkeypatch.setattr(t_mod, "_t_auto_update", lambda argv: None)
    monkeypatch.setattr(t_mod, "_set_term_title", lambda title: None)
    monkeypatch.setattr(t_mod.os, "execvp", lambda exe, args: seen.append(args))
    assert t_mod.main(["hosts", "run", "lab", "echo", "two words", "--help"]) is None
    assert seen[0][:3] == ["ssh", "-t", "u@lab"]
    assert seen[0][3].startswith("zsh -lic ")
    assert shlex.split(seen[0][3])[2] == "T_UPDATE_PROMPTED=$$ echo 'two words' --help"
