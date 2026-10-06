"""Bidirectional app command: identity, release ordering, and serialization."""

import os
from pathlib import Path
import shlex
import sqlite3
import subprocess

import pytest

from test_app import SID, app_slot, app_command


OTHER = "11234567-89ab-cdef-0123-456789abcdef"


@pytest.mark.parametrize("prefix", [["app"], ["session", "open-app"]])
@pytest.mark.parametrize("words,expected", [
    ([], ("push", None, None)), (["13"], ("push", "13", None)),
    (["api", "13"], ("push", "api", "13")),
    (["push", "api", "13"], ("push", "api", "13")),
    (["pull"], ("pull", None, None)), (["pull", "13"], ("pull", "13", None)),
    (["pull", "api", "13"], ("pull", "api", "13")),
])
def test_app_direction(t_mod, prefix, words, expected):
    args = t_mod.build_parser().parse_args([*prefix, *words])
    assert t_mod._app_target(args) == expected


def test_app_invalid_target(t_mod):
    args = t_mod.build_parser().parse_args(["app", "api", "13", "extra"])
    with pytest.raises(ValueError, match="usage"):
        t_mod._app_target(args)


@pytest.mark.parametrize("prefix", [["app"], ["session", "open-app"]])
def test_app_help_explains_how_to_return_to_cli(t_mod, monkeypatch, capsys, prefix):
    monkeypatch.setattr(t_mod, "Config", lambda: pytest.fail("help loaded configuration"))
    assert t_mod.main([*prefix, "--help"]) == 0
    output = " ".join(capsys.readouterr().out.split())
    assert "follow its release instructions if needed" in output
    assert "open handles the handoff" in output
    assert "t open <repo> <slot> --cli" in output


def test_app_without_repository_exposes_both_directions(t_mod, app_slot):
    cfg, row = app_slot
    with pytest.raises(ValueError) as error:
        t_mod._app_select(cfg, [row], None, None, "/unrelated")
    assert "t app [push|pull] <repo> <slot>" in str(error.value)
    assert "pull: desktop → CLI" in str(error.value)
    assert "t help app" in str(error.value)


@pytest.fixture
def pull_store(t_mod, app_slot, monkeypatch, tmp_path):
    cfg, row = app_slot
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("{}\n")
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    records = [f"{SID}\t{rollout}\t{row['cwd']}\tTitle\t0\t0\t{codex_home}"]
    monkeypatch.setattr(t_mod, "zsh_capture", lambda cmd: "\n".join(records))
    return cfg, row, records, rollout


def test_pull_selects_stamped_or_only_thread(t_mod, pull_store):
    cfg, row, records, rollout = pull_store
    for candidate in (row, dict(row, sid="-", state="app")):
        result = t_mod._app_pull_select(cfg, [candidate], "api", "13", "/")
        assert result["sid"] == SID
        assert result["expected_sid"] == candidate["sid"]
        assert result["rollout"] == str(rollout)
        assert result["archived"] is False
        assert result["codex_home"] == records[0].split("\t")[-1]


def test_pull_ambiguity_and_explicit_thread(t_mod, pull_store):
    cfg, row, records, rollout = pull_store
    codex_home = records[0].split("\t")[-1]
    records.append(f"{OTHER}\t{rollout}\t{row['cwd']}\tOther\t1\t0\t{codex_home}")
    app = dict(row, sid="-", state="app")
    with pytest.raises(ValueError, match="t app pull api 13 --thread"):
        t_mod._app_pull_select(cfg, [app], "api", "13", "/")
    assert t_mod._app_pull_select(cfg, [app], "api", "13", "/", OTHER)["sid"] == OTHER
    assert t_mod._app_pull_select(cfg, [row], "api", "13", "/")["sid"] == SID


def test_pull_selects_archived_stamped_thread_without_guessing(t_mod, pull_store):
    cfg, row, records, rollout = pull_store
    codex_home = records[0].split("\t")[-1]
    records[0] = f"{SID}\t{rollout}\t{row['cwd']}\tArchived\t5\t1\t{codex_home}"
    records.append(f"{OTHER}\t{rollout}\t{row['cwd']}\tActive sibling\t4\t0\t{codex_home}")
    result = t_mod._app_pull_select(cfg, [row], "api", "13", "/")
    assert (result["sid"], result["expected_sid"], result["rollout"],
            result["archived"], result["codex_home"]) == (
                SID, SID, str(rollout), True, codex_home)
    with pytest.raises(ValueError, match="several conversations"):
        t_mod._app_pull_select(cfg, [dict(row, sid="-")], "api", "13", "/")
    assert t_mod._app_pull_select(cfg, [dict(row, sid="-")], "api", "13", "/", SID)["archived"]


