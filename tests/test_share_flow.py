"""Guided share orchestration with all provider I/O replaced by fakes."""

from argparse import Namespace
from pathlib import Path
import json
import runpy
from types import SimpleNamespace

import pytest


def chat(agent, sid, title="Synthetic conversation", cwd="/repo/slot"):
    return dict(agent=agent, id=sid, address=agent + ":" + sid,
                title=title, cwd=cwd, host="local")


@pytest.fixture
def harness(monkeypatch):
    flow = runpy.run_path(str(Path(__file__).parents[1] / "libexec/t_share_flow.py"))
    state = SimpleNamespace(events=[], files={}, sources={}, remote={},
                            self_address="codex:source", choose=[], conversations=[],
                            compose=[], review=True, snapshot=None)
    source = chat("codex", "source")
    state.sources[source["address"]] = source
    state.snapshot = dict(text="Selected synthetic history", selected_count=2,
                          truncated=False, truncation_reasons=[])

    class TTY:
        def __init__(self, yes):
            self.yes = yes

        def isatty(self):
            return self.yes

    def tty(yes):
        monkeypatch.setitem(flow["run"].__globals__, "sys",
                            SimpleNamespace(stdin=TTY(yes), stdout=TTY(yes)))

    def resolve(address):
        state.events.append(("resolve", address))
        if address not in state.sources:
            raise ValueError("exact conversation unavailable")
        return dict(state.sources[address])

    def request(route, req, _store):
        state.events.append(("request", req["op"], route["alias"], req.get("address")))
        if req["op"] == "targets":
            return list(state.remote.values())
        if req["op"] == "resolve":
            return dict(state.remote[req["address"]] if route["alias"] else state.sources[req["address"]])
        raise AssertionError("unexpected operation")

    def read(path, limit=None):
        state.events.append(("read", path, limit))
        return state.files[path]

    class Store:
        def __init__(self):
            state.events.append(("store",))

    def deliver(_store, envelope, route):
        state.events.append(("deliver", envelope, route))
        return dict(id="message-id", target=envelope["target"]["address"], host=route["alias"] or "local")

    core = dict(local_self=lambda: state.self_address, local_resolve=resolve,
                local_targets=lambda: list(state.sources.values()),
                route_for=lambda _cfg, host: dict(alias=host, target="ssh-target" if host else None),
                Store=Store, request_at=request, VERSION=1,
                identity=lambda row: row, address=lambda value: value if ":" in value else (_ for _ in ()).throw(ValueError("bad address")),
                read_text_file=read, MAX_CONTENT=10 * 1024 * 1024,
                text_size=lambda value: len(value.encode()),
                attachment_name=lambda value: value,
                make_envelope=lambda source, target, text, attachments: dict(source=source, target=target,
                                                                            summary=text, attachments=attachments),
                deliver=deliver,
                render=lambda result, action, as_json: state.events.append(("render", result, action, as_json)))

    class UI:
        def __init__(self, _api):
            pass

        def __enter__(self):
            state.events.append(("ui_enter",))
            return self

        def __exit__(self, *_):
            state.events.append(("ui_exit",))

        def intro(self, *args):
            state.events.append(("intro", *args))

        def done(self, *args):
            state.events.append(("done", *args))

        def choose(self, label, options):
            state.events.append(("choose", label, [row["key"] for row in options]))
            return state.choose.pop(0) if state.choose else None

        def conversations(self, label, rows, preferred_cwd=""):
            state.events.append(("conversations", label, [row["address"] for row in rows], preferred_cwd))
            return state.conversations.pop(0) if state.conversations else None

        def compose(self, label, note):
            state.events.append(("compose", label, note))
            return state.compose.pop(0) if state.compose else None

        def review(self, source, target, text, notes, **kwargs):
            state.events.append(("review", source, target, text, notes, kwargs))
            return state.review

    def preview(_cfg, source, destination):
        state.events.append(("preview", source["address"], destination))
        return dict(repo="repo", slot="2", dirty=False)

    def launch(_cfg, source, destination, content, *, plan):
        state.events.append(("launch", source["address"], destination, content, plan))
        return dict(repo="repo", slot="2", session="dev-repo-2")

    def attach(result):
        state.events.append(("attach", result))
        return 0

    modules = {"t_share_ui": {"ShareUI": UI},
               "t_share_context": {"snapshot": lambda row: dict(state.snapshot)},
               "t_share_launch": {"preview": preview, "launch": launch, "attach": attach}}
    monkeypatch.setitem(flow["run"].__globals__, "module", lambda name: modules[name])
    api = {"_agent_glyph": lambda agent: {"claude": "C", "codex": "X", "cursor": "U"}[agent]}
    return SimpleNamespace(flow=flow, state=state, core=core, api=api, tty=tty,
                           modules=modules, cfg=SimpleNamespace())


