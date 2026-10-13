"""Guided sharing uses existing terminal chrome and the shared fzf profile."""

import io
import json
import os
from pathlib import Path
import runpy
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def module():
    return runpy.run_path(str(ROOT / "libexec/t_share_ui.py"))


def fake_api(t_mod, width=80, height=24, keys=("enter",)):
    rail_class = t_mod._RailUI

    class Rail(rail_class):
        instances = []

        def __init__(self):
            self.st = t_mod.Style(tty=False)
            self.out = io.StringIO()
            self.rail = "  │"
            self.painted = [0]
            self.frames = []
            self.keys = iter(keys)
            self.raw_count = self.restore_count = 0
            self.instances.append(self)

        def cols(self):
            return width

        def lines(self):
            return height

        def raw(self):
            self.raw_count += 1

        def restore(self):
            self.restore_count += 1

        def repaint(self, lines):
            self.frames.append(lines)
            self.painted[0] = sum(t_mod._term_rows(line, width) for line in lines)

        def read_key(self):
            return next(self.keys)

    api = {"_RailUI": Rail, "_agent_glyph": lambda agent: {"claude": "◆", "codex": "◇", "cursor": "▣"}[agent],
           "_t_source_root": lambda: str(ROOT)}
    return api, Rail


def test_context_manager_choose_and_restore(module, t_mod, monkeypatch):
    monkeypatch.setitem(module["_has_tty"].__globals__, "_has_tty", lambda: True)
    api, Rail = fake_api(t_mod, keys=("down", "enter"))
    with module["ShareUI"](api) as ui:
        ui.intro("Share context", "Choose another agent")
        chosen = ui.choose("Receiving agent", [
            {"key": "claude", "label": "Claude", "detail": "Start another conversation"},
            {"key": "codex", "label": "Codex", "detail": "Continue in Codex"},
        ])
    assert chosen == "codex"
    rail = Rail.instances[-1]
    assert rail.raw_count == 1 and rail.restore_count == 1
    assert "Share context" in rail.out.getvalue()
    assert any("Receiving agent" in line for frame in rail.frames for line in frame)


@pytest.mark.parametrize("width,height", [(80, 24), (44, 20), (32, 12)])
def test_agent_choice_renders_with_shared_rail_at_terminal_sizes(module, t_mod, monkeypatch, width, height):
    monkeypatch.setitem(module["_has_tty"].__globals__, "_has_tty", lambda: True)
    api, Rail = fake_api(t_mod, width, height, keys=("down", "enter"))
    with module["ShareUI"](api) as ui:
        selected = ui.choose("Continue with", [
            {"key": "claude", "label": "✱ Claude", "detail": "Start a new conversation"},
            {"key": "codex", "label": "⬡ Codex", "detail": "Start a new conversation"},
        ])
    assert selected == "codex"
    frames = Rail.instances[-1].frames
    visible = "\n".join(line for frame in frames for line in frame)
    assert "Continue with" in visible
    assert "Claude" in visible and "Codex" in visible
    assert max(t_mod._vis_len(line) for frame in frames for line in frame) <= width


@pytest.mark.parametrize("width,height", [(80, 24), (44, 20), (32, 12)])
def test_review_is_readable_at_terminal_sizes(module, t_mod, monkeypatch, width, height):
    monkeypatch.setitem(module["_has_tty"].__globals__, "_has_tty", lambda: True)
    api, Rail = fake_api(t_mod, width, height, keys=("down",) * 20 + ("enter",))
    source = {"agent": "codex", "address": "codex:11111111-1111-4111-8111-111111111111",
              "title": "Repair login flow"}
    target = {"agent": "claude", "title": "New conversation", "host": "local"}
    with module["ShareUI"](api) as ui:
        accepted = ui.review(source, target, "First actual context line\nSecond context line about the work",
                             notes="Two files will be attached", context_label="Recent conversation text",
                             action="Start Claude")
    assert accepted is True
    frames = Rail.instances[-1].frames
    visible = "\n".join(line for frame in frames for line in frame)
    assert "Review share" in visible
    assert "Start Claude" in visible
    assert "Recent conversation text" in visible or width == 32
    assert "Codex" in visible and "Claude" in visible
    assert "Repair login flow" in visible
    assert "New conversation" in visible
    assert "11111111-1111" not in visible
    assert any("First actual context" in line for frame in frames for line in frame)
    assert max(t_mod._vis_len(line) for frame in frames for line in frame) <= width