@pytest.mark.parametrize("bad_field", ["missing_rollout", "wrong_cwd", "bad_archived", "relative_home", "short_row"])
def test_pull_rejects_unverified_archived_rows(t_mod, pull_store, bad_field):
    cfg, row, records, rollout = pull_store
    fields = records[0].split("\t")
    fields[5] = "1"
    if bad_field == "missing_rollout":
        rollout.unlink()
    elif bad_field == "wrong_cwd":
        fields[2] = row["cwd"] + "/sibling"
    elif bad_field == "bad_archived":
        fields[5] = "maybe"
    elif bad_field == "relative_home":
        fields[6] = "relative/codex-home"
    else:
        fields.pop()
    records[0] = "\t".join(fields)
    with pytest.raises(ValueError, match="not a saved Codex conversation"):
        t_mod._app_pull_select(cfg, [row], "api", "13", "/")


def test_login_shell_seam_includes_only_exact_parent_threads_when_requested(tmp_path):
    codex_home = tmp_path / "alternate-codex-home"
    codex_home.mkdir()
    cwd = str(tmp_path / "work")
    sibling = cwd + "/sibling"
    with sqlite3.connect(codex_home / "state_5.sqlite") as db:
        db.execute("create table threads (id, rollout_path, cwd, name, title, updated_at, archived, source)")
        db.executemany("insert into threads values (?,?,?,?,?,?,?,?)", [
            (SID, "/rollout/archived", cwd, None, "Archived", 4, 1, "cli"),
            (OTHER, "/rollout/active", cwd, None, "Active", 3, 0, "cli"),
            ("21234567-89ab-cdef-0123-456789abcdef", "/rollout/helper", cwd,
             None, "Helper", 5, 0, '{"subagent":{}}'),
            ("31234567-89ab-cdef-0123-456789abcdef", "/rollout/sibling", sibling,
             None, "Sibling", 6, 0, "cli"),
        ])
    script = Path(__file__).parents[1] / "zsh" / "agent.zsh"
    env = {key: os.environ[key] for key in ("PATH", "TERM", "LANG", "LC_ALL") if key in os.environ}
    env.update(HOME=str(tmp_path), CODEX_HOME=str(codex_home),
               XDG_CONFIG_HOME=str(tmp_path / "config"), XDG_CACHE_HOME=str(tmp_path / "cache"),
               XDG_DATA_HOME=str(tmp_path / "data"), XDG_STATE_HOME=str(tmp_path / "state"))
    for path in ("config", "cache", "data", "state"):
        (tmp_path / path).mkdir()
    command = f"source {shlex.quote(str(script))}; _codex_threads_for_cwd {shlex.quote(cwd)}"
    active = subprocess.run(["zsh", "-f", "-c", command], env=env, capture_output=True,
                            text=True, check=True)
    archived = subprocess.run(["zsh", "-f", "-c", command + " --include-archived"],
                              env=env, capture_output=True, text=True, check=True)
    assert [line.split("\t")[0] for line in active.stdout.splitlines()] == [OTHER]
    rows = [line.split("\t") for line in archived.stdout.splitlines()]
    assert [(r[0], r[5], r[6]) for r in rows] == [
        (SID, "1", str(codex_home)), (OTHER, "0", str(codex_home))]


@pytest.mark.parametrize("problem", ["missing", "wrong_cwd", "bad_id", "malformed", "stale_stamp", "claude"])
def test_pull_refuses_unverified_conversations(t_mod, pull_store, problem):
    cfg, row, records, rollout = pull_store
    if problem == "missing":
        rollout.unlink()
    elif problem == "wrong_cwd":
        records[0] = f"{SID}\t{rollout}\t/other"
    elif problem == "bad_id":
        records[0] = f"bad-id\t{rollout}\t{row['cwd']}"
    elif problem == "malformed":
        records[:] = ["bad"]
    elif problem == "stale_stamp":
        row["sid"] = OTHER
    else:
        row["agent"] = "claude"
    with pytest.raises(ValueError):
        t_mod._app_pull_select(cfg, [row], "api", "13", "/")
    if problem not in ("claude", "stale_stamp"):
        with pytest.raises(ValueError, match="no saved"):
            t_mod._app_pull_select(cfg, [dict(row, sid="-")], "api", "13", "/")