def args(**changes):
    values = dict(destination=None, source=None, file=None, attach=[], host=None,
                  share_action=None, retry=None, json=False)
    values.update(changes)
    return Namespace(**values)


def events(harness, name):
    return [event for event in harness.state.events if event[0] == name]


def test_bare_interactive_share_defaults_to_new_claude_and_inferred_codex(harness):
    harness.tty(True)
    harness.state.choose = ["claude"]
    assert harness.flow["run"](harness.cfg, args(), harness.api, harness.core) == 0
    assert events(harness, "choose")[0][2][0] == "claude"
    assert events(harness, "preview") == [("preview", "codex:source", "claude")]
    assert events(harness, "launch")[0][3] == "Selected synthetic history"
    assert events(harness, "review")[0][5]["context_label"] == "Recent conversation text"
    assert not events(harness, "deliver")
    assert harness.state.events.index(events(harness, "review")[0]) < harness.state.events.index(events(harness, "launch")[0])
    assert harness.state.events.index(("ui_exit",)) < harness.state.events.index(events(harness, "attach")[0])
    assert events(harness, "attach")[0][1]["session"] == "dev-repo-2"


def test_short_destination_runs_without_tty_and_json_prints_result(harness, capsys):
    harness.tty(False)
    request = args(destination="claude", json=True)
    assert harness.flow["wanted"](request)
    assert harness.flow["run"](harness.cfg, request, harness.api, harness.core) == 0
    value = json.loads(capsys.readouterr().out)
    assert value == {"version": 1, "result": {"repo": "repo", "slot": "2",
                                               "session": "dev-repo-2", "status": "started"}}
    assert len(events(harness, "launch")) == 1
    assert not events(harness, "ui_enter")
    assert not events(harness, "attach")


def test_cancel_source_destination_note_and_review_without_side_effects(harness):
    harness.tty(True)
    harness.state.self_address = "missing:id"
    assert harness.flow["run"](harness.cfg, args(), harness.api, harness.core) == 130
    assert not events(harness, "store") and not events(harness, "launch") and not events(harness, "deliver")

    harness.state.events.clear()
    harness.state.self_address = "codex:source"
    assert harness.flow["run"](harness.cfg, args(), harness.api, harness.core) == 130
    assert not events(harness, "store") and not events(harness, "launch")

    harness.state.events.clear()
    harness.state.compose = [None]
    def unsupported(_row):
        raise ValueError("unsupported provider")
    harness.modules["t_share_context"]["snapshot"] = unsupported
    assert harness.flow["run"](harness.cfg, args(destination="claude"), harness.api, harness.core) == 130
    assert not events(harness, "launch") and not events(harness, "deliver")

    harness.state.events.clear()
    harness.modules["t_share_context"]["snapshot"] = lambda _row: dict(harness.state.snapshot)
    harness.state.review = False
    assert harness.flow["run"](harness.cfg, args(destination="claude"), harness.api, harness.core) == 130
    assert not events(harness, "launch") and not events(harness, "deliver")


def test_guided_existing_recipient_delivers_without_launch(harness):
    harness.tty(True)
    target = chat("claude", "existing", "Existing target")
    harness.state.sources[target["address"]] = target
    assert harness.flow["run"](harness.cfg, args(destination=target["address"]),
                                harness.api, harness.core) == 0
    assert not events(harness, "preview") and not events(harness, "launch")
    assert events(harness, "deliver")[0][1]["target"]["address"] == target["address"]
    assert events(harness, "request")[0][1:] == ("resolve", None, target["address"])