@pytest.mark.parametrize("key", ["q", "esc", "abort"])
def test_review_cancel_is_not_acceptance(module, t_mod, monkeypatch, key):
    monkeypatch.setitem(module["_has_tty"].__globals__, "_has_tty", lambda: True)
    api, _ = fake_api(t_mod, keys=(key,))
    with module["ShareUI"](api) as ui:
        assert ui.review({"agent": "codex"}, {"agent": "claude"}, "Actual text") is False


def test_compose_requires_lone_period_and_discards_eof_partial(module, t_mod, monkeypatch):
    monkeypatch.setitem(module["_has_tty"].__globals__, "_has_tty", lambda: True)
    api, Rail = fake_api(t_mod)
    with module["ShareUI"](api) as ui:
        monkeypatch.setattr(module["sys"], "stdin", io.StringIO("First line\nSecond line\n.\n"))
        assert ui.compose("Write a note", "Explain the handoff") == "First line\nSecond line"
        monkeypatch.setattr(module["sys"], "stdin", io.StringIO("Partial text\n"))
        assert ui.compose("Write a note") is None
        monkeypatch.setattr(module["sys"], "stdin", io.StringIO("Partial text\n/cancel\n"))
        assert ui.compose("Write a note") is None
    assert Rail.instances[-1].raw_count == 4
    assert Rail.instances[-1].restore_count == 4


def test_compose_bounds_text_and_restores_terminal(module, t_mod, monkeypatch):
    monkeypatch.setitem(module["_has_tty"].__globals__, "_has_tty", lambda: True)
    monkeypatch.setitem(module["_has_tty"].__globals__, "MAX_COMPOSE_BYTES", 8)
    api, Rail = fake_api(t_mod)
    with module["ShareUI"](api) as ui:
        monkeypatch.setattr(module["sys"], "stdin", io.StringIO("Nine bytes!\n.\n"))
        with pytest.raises(module["ShareUIError"], match="10 MiB"):
            ui.compose("Write a note")
    assert Rail.instances[-1].raw_count == 2
    assert Rail.instances[-1].restore_count == 2


def test_conversation_picker_uses_shared_profile_and_exact_record(module, t_mod, monkeypatch):
    monkeypatch.setitem(module["_has_tty"].__globals__, "_has_tty", lambda: True)
    api, Rail = fake_api(t_mod)
    preferred = {"agent": "claude", "id": "11111111-1111-4111-8111-111111111111",
                 "address": "claude:11111111-1111-4111-8111-111111111111",
                 "title": "Long synthetic target title", "cwd": "/tmp/project-a", "host": "local"}
    other = {"agent": "cursor", "id": "22222222-2222-4222-8222-222222222222",
             "address": "cursor:22222222-2222-4222-8222-222222222222",
             "title": "Other target", "cwd": "/tmp/project-b", "host": "mini"}
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv
        lines = kwargs["input"].splitlines()
        assert len(lines) == 2 and lines[0].split("\t")[1].startswith("Long synthetic")
        assert lines[0].split("\t")[2].startswith("◆ Claude")
        assert "Long synthetic target title" in Path(lines[0].split("\t")[-1]).read_text()
        assert os.stat(lines[0].split("\t")[-1]).st_mode & 0o077 == 0
        return SimpleNamespace(returncode=0, stdout=lines[0] + "\n")

    monkeypatch.setattr(module["subprocess"], "run", fake_run)
    with module["ShareUI"](api) as ui:
        assert ui.conversations("Choose conversation", [other, preferred], "/tmp/project-a") is preferred
    assert "_t_fzf_single" in seen["argv"][2]
    assert "--preview=cat -- {-1}" in seen["argv"]
    assert Rail.instances[-1].raw_count == 2 and Rail.instances[-1].restore_count == 2


