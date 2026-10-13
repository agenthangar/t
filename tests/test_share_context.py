"""Read-only, bounded history snapshots from synthetic provider files."""

import json
from pathlib import Path
import runpy
import sqlite3

import pytest


CLAUDE_ID = "11111111-1111-4111-8111-111111111111"
CODEX_ID = "22222222-2222-4222-8222-222222222222"
CURSOR_ID = "33333333-3333-4333-8333-333333333333"


def row(agent, sid, cwd="/work"):
    return dict(agent=agent, id=sid, address=agent + ":" + sid,
                cwd=cwd, title="Synthetic task", host="local")


@pytest.fixture
def context(tmp_path, monkeypatch):
    claude = tmp_path / "claude"
    codex = tmp_path / "codex"
    claude.mkdir()
    codex.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    return runpy.run_path(str(Path(__file__).parents[1] / "libexec/t_share_context.py")), claude, codex


def jsonl(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(item) + "\n" for item in records), encoding="utf-8")


def claude_path(root):
    return root / "projects" / "-work" / (CLAUDE_ID + ".jsonl")


def codex_path(root):
    return root / "sessions" / "2026" / "10" / "13" / ("rollout-now-" + CODEX_ID + ".jsonl")


def codex_record(role, text):
    return {"type": "response_item", "payload": {"type": "message", "role": role,
            "content": [{"type": "input_text" if role == "user" else "output_text", "text": text}]}}


def test_claude_snapshot_filters_tool_and_meta_records(context):
    api, claude, _ = context
    jsonl(claude_path(claude), [
        {"type": "user", "sessionId": CLAUDE_ID, "cwd": "/work", "message": {"content": "Please inspect the fixture"}},
        {"type": "assistant", "sessionId": CLAUDE_ID, "message": {"content": [
            {"type": "thinking", "thinking": "private analysis"},
            {"type": "tool_use", "input": {"command": "secret-tool"}},
            {"type": "text", "text": "I found an issue."}]}},
        {"type": "user", "sessionId": CLAUDE_ID, "message": {"content": [
            {"type": "tool_result", "content": "tool payload"}]}},
        {"type": "user", "sessionId": CLAUDE_ID, "isMeta": True, "message": {"content": "injected metadata"}},
        {"type": "user", "sessionId": CLAUDE_ID, "message": {"content": "<system-reminder>injected</system-reminder>"}},
        {"type": "user", "sessionId": CLAUDE_ID, "message": {"content": [
            {"type": "text", "text": "Continue with the fix."}]}},
    ])
    result = api["snapshot"](row("claude", CLAUDE_ID))
    assert result["source"] == "claude:" + CLAUDE_ID
    assert result["selected_count"] == 3
    assert not result["truncated"]
    assert "not a generated summary" in result["text"]
    assert "USER:\nPlease inspect" in result["text"]
    assert "ASSISTANT:\nI found an issue" in result["text"]
    for forbidden in ("private analysis", "secret-tool", "tool payload", "injected metadata", "system-reminder"):
        assert forbidden not in result["text"]


def test_claude_identity_after_provider_prelude_and_user_markup_survives(context):
    api, claude, _ = context
    jsonl(claude_path(claude), [
        {"type": "queue-operation", "operation": "enqueue"},
        {"type": "file-history-snapshot", "messageId": "synthetic"},
        {"type": "user", "sessionId": CLAUDE_ID, "cwd": "/work",
         "message": {"content": "<html>Build a sample page</html>"}},
        {"type": "assistant", "sessionId": CLAUDE_ID,
         "message": {"content": [{"type": "text", "text": "Created the page."}]}},
    ])
    result = api["snapshot"](row("claude", CLAUDE_ID))
    assert result["selected_count"] == 2
    assert "<html>Build a sample page</html>" in result["text"]
    assert "Created the page." in result["text"]