def test_manual_file_and_attachments_share_exact_reviewed_content(harness):
    harness.tty(True)
    harness.state.files = {"summary.md": "Prepared summary", "/tmp/notes.md": "Supporting notes"}
    assert harness.flow["run"](harness.cfg, args(destination="claude", file="summary.md",
                                                   attach=["/tmp/notes.md"]),
                                harness.api, harness.core) == 0
    assert events(harness, "review")[0][3] == "Prepared summary"
    assert "Attachments: notes.md" in events(harness, "review")[0][4]
    assert events(harness, "launch")[0][3] == "Prepared summary\n\n## Attachment: notes.md\n\nSupporting notes"
    assert not events(harness, "deliver")


def test_remote_agent_chooser_selects_existing_target(harness):
    harness.tty(True)
    target = chat("claude", "remote", "Remote target", "/remote/work")
    harness.state.remote[target["address"]] = target
    harness.state.conversations = [target]
    assert harness.flow["run"](harness.cfg, args(destination="claude", host="mini"),
                                harness.api, harness.core) == 0
    assert events(harness, "request")[0][1:3] == ("targets", "mini")
    assert events(harness, "conversations")[0][2] == [target["address"]]
    assert events(harness, "deliver")[0][2]["alias"] == "mini"
    assert not events(harness, "launch")


def test_noninteractive_ambiguous_source_and_remote_short_name_are_actionable(harness):
    harness.tty(False)
    harness.state.self_address = "missing:id"
    with pytest.raises(ValueError, match="--from"):
        harness.flow["run"](harness.cfg, args(destination="claude"), harness.api, harness.core)
    harness.state.self_address = "codex:source"
    with pytest.raises(ValueError, match="exact --to"):
        harness.flow["run"](harness.cfg, args(destination="claude", host="mini"),
                            harness.api, harness.core)


def test_context_fallback_compose_is_reviewed_and_delivered(harness):
    harness.tty(True)
    harness.state.compose = ["User supplied context"]
    harness.modules["t_share_context"]["snapshot"] = lambda _row: (_ for _ in ()).throw(ValueError("cannot extract"))
    assert harness.flow["run"](harness.cfg, args(destination="claude"), harness.api, harness.core) == 0
    assert events(harness, "compose")[0][2] == "cannot extract"
    assert events(harness, "review")[0][5]["context_label"] == "Your note"
    assert events(harness, "launch")[0][3] == "User supplied context"


def test_source_picker_selection_is_revalidated_before_capture(harness):
    harness.tty(True)
    harness.state.self_address = "missing:id"
    saved = chat("codex", "saved", "Saved source")
    harness.state.sources[saved["address"]] = saved
    harness.state.conversations = [saved]
    assert harness.flow["run"](harness.cfg, args(destination="claude", source="codex"),
                                harness.api, harness.core) == 0
    assert events(harness, "conversations")[0][2] == ["codex:source", "codex:saved"]
    assert ("resolve", "codex:saved") in events(harness, "resolve")
    assert events(harness, "launch")[0][1] == "codex:saved"


def test_explicit_source_address_bypasses_picker_and_claude_defaults_to_codex(harness):
    harness.tty(True)
    source = chat("claude", "source", "Claude source")
    harness.state.sources[source["address"]] = source
    harness.state.choose = ["codex"]
    assert harness.flow["run"](harness.cfg, args(source="claude:source"),
                                harness.api, harness.core) == 0
    assert not events(harness, "conversations")
    assert events(harness, "choose")[0][2][0] == "codex"
    assert events(harness, "launch")[0][1:3] == ("claude:source", "codex")


def test_noninteractive_context_error_requires_file_and_review_notes_include_truncation(harness):
    harness.tty(False)
    harness.modules["t_share_context"]["snapshot"] = lambda _row: (_ for _ in ()).throw(ValueError("no exporter"))
    with pytest.raises(ValueError, match="--file"):
        harness.flow["run"](harness.cfg, args(destination="claude"), harness.api, harness.core)
    assert not events(harness, "launch")
    harness.tty(True)
    harness.modules["t_share_context"]["snapshot"] = lambda _row: dict(
        text="Bounded selected text", selected_count=3, truncated=True,
        truncation_reasons=["earlier messages omitted"])
    harness.modules["t_share_launch"]["preview"] = lambda _cfg, _source, _dest: dict(repo="repo", slot="2", dirty=True)
    assert harness.flow["run"](harness.cfg, args(destination="claude"), harness.api, harness.core) == 0
    notes = events(harness, "review")[0][4]
    assert "Selected history: earlier messages omitted" in notes
    assert "Uncommitted edits stay in the source worktree." in notes