def test_app_handoff_lock_serializes_aliases_and_releases(t_mod, app_slot):
    _, row = app_slot
    with t_mod._app_handoff_lock(row):
        with pytest.raises(ValueError, match="in progress"):
            with t_mod._app_handoff_lock(dict(row, slot="a-13")):
                pytest.fail("must not enter a concurrent handoff")
    with pytest.raises(ValueError, match="operation failed"):
        with t_mod._app_handoff_lock(row):
            raise ValueError("operation failed")
    with t_mod._app_handoff_lock(row):
        pass


def test_app_lock_io_error(t_mod, app_slot, monkeypatch):
    _, row = app_slot
    monkeypatch.setattr(t_mod.os, "makedirs", lambda *a, **kw: (_ for _ in ()).throw(OSError("denied")))
    with pytest.raises(ValueError, match="could not lock"):
        with t_mod._app_handoff_lock(row):
            pass


def test_pull_shell_uses_login_contract(t_mod, app_slot, monkeypatch):
    _, row = app_slot
    calls = []
    monkeypatch.setattr(t_mod, "_run", lambda argv, **kw: calls.append((argv, kw)))
    t_mod._app_pull_cli(dict(row, expected_sid="-", codex_home="/isolated codex"))
    assert calls[0][0][:2] == ["zsh", "-lic"]
    assert f"_t_app_pull_slot dev-api-13 {row['cwd']} {SID} -" in calls[0][0][-1]
    assert "'/isolated codex'" in calls[0][0][-1]


@pytest.mark.parametrize("owner,expected_pid", [("123", 123), ("", None), ("bad output", None)])
def test_pull_release_helper(t_mod, app_slot, monkeypatch, owner, expected_pid):
    _, row = app_slot
    row = dict(row, codex_home="/isolated codex", rollout="/saved.jsonl", archived=False)
    calls = []
    def capture(command):
        assert command == shlex.join(["_t_app_pull_owner", "dev-api-13", row["cwd"], SID, row["codex_home"]])
        return owner
    monkeypatch.setattr(t_mod, "zsh_capture", capture)
    def load(path):
        assert path.endswith("/libexec/t_app_handoff.py")
        return {"assert_released": lambda *args, **kwargs: calls.append((args, kwargs))}
    monkeypatch.setattr(t_mod.runpy, "run_path", load)
    t_mod._app_assert_released(row, "test.app")
    expected = dict(codex_home=row["codex_home"], rollout=row["rollout"],
                    archived=False, cli_pid=expected_pid)
    assert calls == [(("test.app", SID, row["cwd"], t_mod._run), expected)]
    t_mod._app_assert_released(row, "test.app", retry="Retry t open api 13 --cli")
    assert calls[-1][1] == dict(expected, retry="Retry t open api 13 --cli")
    monkeypatch.setattr(t_mod.runpy, "run_path", lambda path: (_ for _ in ()).throw(OSError("missing")))
    with pytest.raises(ValueError, match="desktop ownership"):
        t_mod._app_assert_released(row, None)


def test_pull_restore_helper_uses_exact_store_and_thread(t_mod, app_slot, monkeypatch):
    _, row = app_slot
    row = dict(row, codex_home="/isolated codex", rollout="/archive.jsonl")
    calls = []
    monkeypatch.setattr(t_mod.runpy, "run_path", lambda path: {
        "unarchive_thread": lambda *args, **kw: calls.append((args, kw))})
    t_mod._app_restore_archived(row)
    assert calls == [((SID, row["cwd"]), dict(codex_home="/isolated codex", rollout="/archive.jsonl"))]
    monkeypatch.setattr(t_mod.runpy, "run_path", lambda path: (_ for _ in ()).throw(OSError("missing")))
    with pytest.raises(ValueError, match="could not restore"):
        t_mod._app_restore_archived(row)