def test_codex_snapshot_filters_provider_wrappers_and_tool_data(context):
    api, _, codex = context
    path = codex_path(codex)
    jsonl(path, [
        {"type": "session_meta", "payload": {"id": CODEX_ID, "cwd": "/work"}},
        codex_record("developer", "developer instructions"),
        codex_record("user", "# AGENTS.md instructions for /work\nsecrets"),
        codex_record("user", "<environment_context>injected</environment_context>"),
        codex_record("user", "Fix the sample"),
        {"type": "response_item", "payload": {"type": "function_call", "arguments": "private command"}},
        {"type": "response_item", "payload": {"type": "function_call_output", "output": "private output"}},
        {"type": "event_msg", "payload": {"type": "agent_message", "message": "duplicate"}},
        codex_record("assistant", "Updated the sample."),
    ])
    result = api["snapshot"](row("codex", CODEX_ID))
    assert result["selected_count"] == 2
    assert "Fix the sample" in result["text"]
    assert "Updated the sample" in result["text"]
    for forbidden in ("developer instructions", "secrets", "environment_context", "private command", "private output", "duplicate"):
        assert forbidden not in result["text"]


def test_codex_db_exact_id_and_cwd_can_point_to_moved_rollout(context):
    api, _, codex = context
    path = codex_path(codex)
    jsonl(path, [{"type": "session_meta", "payload": {"id": CODEX_ID, "cwd": "/old-work"}},
                 codex_record("user", "Current work")])
    db = sqlite3.connect(codex / "state_5.sqlite")
    db.execute("create table threads(id,cwd,rollout_path,source)")
    db.execute("insert into threads values (?,?,?,?)", (CODEX_ID, "/work", str(path), "cli"))
    db.commit(); db.close()
    assert "Current work" in api["snapshot"](row("codex", CODEX_ID))["text"]
    with pytest.raises(api["ContextError"], match="unavailable"):
        api["snapshot"](row("codex", CODEX_ID, "/other"))


def test_wrong_transcript_identity_and_source_row_are_rejected(context):
    api, claude, codex = context
    jsonl(claude_path(claude), [{"type": "user", "sessionId": CODEX_ID, "cwd": "/work",
                                "message": {"content": "Wrong identity"}}])
    with pytest.raises(api["ContextError"], match="unavailable"):
        api["snapshot"](row("claude", CLAUDE_ID))
    jsonl(codex_path(codex), [{"type": "session_meta", "payload": {"id": CLAUDE_ID, "cwd": "/work"}},
                             codex_record("user", "Wrong identity")])
    with pytest.raises(api["ContextError"], match="unavailable"):
        api["snapshot"](row("codex", CODEX_ID))
    for invalid in [None, dict(row("claude", CLAUDE_ID), address="claude:wrong"),
                    dict(row("claude", CLAUDE_ID), host="remote")]:
        with pytest.raises(api["ContextError"], match="source"):
            api["snapshot"](invalid)


def test_recent_messages_and_long_lines_report_truncation(context):
    api, claude, _ = context
    records = [{"type": "user", "sessionId": CLAUDE_ID, "cwd": "/work",
                "message": {"content": "first message"}}]
    records.extend({"type": "user", "sessionId": CLAUDE_ID, "cwd": "/work",
                    "message": {"content": "message " + str(n)}} for n in range(8))
    records.append({"type": "assistant", "sessionId": CLAUDE_ID, "message": {
        "content": [{"type": "text", "text": "x" * 4000 + "recent ending"}]}})
    jsonl(claude_path(claude), records)
    result = api["snapshot"](row("claude", CLAUDE_ID), max_chars=500, max_messages=4)
    assert len(result["text"]) <= 500
    assert result["truncated"]
    assert "recent ending" in result["text"]
    assert "USER (original request):\nfirst message" in result["text"]
    assert "a long message was shortened" in result["truncation_reasons"]


def test_tail_window_and_partial_record(context):
    api, claude, _ = context
    path = claude_path(claude)
    records = [{"type": "user", "sessionId": CLAUDE_ID, "cwd": "/work",
                "message": {"content": "old " + "a" * 5000}},
               {"type": "user", "sessionId": CLAUDE_ID, "cwd": "/work",
                "message": {"content": "recent message"}}]
    jsonl(path, records)
    with path.open("a") as stream:
        stream.write('{"type":"assistant","message":')
    result = api["snapshot"](row("claude", CLAUDE_ID), scan_bytes=2048)
    assert "recent message" in result["text"]
    assert "USER (original request):\nold " in result["text"]
    assert "earlier transcript bytes omitted" in result["truncation_reasons"]


