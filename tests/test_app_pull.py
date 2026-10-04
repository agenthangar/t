"""Bidirectional app command: identity, release ordering, and serialization."""

import os
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


@pytest.fixture
def pull_store(t_mod, app_slot, monkeypatch, tmp_path):
    cfg, row = app_slot
    rollout = tmp_path / "rollout.jsonl"
    rollout.write_text("{}\n")
    records = [f"{SID}\t{rollout}\t{row['cwd']}\tTitle\t0"]
    monkeypatch.setattr(t_mod, "zsh_capture", lambda cmd: "\n".join(records))
    return cfg, row, records, rollout


def test_pull_selects_stamped_or_only_thread(t_mod, pull_store):
    cfg, row, records, _ = pull_store
    for candidate in (row, dict(row, sid="-", state="app")):
        result = t_mod._app_pull_select(cfg, [candidate], "api", "13", "/")
        assert result["sid"] == SID
        assert result["expected_sid"] == candidate["sid"]


def test_pull_ambiguity_and_explicit_thread(t_mod, pull_store):
    cfg, row, records, rollout = pull_store
    records.append(f"{OTHER}\t{rollout}\t{row['cwd']}")
    app = dict(row, sid="-", state="app")
    with pytest.raises(ValueError, match="choose --thread"):
        t_mod._app_pull_select(cfg, [app], "api", "13", "/")
    assert t_mod._app_pull_select(cfg, [app], "api", "13", "/", OTHER)["sid"] == OTHER
    assert t_mod._app_pull_select(cfg, [row], "api", "13", "/")["sid"] == SID


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
    t_mod._app_pull_cli(dict(row, expected_sid="-"))
    assert calls[0][0][:2] == ["zsh", "-lic"]
    assert f"_t_app_pull_slot dev-api-13 {row['cwd']} {SID} -" in calls[0][0][-1]


def test_pull_release_helper(t_mod, app_slot, monkeypatch):
    _, row = app_slot
    calls = []
    def load(path):
        assert path.endswith("/libexec/t_app_handoff.py")
        return {"assert_released": lambda *args: calls.append(args)}
    monkeypatch.setattr(t_mod.runpy, "run_path", load)
    t_mod._app_assert_released(row, "test.app")
    assert calls == [("test.app", SID, row["cwd"], t_mod._run)]
    monkeypatch.setattr(t_mod.runpy, "run_path", lambda path: (_ for _ in ()).throw(OSError("missing")))
    with pytest.raises(ValueError, match="desktop ownership"):
        t_mod._app_assert_released(row, None)


@pytest.mark.parametrize("scenario", ["success", "dry", "running_app", "launch_failed", "linux", "missing_worktree"])
def test_pull_command_orders_release_before_resume(t_mod, pull_store, monkeypatch, capsys, scenario):
    cfg, row, records, _ = pull_store
    rows = "\t".join(row[k] for k in ("sid", "cwd", "slot", "state", "context", "summary", "agent"))
    monkeypatch.setattr(t_mod, "zsh_capture", lambda cmd: rows if cmd == "_dev_session_rows" else "\n".join(records))
    monkeypatch.setattr(t_mod.sys, "platform", "linux" if scenario == "linux" else "darwin")
    monkeypatch.setattr(t_mod, "_app_bundle", lambda: "test.app")
    events = []
    def release(selected, bundle):
        events.append("release")
        if scenario == "running_app":
            raise ValueError("quit the desktop app first")
    def resume(selected):
        assert selected["sid"] == SID
        events.append("resume")
        return subprocess.CompletedProcess([], int(scenario == "launch_failed"), "", "failed; reservation retained")
    monkeypatch.setattr(t_mod, "_app_assert_released", release)
    monkeypatch.setattr(t_mod, "_app_pull_cli", resume)
    if scenario == "missing_worktree":
        os.rmdir(row["cwd"])
    args = t_mod.build_parser().parse_args(["app", "pull", "api", "13", *(["--dry-run"] if scenario == "dry" else [])])
    assert t_mod.cmd_app(cfg, args) == (0 if scenario in ("success", "dry") else 1)
    assert events == (["release", "resume"] if scenario in ("success", "launch_failed") else ["release"] if scenario == "running_app" else [])
    output = capsys.readouterr()
    if scenario == "success":
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