@pytest.mark.parametrize("opening,scenario", [
    (opening, scenario)
    for opening in (False, True)
    for scenario in ("success", "archived", "restore_failed", "dry", "running_app", "launch_failed", "linux", "missing_worktree")
    if not (opening and scenario == "dry")
])
def test_pull_command_orders_release_before_resume(t_mod, pull_store, monkeypatch, capsys, scenario, opening):
    cfg, row, records, _ = pull_store
    if scenario in ("archived", "restore_failed"):
        fields = records[0].split("\t")
        fields[5] = "1"
        records[0] = "\t".join(fields)
    rows = "\t".join(row[k] for k in ("sid", "cwd", "slot", "state", "context", "summary", "agent"))
    monkeypatch.setattr(t_mod, "zsh_capture", lambda cmd: rows if cmd == "_dev_session_rows" else "\n".join(records))
    monkeypatch.setattr(t_mod.sys, "platform", "linux" if scenario == "linux" else "darwin")
    monkeypatch.setattr(t_mod, "_app_bundle", lambda: "test.app")
    events = []
    def release(selected, bundle, *, retry=None):
        events.append("release")
        assert (retry is not None) is opening
        if scenario == "running_app":
            raise ValueError("quit the Codex desktop app on this Mac. " + (retry or "Retry t app pull"))
    def resume(selected):
        assert selected["sid"] == SID
        events.append("resume")
        return subprocess.CompletedProcess([], int(scenario == "launch_failed"), "", "failed; reservation retained")
    monkeypatch.setattr(t_mod, "_app_assert_released", release)
    def restore(selected):
        events.append("restore")
        assert selected["archived"] and selected["sid"] == SID
        if scenario == "restore_failed":
            raise ValueError("restore failed; reservation retained")
    monkeypatch.setattr(t_mod, "_app_restore_archived", restore)
    monkeypatch.setattr(t_mod, "_trust_auto", lambda dirs: events.append("trust"))
    monkeypatch.setattr(t_mod, "_app_pull_cli", resume)
    if scenario == "missing_worktree":
        os.rmdir(row["cwd"])
    args = t_mod.build_parser().parse_args(["app", "pull", "api", "13", *(["--dry-run"] if scenario == "dry" else [])])
    if opening:
        monkeypatch.setattr(t_mod, "Config", lambda: cfg)
        assert t_mod.main(["_app-open-cli", "a", "13"]) == (0 if scenario in ("success", "archived", "dry") else 1)
    else:
        assert t_mod.cmd_app(cfg, args) == (0 if scenario in ("success", "archived", "dry") else 1)
    assert events == (["release", "trust", "resume"] if scenario in ("success", "launch_failed")
                      else ["release", "restore", "trust", "resume"] if scenario == "archived"
                      else ["release", "restore"] if scenario == "restore_failed"
                      else ["release"] if scenario == "running_app" else [])
    output = capsys.readouterr()
    if opening:
        assert output.out == ("dev-api-13\n" if scenario in ("success", "archived") else "")
        if scenario == "running_app":
            assert "quit the Codex desktop app on this Mac" in output.err
            assert "Retry: t open a 13 --cli" in output.err
            assert "t app pull" not in output.err
    elif scenario == "success":
        assert "t open api 13 --cli" in output.out


@pytest.mark.parametrize("flags", [["--no-preview"], ["--no-plan"], ["--reuse-window"], ["--url", "https://example.com"], ["--plan", "/tmp/plan.md"]])
def test_pull_rejects_push_options(t_mod, app_slot, flags, capsys):
    cfg, _ = app_slot
    assert t_mod.cmd_app(cfg, t_mod.build_parser().parse_args(["app", "pull", "api", "13", *flags])) == 1
    assert "only to t app push" in capsys.readouterr().err


def test_explicit_push_matches_legacy(t_mod, app_command, app_slot, capsys):
    _, events, _ = app_command
    cfg, _ = app_slot
    assert t_mod.cmd_app(cfg, t_mod.build_parser().parse_args(["app", "push", "api", "13", "--no-plan"])) == 0
    assert [event[0] for event in events] == ["stop", "open"]
    assert t_mod.cmd_app(cfg, t_mod.build_parser().parse_args(["app", "push", "api", "13", "--thread", SID])) == 1
    assert "only to t app pull" in capsys.readouterr().err