def test_cursor_requires_manual_summary_and_bounds_are_validated(context):
    api, _, _ = context
    with pytest.raises(api["ContextError"], match="--file"):
        api["snapshot"](row("cursor", CURSOR_ID))
    with pytest.raises(api["ContextError"], match="--file"):
        api["snapshot"](row("cursor", "older.chat", ""))
    for kwargs in ({"max_chars": 10}, {"max_messages": True}, {"scan_bytes": 9}):
        with pytest.raises(api["ContextError"], match="bounds"):
            api["snapshot"](row("claude", CLAUDE_ID), **kwargs)


def test_message_parser_rejects_nonconversation_content(context):
    api, _, _ = context
    parse = api["_message"]
    assert api["_clean"](None) == ""
    assert api["_text"](123, "user", "claude") == ""
    assert api["_text"]("assistant text", "assistant", "claude") == ""
    assert parse(None, "claude", CLAUDE_ID) is None
    assert parse({"type": "assistant", "sessionId": CLAUDE_ID, "message": {"role": "user", "content": "wrong role"}}, "claude", CLAUDE_ID) is None
    with pytest.raises(api["ContextError"], match="changed"):
        parse({"type": "user", "sessionId": CODEX_ID, "message": {"content": "wrong chat"}}, "claude", CLAUDE_ID)
    assert parse({"type": "response_item", "payload": []}, "codex", CODEX_ID) is None
    assert parse({"type": "response_item", "payload": {"type": "message", "role": "system", "content": []}}, "codex", CODEX_ID) is None
    assert parse({"type": "response_item", "payload": {"type": "message", "role": "assistant", "content": [
        {"type": "reasoning", "text": "private"}]}}, "codex", CODEX_ID) is None
    assert parse({"type": "response_item", "payload": {"type": "message", "role": "user", "content": [
        {"type": "input_text", "text": "# Instructions\nwrapped"}]}}, "codex", CODEX_ID) is None


def test_missing_and_corrupt_transcripts_are_actionable(context):
    api, claude, codex = context
    with pytest.raises(api["ContextError"], match="unavailable"):
        api["snapshot"](row("claude", CLAUDE_ID))
    path = claude_path(claude)
    path.parent.mkdir(parents=True)
    path.write_text("invalid header\n")
    with pytest.raises(api["ContextError"], match="unavailable"):
        api["snapshot"](row("claude", CLAUDE_ID))
    path.write_text("{}\n")
    with pytest.raises(api["ContextError"], match="unavailable"):
        api["snapshot"](row("claude", CLAUDE_ID))
    path.unlink()
    path.symlink_to(codex / "missing")
    with pytest.raises(api["ContextError"], match="unavailable"):
        api["snapshot"](row("claude", CLAUDE_ID))


def test_empty_messages_and_malformed_tail_do_not_become_context(context):
    api, claude, _ = context
    path = claude_path(claude)
    jsonl(path, [{"type": "user", "sessionId": CLAUDE_ID, "cwd": "/work",
                  "message": {"content": "<command-name>injected</command-name>"}}])
    with pytest.raises(api["ContextError"], match="no user or assistant text"):
        api["snapshot"](row("claude", CLAUDE_ID))
    jsonl(path, [{"type": "user", "sessionId": CLAUDE_ID, "cwd": "/work",
                  "message": {"content": "Real task"}}])
    with path.open("a") as stream:
        stream.write("not json\n")
    assert "Real task" in api["snapshot"](row("claude", CLAUDE_ID))["text"]


def test_codex_fallback_checks_identity_and_skips_unreadable_index(context):
    api, _, codex = context
    path = codex_path(codex)
    jsonl(path, [{"type": "session_meta", "payload": {"id": CODEX_ID, "cwd": "/work"}},
                 codex_record("user", "Do work")])
    (codex / "state_5.sqlite").write_bytes(b"not a database")
    assert "Do work" in api["snapshot"](row("codex", CODEX_ID))["text"]
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": CODEX_ID,
                            "cwd": "/work", "thread_source": "subagent"}}) + "\n")
    with pytest.raises(api["ContextError"], match="unavailable"):
        api["snapshot"](row("codex", CODEX_ID))
    path.write_text(json.dumps({"type": "session_meta", "payload": {"id": CODEX_ID,
                            "cwd": "/elsewhere"}}) + "\n")
    with pytest.raises(api["ContextError"], match="unavailable"):
        api["snapshot"](row("codex", CODEX_ID))