def test_manual_context_validation_prevents_delivery(harness):
    harness.tty(True)
    for request, files, match in [
        (args(destination="claude", file="empty"), {"empty": "   "}, "empty"),
        (args(destination="claude", file="summary", attach=["-"]), {"summary": "Summary"}, "stdin"),
        (args(destination="claude", file="summary", attach=["note"] * 101),
         {"summary": "Summary"}, "100"),
    ]:
        harness.state.files = files
        with pytest.raises(ValueError, match=match):
            harness.flow["run"](harness.cfg, request, harness.api, harness.core)
        assert not events(harness, "launch") and not events(harness, "deliver")
        harness.state.events.clear()


def test_remote_picker_cancel_and_no_targets_never_deliver(harness):
    harness.tty(True)
    target = chat("claude", "remote")
    harness.state.remote[target["address"]] = target
    assert harness.flow["run"](harness.cfg, args(destination="claude", host="mini"),
                                harness.api, harness.core) == 130
    assert not events(harness, "deliver") and not events(harness, "launch")
    harness.state.events.clear()
    harness.state.remote.clear()
    with pytest.raises(ValueError, match="no Claude conversations"):
        harness.flow["run"](harness.cfg, args(destination="claude", host="mini"),
                            harness.api, harness.core)


def test_noninteractive_bare_and_interactive_stdin_are_actionable(harness):
    harness.tty(False)
    with pytest.raises(ValueError, match="--to"):
        harness.flow["run"](harness.cfg, args(), harness.api, harness.core)
    harness.tty(True)
    with pytest.raises(ValueError, match="pipe context"):
        harness.flow["run"](harness.cfg, args(destination="claude", file="-"),
                            harness.api, harness.core)


def test_no_saved_source_and_mismatched_target_fail_before_delivery(harness):
    harness.tty(True)
    harness.state.self_address = "missing:id"
    harness.state.sources.clear()
    with pytest.raises(ValueError, match="no saved source"):
        harness.flow["run"](harness.cfg, args(destination="claude"), harness.api, harness.core)
    harness.state.sources["codex:source"] = chat("codex", "source")
    harness.state.self_address = "codex:source"
    target = chat("claude", "target")
    harness.state.sources[target["address"]] = target
    original = harness.core["request_at"]
    harness.core["request_at"] = lambda route, request, store: (
        dict(target, address="claude:other") if request["op"] == "resolve"
        else original(route, request, store))
    with pytest.raises(ValueError, match="does not match"):
        harness.flow["run"](harness.cfg, args(destination="claude:target"), harness.api, harness.core)
    assert not events(harness, "deliver")


def test_attachment_cannot_exceed_total_context_limit(harness):
    harness.tty(True)
    harness.state.files = {"summary": "12345", "attachment": "67890"}
    harness.core["MAX_CONTENT"] = 9
    with pytest.raises(ValueError, match="10 MiB limit"):
        harness.flow["run"](harness.cfg, args(destination="claude", file="summary",
                                               attach=["attachment"]), harness.api, harness.core)
    assert not events(harness, "launch")


def test_plain_new_and_noninteractive_existing_receipt(harness, capsys):
    harness.tty(False)
    assert harness.flow["run"](harness.cfg, args(destination="claude"), harness.api, harness.core) == 0
    output = capsys.readouterr().out
    assert "Started Claude" in output
    assert "t session open repo 2" in output
    harness.state.events.clear()
    target = chat("claude", "target")
    harness.state.sources[target["address"]] = target
    assert harness.flow["run"](harness.cfg, args(destination=target["address"]),
                                harness.api, harness.core) == 0
    assert events(harness, "render")[0][1]["target"] == target["address"]


def test_module_loader_points_at_sibling_python_files(harness):
    module = harness.flow["module"]("t_share_context")
    assert callable(module["snapshot"])