@pytest.mark.parametrize("width,height", [(80, 24), (44, 20), (32, 12)])
def test_picker_leads_with_title_at_all_terminal_sizes(module, t_mod, monkeypatch, width, height):
    monkeypatch.setitem(module["_has_tty"].__globals__, "_has_tty", lambda: True)
    monkeypatch.setattr(module["shutil"], "get_terminal_size", lambda fallback: os.terminal_size((width, height)))
    api, _ = fake_api(t_mod, width, height)
    record = {"agent": "claude", "id": "11111111-1111-4111-8111-111111111111",
              "address": "claude:11111111-1111-4111-8111-111111111111",
              "title": "Investigate payment reconciliation", "cwd": "/repo/t/15", "host": "local"}
    captured = {}

    def fake_run(argv, **kwargs):
        captured["row"] = kwargs["input"].splitlines()[0]
        return SimpleNamespace(returncode=130, stdout="")

    monkeypatch.setattr(module["subprocess"], "run", fake_run)
    with module["ShareUI"](api) as ui:
        assert ui.conversations("Select", [record]) is None
    columns = captured["row"].split("\t")
    assert columns[1].startswith("Investigate")
    assert len(columns[1]) >= 12
    assert columns[3].startswith("t/15")
    assert "11111111" not in " ".join(columns[1:-1])
    assert sum(len(value) for value in columns[1:-1]) + 2 <= max(26, width - 6)


def test_conversation_picker_cancel_and_invalid_response(module, t_mod, monkeypatch):
    monkeypatch.setitem(module["_has_tty"].__globals__, "_has_tty", lambda: True)
    api, _ = fake_api(t_mod)
    record = {"agent": "claude", "id": "abc", "address": "claude:abc", "title": "A", "cwd": "/tmp/a", "host": "local"}
    monkeypatch.setattr(module["subprocess"], "run",
                        lambda *args, **kwargs: SimpleNamespace(returncode=130, stdout=""))
    with module["ShareUI"](api) as ui:
        assert ui.conversations("Choose", [record]) is None
    monkeypatch.setattr(module["subprocess"], "run",
                        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="fake\n"))
    with module["ShareUI"](api) as ui:
        with pytest.raises(module["ShareUIError"], match="invalid conversation selection"):
            ui.conversations("Choose", [record])
    monkeypatch.setattr(module["subprocess"], "run",
                        lambda *args, **kwargs: (_ for _ in ()).throw(KeyboardInterrupt()))
    with module["ShareUI"](api) as ui:
        assert ui.conversations("Choose", [record]) is None
    monkeypatch.setattr(module["subprocess"], "run",
                        lambda *args, **kwargs: SimpleNamespace(returncode=127, stdout=""))
    with module["ShareUI"](api) as ui:
        with pytest.raises(module["ShareUIError"], match="install fzf"):
            ui.conversations("Choose", [record])


def test_plain_prompt_only_when_raw_ui_fails_on_real_tty(module, t_mod, monkeypatch, capsys):
    monkeypatch.setitem(module["_has_tty"].__globals__, "_has_tty", lambda: True)
    api, Rail = fake_api(t_mod)

    def unavailable(self):
        raise OSError("raw mode unavailable")

    monkeypatch.setattr(Rail, "raw", unavailable)
    monkeypatch.setattr("builtins.input", lambda prompt: "2")
    with module["ShareUI"](api) as ui:
        assert ui.plain is True
        assert ui.choose("Recipient", [
            {"key": "claude", "label": "Claude", "detail": "New conversation"},
            {"key": "codex", "label": "Codex", "detail": "New conversation"},
        ]) == "codex"
    assert "Recipient" in capsys.readouterr().out
    assert Rail.instances[-1].restore_count == 1


def test_plain_fallback_review_requires_explicit_yes_and_handles_eof(module, t_mod, monkeypatch, capsys):
    monkeypatch.setitem(module["_has_tty"].__globals__, "_has_tty", lambda: True)
    api, Rail = fake_api(t_mod)
    monkeypatch.setattr(Rail, "raw", lambda self: (_ for _ in ()).throw(OSError("raw unavailable")))
    answers = iter(["9", "1", "no", "yes"])
    monkeypatch.setattr("builtins.input", lambda prompt: next(answers))
    with module["ShareUI"](api) as ui:
        ui.intro("Share context", "Review first")
        assert ui.choose("Target", [{"key": "claude", "label": "Claude"}]) == "claude"
        assert ui.review({"agent": "codex"}, {"agent": "claude"}, "Context") is False
        assert ui.review({"agent": "codex"}, {"agent": "claude"}, "Context") is True
        ui.done("To", "Claude")
    assert "Review first" in capsys.readouterr().out
    monkeypatch.setattr("builtins.input", lambda prompt: (_ for _ in ()).throw(EOFError()))
    with module["ShareUI"](api) as ui:
        assert ui.choose("Target", [{"key": "claude", "label": "Claude"}]) is None
        assert ui.review({"agent": "codex"}, {"agent": "claude"}, "Context") is None


