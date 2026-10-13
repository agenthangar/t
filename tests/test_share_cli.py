"""The reviewed root share command has a clear send and read boundary."""

import pytest
import json
import os
from pathlib import Path
import sqlite3
import subprocess


def test_share_parser_accepts_send_retry_and_read_forms(t_mod):
    parser = t_mod.build_parser()
    send = parser.parse_args(["share", "--to", "codex:abc", "--file", "-",
                              "--attach", "history.md", "--from", "claude:def",
                              "--host", "mini", "--json"])
    assert (send.verb, send.share_action, send.destination, send.file,
            send.attach, send.source, send.host, send.json) == (
                "share", None, "codex:abc", "-", ["history.md"],
                "claude:def", "mini", True)
    retry = parser.parse_args(["share", "--retry", "some-id", "--json"])
    assert retry.retry == "some-id" and retry.share_action is None
    targets = parser.parse_args(["share", "targets", "--host", "mini", "--json"])
    assert (targets.share_action, targets.host, targets.json) == ("targets", "mini", True)
    inbox = parser.parse_args(["share", "inbox", "--for", "cursor:ghi"])
    assert (inbox.share_action, inbox.recipient) == ("inbox", "cursor:ghi")
    read = parser.parse_args(["share", "read", "some-id", "--attachment", "history.md",
                              "--for", "codex:abc", "--host", "mini", "--json"])
    assert (read.share_action, read.message_id, read.attachment, read.recipient,
            read.host, read.json) == (
                "read", "some-id", "history.md", "codex:abc", "mini", True)
    for action in ("targets", "inbox", "read"):
        argv = ["share", "--host", "mini", "--json", action]
        if action == "read":
            argv.append("some-id")
        leading = parser.parse_args(argv)
        assert leading.host == "mini" and leading.json is True