def test_bounded_read_rejects_symlink_and_large_header(context, tmp_path):
    api, _, _ = context
    path = tmp_path / "real.jsonl"
    path.write_text("{}\n")
    link = tmp_path / "link.jsonl"
    link.symlink_to(path)
    with pytest.raises(api["ContextError"], match="cannot be read"):
        api["_read_tail"](link, 1024)
    path.write_text("x" * 65537 + "\n")
    with pytest.raises(api["ContextError"], match="cannot be read"):
        api["_read_tail"](path, 1024)


def test_original_request_retained_with_many_recent_turns(context):
    api, claude, _ = context
    records = [{"type": "user", "sessionId": CLAUDE_ID, "cwd": "/work",
                "message": {"content": "Original task: update demo"}}]
    records.extend({"type": "assistant", "sessionId": CLAUDE_ID,
                    "message": {"content": [{"type": "text", "text": "Update " + str(n)}]}} for n in range(12))
    jsonl(claude_path(claude), records)
    result = api["snapshot"](row("claude", CLAUDE_ID), max_messages=4)
    assert "USER (original request):\nOriginal task" in result["text"]
    assert "Update 11" in result["text"]
    assert "middle messages omitted" in result["truncation_reasons"]


def test_long_original_request_keeps_opening_goal_and_recent_tail(context):
    api, claude, _ = context
    opening = "Goal: migrate the sample application to a new framework. "
    ending = "Final constraint: preserve the public API."
    records = [{"type": "user", "sessionId": CLAUDE_ID, "cwd": "/work",
                "message": {"content": opening + "details " * 1000 + ending}}]
    records.extend({"type": "assistant", "sessionId": CLAUDE_ID,
                    "message": {"content": [{"type": "text", "text": "Step " + str(n)}]}}
                   for n in range(8))
    jsonl(claude_path(claude), records)
    result = api["snapshot"](row("claude", CLAUDE_ID), max_messages=3)
    assert opening in result["text"]
    assert ending in result["text"]
    assert "[middle omitted]" in result["text"]
    assert "original request was shortened" in result["truncation_reasons"]


def test_malformed_candidates_are_skipped_before_valid_exact_source(context):
    api, claude, codex = context
    first = claude / "projects" / "a" / (CLAUDE_ID + ".jsonl")
    jsonl(first, [[]])
    second = claude / "projects" / "z" / (CLAUDE_ID + ".jsonl")
    jsonl(second, [{"type": "user", "sessionId": CLAUDE_ID, "cwd": "/work",
                    "message": {"content": "Verified task"}}])
    assert "Verified task" in api["snapshot"](row("claude", CLAUDE_ID))["text"]
    bad = codex / "sessions" / "2026" / "10" / "12" / ("rollout-bad-" + CODEX_ID + ".jsonl")
    bad.parent.mkdir(parents=True)
    bad.write_text("bad header\n")
    good = codex_path(codex)
    jsonl(good, [{"type": "session_meta", "payload": {"id": CODEX_ID, "cwd": "/work"}},
                 codex_record("user", "Verified Codex task")])
    assert "Verified Codex task" in api["snapshot"](row("codex", CODEX_ID))["text"]


def test_small_bound_reports_source_header_limit(context):
    api, claude, _ = context
    jsonl(claude_path(claude), [{"type": "user", "sessionId": CLAUDE_ID, "cwd": "/work",
                               "message": {"content": "Task"}}])
    with pytest.raises(api["ContextError"], match="too small"):
        api["snapshot"](row("claude", CLAUDE_ID), max_chars=200)
    with pytest.raises(api["ContextError"], match="no room"):
        api["snapshot"](row("claude", CLAUDE_ID), max_chars=250, max_messages=1)


def test_first_user_scan_is_bounded_and_ignores_malformed_rows(context, tmp_path):
    api, _, _ = context
    path = tmp_path / "head.jsonl"
    path.write_text("bad\n" + json.dumps({"type": "user", "sessionId": CLAUDE_ID,
                     "message": {"content": "Original"}}) + "\n")
    assert api["_first_user"](path, "claude", CLAUDE_ID)[1] == "Original"
    assert api["_first_user"](path, "claude", CLAUDE_ID, limit=4) is None
    assert api["_first_user"](tmp_path / "missing", "claude", CLAUDE_ID) is None
    path.write_text(json.dumps({"type": "assistant", "sessionId": CLAUDE_ID,
                                "message": {"content": [{"type": "text", "text": "Only assistant"}]}}) + "\n")
    assert api["_first_user"](path, "claude", CLAUDE_ID) is None