def test_review_preview_is_bounded_and_choices_are_validated(module, t_mod, monkeypatch):
    monkeypatch.setitem(module["_has_tty"].__globals__, "_has_tty", lambda: True)
    api, Rail = fake_api(t_mod, keys=("enter",))
    with module["ShareUI"](api) as ui:
        assert ui.choose("Empty", []) is None
        with pytest.raises(module["ShareUIError"], match="unique"):
            ui.choose("Duplicate", [{"key": "x", "label": "First"},
                                    {"key": "x", "label": "Second"}])
        with pytest.raises(module["ShareUIError"], match="context must be text"):
            ui.review({"agent": "codex"}, {"agent": "claude"}, None)
        assert ui.review({"agent": "codex"}, {"agent": "claude"}, "Long line\n" * 200) is True
    assert module["_context_lines"]("Long line\n" * 200, 40)[-1].startswith("… preview shortened")


def test_non_tty_never_enters_guided_ui(module, t_mod, monkeypatch):
    monkeypatch.setitem(module["_has_tty"].__globals__, "_has_tty", lambda: False)
    api, _ = fake_api(t_mod)
    with pytest.raises(module["ShareUIError"], match="terminal"):
        with module["ShareUI"](api):
            pass


@pytest.mark.parametrize("width,height", [(80, 24), (44, 20), (32, 12)])
def test_real_isolated_tty_source_target_review_cancel(tmp_path, width, height):
    """Exercise the real rail and fzf profile without launching an agent or writing a share."""
    if not shutil.which("tmux") or not shutil.which("fzf"):
        pytest.skip("isolated terminal picker requires tmux and fzf")
    env = {key: value for key, value in os.environ.items() if not key.startswith("COV_CORE_")}
    env.pop("TMUX", None)
    env.update(TERM="xterm-256color", FZF_DEFAULT_OPTS="", FZF_DEFAULT_COMMAND="",
               HOME=str(tmp_path / "home"), XDG_CONFIG_HOME=str(tmp_path / "config"),
               XDG_STATE_HOME=str(tmp_path / "state"), XDG_CACHE_HOME=str(tmp_path / "cache"),
               CLAUDE_CONFIG_DIR=str(tmp_path / "claude"), CODEX_HOME=str(tmp_path / "codex"))
    Path(env["HOME"]).mkdir()
    options = tmp_path / "fzf-options"
    options.write_text("")
    env["FZF_DEFAULT_OPTS_FILE"] = str(options)
    harness = tmp_path / "share_tty.py"
    stage, result, sent = (tmp_path / name for name in ("stage", "result.json", "sent"))
    harness.write_text('''import importlib.util, json, pathlib, runpy, sys
from importlib.machinery import SourceFileLoader
root, stage, result, sent = map(pathlib.Path, sys.argv[1:])
loader = SourceFileLoader('t_share_tty_bin', str(root / 'bin/t'))
spec = importlib.util.spec_from_file_location('t_share_tty_bin', str(root / 'bin/t'), loader=loader)
t = importlib.util.module_from_spec(spec)
spec.loader.exec_module(t)
ShareUI = runpy.run_path(str(root / 'libexec/t_share_ui.py'))['ShareUI']
sources = [
    {'agent':'cursor','id':'22222222-2222-4222-8222-222222222222',
     'address':'cursor:22222222-2222-4222-8222-222222222222',
     'title':'Unrelated cursor task','cwd':'/repo/t/8','host':'local'},
    {'agent':'codex','id':'11111111-1111-4111-8111-111111111111',
     'address':'codex:11111111-1111-4111-8111-111111111111',
     'title':'Repair login flow','cwd':'/repo/t/15','host':'local'},
]
targets = [
    {'agent':'claude','id':'33333333-3333-4333-8333-333333333333',
     'address':'claude:33333333-3333-4333-8333-333333333333',
     'title':'Claude handoff review','cwd':'/repo/t/15','host':'local'},
    {'agent':'cursor','id':'44444444-4444-4444-8444-444444444444',
     'address':'cursor:44444444-4444-4444-8444-444444444444',
     'title':'Other recipient','cwd':'/repo/t/8','host':'local'},
]
with ShareUI(vars(t)) as ui:
    stage.write_text('source')
    source = ui.conversations('From conversation', sources, '/repo/t/15')
    if source is None:
        result.write_text(json.dumps({'cancelled':'source'}))
        sys.exit(0)
    ui.done('From', source['title'])
    stage.write_text('target')
    target = ui.conversations('To conversation', targets, '/repo/t/15')
    if target is None:
        result.write_text(json.dumps({'cancelled':'target'}))
        sys.exit(0)
    ui.done('To', target['title'])
    stage.write_text('review')
    accepted = ui.review(source, target, 'Actual context from the login work.\\nNext step is verify logout.',
                         context_label='Recent conversation text', action='Share context')
    if accepted:
        sent.write_text('unexpected share')
    result.write_text(json.dumps({'cancelled':'review' if not accepted else None,
                                  'source':source['id'], 'target':target['id']}))
''')
    with tempfile.TemporaryDirectory(prefix="t-share-tty-") as short:
        socket = str(Path(short) / "tmux.sock")

        def tmux(*args, check=True):
            return subprocess.run(["tmux", "-S", socket, "-f", "/dev/null", *args],
                                  env=env, text=True, capture_output=True, timeout=10,
                                  check=check).stdout

        def screen():
            return tmux("capture-pane", "-p", "-t", "share", "-S", "-50")

        def await_screen(needle):
            deadline = time.monotonic() + 10
            current = ""
            while time.monotonic() < deadline:
                current = screen()
                if needle in current:
                    return current
                time.sleep(.05)
            pytest.fail(f"{needle!r} missing in {width}x{height} pane:\n{current}")

        command = shlex.join([sys.executable, str(harness), str(ROOT), str(stage),
                              str(result), str(sent)])
        try:
            tmux("new-session", "-d", "-s", "share", "-x", str(width), "-y", str(height), command)
            tmux("set-option", "-t", "share", "remain-on-exit", "on")
            source_pane = await_screen("Repair login")
            assert "share>" in source_pane and "From conversation" in source_pane
            assert "11111111" not in source_pane
            tmux("send-keys", "-t", "share", "-l", "Repair login")
            await_screen("1/2")
            tmux("send-keys", "-t", "share", "Enter")
            target_pane = await_screen("Claude handoff")
            assert "share>" in target_pane and "To conversation" in target_pane
            assert "33333333" not in target_pane
            tmux("send-keys", "-t", "share", "-l", "Claude handoff")
            await_screen("1/2")
            tmux("send-keys", "-t", "share", "Enter")
            review_pane = await_screen("Review share")
            assert "Repair login flow" in review_pane
            assert "11111111" not in review_pane and "33333333" not in review_pane
            reviewed = review_pane
            review_frames = [review_pane]
            for _ in range(6):
                if "Actual context" in reviewed:
                    break
                tmux("send-keys", "-t", "share", "Space")
                time.sleep(.05)
                reviewed = screen()
                review_frames.append(reviewed)
            assert "Claude handoff review" in "\n".join(review_frames)
            assert "Actual context" in reviewed
            assert "Share context" in reviewed
            print(f"\n--- share {width}x{height} source ---\n{source_pane}")
            print(f"\n--- share {width}x{height} target ---\n{target_pane}")
            print(f"\n--- share {width}x{height} review initial ---\n{review_pane}")
            print(f"\n--- share {width}x{height} review context ---\n{reviewed}")
            tmux("send-keys", "-t", "share", "Escape")
            deadline = time.monotonic() + 10
            while not result.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            assert result.exists(), screen()
            assert json.loads(result.read_text()) == {
                "cancelled": "review", "source": "11111111-1111-4111-8111-111111111111",
                "target": "33333333-3333-4333-8333-333333333333"}
            assert not sent.exists()
            assert not (Path(env["XDG_STATE_HOME"]) / "t" / "share").exists()
        finally:
            subprocess.run(["tmux", "-S", socket, "kill-server"], env=env,
                           capture_output=True, timeout=10)