@pytest.mark.parametrize("argv", [
    ["share", "send"], ["share", "targets", "--file", "x"],
    ["share", "inbox", "--to", "codex:abc"],
    ["share", "read", "id", "--retry", "other"],
])
def test_share_parser_rejects_cross_action_flags(t_mod, argv):
    with pytest.raises(SystemExit) as exc:
        t_mod.build_parser().parse_args(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize("argv", [
    ["share", "--json"],
    ["share", "--retry", "id", "--to", "codex:abc"],
    ["share", "--retry", "id", "--file", "summary.md"],
    ["share", "--to", "codex:abc", "--file", "summary.md", "targets"],
])
def test_share_main_rejects_incomplete_or_mixed_send_forms(t_mod, argv):
    with pytest.raises(SystemExit) as exc:
        t_mod.main(argv)
    assert exc.value.code == 2


@pytest.mark.parametrize("argv", [
    ["share"], ["share", "--to", "claude"],
    ["share", "--to", "codex:abc"], ["share", "--file", "summary.md"],
    ["share", "--from", "codex", "--to", "claude"],
    ["share", "--to", "claude", "--json"],
])
def test_guided_share_forms_reach_handler_without_requiring_file(t_mod, monkeypatch, argv):
    monkeypatch.setattr(t_mod.sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(t_mod.sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr(t_mod, "Config", lambda: object())
    monkeypatch.setattr(t_mod, "_t_update_interactive", lambda: pytest.fail("share checked updates"))
    seen = []
    monkeypatch.setitem(t_mod.IMPLEMENTED, "share", lambda cfg, args: seen.append(args) or 0)
    assert t_mod.main(argv) == 0
    assert len(seen) == 1


def test_bare_share_help_has_no_side_effects(t_mod, monkeypatch, capsys):
    monkeypatch.setattr(t_mod, "Config", lambda: pytest.fail("help loaded configuration"))
    monkeypatch.setattr(t_mod, "_t_auto_update", lambda argv: pytest.fail("help checked updates"))
    assert t_mod.main(["share"]) == 0
    menu = capsys.readouterr().out
    assert all(word in menu for word in ("--to", "--file", "targets", "inbox", "read"))
    assert t_mod.main(["share", "--help"]) == 0
    assert capsys.readouterr().out == menu
    assert t_mod.main(["help", "share", "read"]) == 0
    assert "usage: t share read" in capsys.readouterr().out
    assert t_mod.main(["share", "--to", "codex:abc", "--help"]) == 0
    option_help = capsys.readouterr().out
    assert "usage: t share " in option_help
    assert "usage: t share codex:abc" not in option_help


def test_share_handler_delegates_to_bundled_module(t_mod, monkeypatch):
    seen = {}
    def fake_run_path(path):
        seen["path"] = path
        return {"run": lambda cfg, args, helpers: (cfg, args, helpers)}
    monkeypatch.setattr(t_mod.runpy, "run_path", fake_run_path)
    args = t_mod.build_parser().parse_args(["share", "targets"])
    cfg = object()
    result = t_mod.cmd_share(cfg, args)
    assert seen["path"].endswith("/libexec/t_share.py")
    assert result[0] is cfg and result[1] is args
    assert result[2]["Config"] is t_mod.Config


def test_internal_share_bypasses_configuration_and_update(t_mod, monkeypatch):
    monkeypatch.setattr(t_mod, "Config", lambda: pytest.fail("internal route loaded config"))
    monkeypatch.setattr(t_mod, "_t_auto_update", lambda argv: pytest.fail("internal route checked update"))
    monkeypatch.setattr(t_mod.runpy, "run_path", lambda path: {"internal": lambda: 7})
    assert t_mod.main(["__share"]) == 7


def test_real_cli_shares_between_synthetic_conversations(tmp_path):
    root = Path(__file__).resolve().parent.parent
    home = tmp_path / "home"
    claude = tmp_path / "claude"
    codex = tmp_path / "codex"
    for path in (home, claude, codex):
        path.mkdir()
    source = "11111111-1111-4111-8111-111111111111"
    target = "22222222-2222-4222-8222-222222222222"
    transcript = claude / "projects" / "-synthetic" / (source + ".jsonl")
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({"type": "user", "cwd": "/tmp/synthetic-project",
                                      "message": {"content": "Synthetic source"}}) + "\n")
    db = sqlite3.connect(codex / "state_5.sqlite")
    db.execute("create table threads (id,cwd,source,name,title,first_user_message,archived)")
    db.execute("insert into threads values (?,?,?,?,?,?,?)",
               (target, "/tmp/synthetic-project", "cli", "Synthetic target", "", "", 1))
    db.commit()
    db.close()
    summary = tmp_path / "summary.md"
    summary.write_text("Synthetic summary\n")
    history = tmp_path / "history.md"
    history.write_text("Selected synthetic history\n")
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("COV_CORE_", "T_")) and key not in (
               "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "TMUX_PANE")}
    env.update(HOME=str(home), CLAUDE_CONFIG_DIR=str(claude), CODEX_HOME=str(codex),
               XDG_CONFIG_HOME=str(tmp_path / "config"), XDG_STATE_HOME=str(tmp_path / "state"),
               XDG_CACHE_HOME=str(tmp_path / "cache"), T_NO_UPDATE_CHECK="1", TMUX="")

    def call(*args):
        result = subprocess.run([str(root / "bin/t"), "share", *args], cwd=tmp_path,
                                env=env, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)["result"]

    targets = call("targets", "--json")
    assert {row["address"] for row in targets} == {f"claude:{source}", f"codex:{target}"}
    sent = call("--to", f"codex:{target}", "--from", f"claude:{source}",
                "--file", str(summary), "--attach", str(history), "--json")
    mid = sent["id"]
    assert sent["status"] == "stored"
    assert [row["id"] for row in call("inbox", "--for", f"codex:{target}", "--json")] == [mid]
    assert call("read", mid, "--for", f"codex:{target}", "--json")["summary"] == summary.read_text()
    assert call("read", mid, "--for", f"codex:{target}", "--attachment", "history.md",
                "--json")["text"] == history.read_text()
    assert call("--retry", mid, "--json")["id"] == mid


def test_real_cli_remote_protocol_with_separate_synthetic_homes(tmp_path):
    root = Path(__file__).resolve().parent.parent
    source_home = tmp_path / "source-home"
    remote_home = tmp_path / "remote-home"
    source_claude = tmp_path / "source-claude"
    remote_codex = tmp_path / "remote-codex"
    fakebin = tmp_path / "fakebin"
    for directory in (source_home, remote_home, source_claude, remote_codex, fakebin):
        directory.mkdir()
    source = "11111111-1111-4111-8111-111111111111"
    target = "22222222-2222-4222-8222-222222222222"
    transcript = source_claude / "projects" / "-synthetic" / (source + ".jsonl")
    transcript.parent.mkdir(parents=True)
    transcript.write_text(json.dumps({"type": "user", "cwd": "/tmp/synthetic-project",
                                      "message": {"content": "Synthetic source"}}) + "\n")
    db = sqlite3.connect(remote_codex / "state_5.sqlite")
    db.execute("create table threads (id,cwd,source,name,title,first_user_message,archived)")
    db.execute("insert into threads values (?,?,?,?,?,?,?)",
               (target, "/tmp/synthetic-project", "cli", "Synthetic target", "", "", 1))
    db.commit()
    db.close()
    config = tmp_path / "source-config" / "t" / "config.sh"
    config.parent.mkdir(parents=True)
    config.write_text("REMOTE_HOSTS[mini]=fake-target\n")
    summary = tmp_path / "summary.md"
    summary.write_text("Synthetic remote summary\n")
    ssh = fakebin / "ssh"
    ssh.write_text("""#!/usr/bin/env python3
import os
import subprocess
import sys
assert sys.argv[-2:] == ['fake-target', "zsh -lic 't __share'"]
env = os.environ.copy()
for key, value in {'HOME': 'T_FAKE_REMOTE_HOME', 'CLAUDE_CONFIG_DIR': 'T_FAKE_REMOTE_CLAUDE',
                   'CODEX_HOME': 'T_FAKE_REMOTE_CODEX', 'XDG_CONFIG_HOME': 'T_FAKE_REMOTE_CONFIG',
                   'XDG_STATE_HOME': 'T_FAKE_REMOTE_STATE', 'XDG_CACHE_HOME': 'T_FAKE_REMOTE_CACHE'}.items():
    env[key] = env[value]
result = subprocess.run([env['T_FAKE_T'], '__share'], input=sys.stdin.buffer.read(),
                        env=env, capture_output=True)
sys.stdout.buffer.write(b'login banner\\n' + result.stdout)
sys.stderr.buffer.write(result.stderr)
sys.exit(result.returncode)
""")
    ssh.chmod(0o755)
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("COV_CORE_", "T_")) and key not in (
               "CLAUDE_CODE_SESSION_ID", "CODEX_THREAD_ID", "TMUX_PANE")}
    env.update(HOME=str(source_home), CLAUDE_CONFIG_DIR=str(source_claude),
               CODEX_HOME=str(tmp_path / "empty-source-codex"),
               XDG_CONFIG_HOME=str(config.parent.parent), XDG_STATE_HOME=str(tmp_path / "source-state"),
               XDG_CACHE_HOME=str(tmp_path / "source-cache"), T_NO_UPDATE_CHECK="1", TMUX="",
               PATH=str(fakebin) + os.pathsep + env.get("PATH", ""), T_FAKE_T=str(root / "bin/t"),
               T_FAKE_REMOTE_HOME=str(remote_home),
               T_FAKE_REMOTE_CLAUDE=str(tmp_path / "empty-remote-claude"),
               T_FAKE_REMOTE_CODEX=str(remote_codex),
               T_FAKE_REMOTE_CONFIG=str(tmp_path / "remote-config"),
               T_FAKE_REMOTE_STATE=str(tmp_path / "remote-state"),
               T_FAKE_REMOTE_CACHE=str(tmp_path / "remote-cache"))

    def call(*args):
        result = subprocess.run([str(root / "bin/t"), "share", *args], cwd=tmp_path,
                                env=env, capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)["result"]

    assert [row["address"] for row in call("--host", "mini", "--json", "targets")] == [f"codex:{target}"]
    sent = call("--to", f"codex:{target}", "--from", f"claude:{source}",
                "--file", str(summary), "--host", "mini", "--json")
    mid = sent["id"]
    assert sent["host"] == "mini"
    assert [row["id"] for row in call("inbox", "--for", f"codex:{target}", "--host", "mini", "--json")] == [mid]
    assert call("read", mid, "--for", f"codex:{target}", "--host", "mini", "--json")["summary"] == summary.read_text()
    assert call("--retry", mid, "--json")["id"] == mid
    assert not (tmp_path / "source-state" / "t" / "share" / "inbox").exists()
    assert (tmp_path / "remote-state" / "t" / "share" / "inbox" / (mid + ".json")).is_file()
