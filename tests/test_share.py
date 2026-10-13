"""Share envelopes and inboxes, using only synthetic agent identities and state."""

import importlib.util
import hashlib
import io
import copy
import json
import os
from pathlib import Path
import subprocess
import threading
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def share():
    spec = importlib.util.spec_from_file_location("share_test", ROOT / "libexec/t_share.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def state(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    xdg = home / "state"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_STATE_HOME", str(xdg))
    monkeypatch.delenv("TMUX", raising=False)
    monkeypatch.delenv("TMUX_PANE", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    monkeypatch.delenv("CODEX_THREAD_ID", raising=False)
    monkeypatch.delenv("CURSOR_SESSION_ID", raising=False)
    for key in tuple(os.environ):
        if key.startswith("COV_CORE_"):
            monkeypatch.delenv(key, raising=False)
    return xdg


def identity(agent, suffix="one", *, host="local"):
    return {
        "agent": agent,
        "id": f"{agent}-{suffix}",
        "address": f"{agent}:{agent}-{suffix}",
        "cwd": "/tmp/synthetic-project",
        "title": f"Synthetic {agent} conversation",
        "host": host,
    }


def envelope(share):
    return share.make_envelope(identity("claude"), identity("codex"), "Summary", [])


def resign(share, item):
    item["digest"] = hashlib.sha256(share.encoded({
        key: value for key, value in item.items() if key != "digest"
    })).hexdigest()
    return item


@pytest.mark.parametrize("source_agent,target_agent", [
    (source, target)
    for source in ("claude", "codex", "cursor")
    for target in ("claude", "codex", "cursor")
    if source != target
])
def test_six_agent_directions_have_exact_isolated_inboxes(
    share, state, source_agent, target_agent
):
    store = share.Store()
    source = identity(source_agent)
    target = identity(target_agent)
    other = identity(target_agent, "other")
    envelope = share.make_envelope(source, target, "Decision: use the new API.", [
        {"name": "selected-history.md", "text": "Only the selected history.\n"}
    ])

    receipt = store.receive(envelope)
    assert receipt["id"] == envelope["id"]
    assert receipt["target"] == target["address"]
    assert receipt["status"] == "stored"
    assert [row["id"] for row in store.inbox(target["address"])] == [envelope["id"]]
    assert store.inbox(other["address"]) == []
    with pytest.raises(share.ShareError):
        store.read(other["address"], envelope["id"])

    read = store.read(target["address"], envelope["id"])
    assert read["summary"] == "Decision: use the new API."
    assert "selected-history.md" in read["attachments"]
    attachment = store.read(target["address"], envelope["id"], "selected-history.md")
    assert attachment["text"] == "Only the selected history.\n"
    assert len(store.inbox(target["address"])) == 1  # reads do not consume


def test_outgoing_bundle_and_duplicate_receipt_are_stable(share, state):
    store = share.Store()
    source, target = identity("claude"), identity("codex")
    envelope = share.make_envelope(source, target, "Plan", [])
    route = {"alias": "mini", "target": "mini.example"}
    store.save_outgoing(envelope, route)
    assert store.outgoing(envelope["id"]) == {"envelope": envelope, "route": route}
    first = store.receive(envelope)
    assert store.receive(envelope) == first
    assert len(store.inbox(target["address"])) == 1


def test_replaying_id_with_changed_payload_is_rejected(share, state):
    store = share.Store()
    source, target = identity("claude"), identity("codex")
    envelope = share.make_envelope(source, target, "Original", [])
    store.receive(envelope)
    changed = dict(envelope, summary="Changed")
    with pytest.raises(share.ShareError):
        store.receive(changed)
    assert store.read(target["address"], envelope["id"])["summary"] == "Original"


def test_replaying_id_with_another_recipient_is_rejected(share, state):
    store = share.Store()
    source, target = identity("claude"), identity("codex")
    envelope = share.make_envelope(source, target, "Original", [])
    store.receive(envelope)
    changed = dict(envelope, target=identity("cursor"))
    changed["digest"] = hashlib.sha256(share.encoded({
        key: value for key, value in changed.items() if key != "digest"
    })).hexdigest()
    with pytest.raises(share.ShareError):
        store.receive(changed)
    assert store.inbox("cursor:cursor-one") == []


def test_concurrent_identical_delivery_creates_one_message(share, state):
    store = share.Store()
    target = identity("cursor")
    envelope = share.make_envelope(identity("claude"), target, "Concurrent", [])
    results = []
    errors = []

    def deliver():
        try:
            results.append(store.receive(envelope))
        except Exception as exc:  # reported by the assertion below
            errors.append(exc)

    threads = [threading.Thread(target=deliver) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert not errors
    assert len(results) == 8
    assert all(result == results[0] for result in results)
    assert len(store.inbox(target["address"])) == 1


def test_store_uses_private_xdg_state_and_atomic_messages(share, state):
    store = share.Store()
    target = identity("codex")
    envelope = share.make_envelope(identity("claude"), target, "Private", [])
    store.receive(envelope)
    root = state / "t" / "share"
    assert root.is_dir()
    assert root.stat().st_mode & 0o777 == 0o700
    files = [path for path in root.rglob("*") if path.is_file()]
    assert files
    assert all(path.stat().st_mode & 0o777 == 0o600 for path in files)
    assert not any(".tmp" in path.name for path in files)


def test_store_rejects_public_or_symlink_state_directory(share, state, tmp_path):
    root = state / "t" / "share"
    root.mkdir(parents=True, mode=0o700)
    root.chmod(0o755)
    with pytest.raises(share.ShareError):
        share.Store().receive(envelope(share))
    root.chmod(0o700)
    root.rmdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    root.symlink_to(elsewhere)
    with pytest.raises(share.ShareError):
        share.Store().receive(envelope(share))


def test_store_rejects_corrupted_and_public_message_files(share, state, monkeypatch):
    store = share.Store()
    item = envelope(share)
    store.receive(item)
    path = store.path("inbox", item["id"])
    path.chmod(0o644)
    with pytest.raises(share.ShareError, match="private regular"):
        store.read(item["target"]["address"], item["id"])
    path.chmod(0o600)
    path.write_text("not JSON")
    with pytest.raises(share.ShareError, match="invalid stored"):
        store.read(item["target"]["address"], item["id"])
    path.write_bytes(share.encoded(item))
    monkeypatch.setattr(share, "MAX_WIRE", 4)
    with pytest.raises(share.ShareError, match="too large"):
        store.read(item["target"]["address"], item["id"])


def test_store_rejects_wrong_filename_and_missing_attachment(share, state):
    store = share.Store()
    item = envelope(share)
    store.receive(item)
    path = store.path("inbox", item["id"])
    other_id = str(share.uuid.uuid4())
    store.path("inbox", other_id).write_bytes(path.read_bytes())
    store.path("inbox", other_id).chmod(0o600)
    with pytest.raises(share.ShareError, match="ID mismatch"):
        store.inbox(item["target"]["address"])
    store.path("inbox", other_id).unlink()
    with pytest.raises(share.ShareError, match="attachment not found"):
        store.read(item["target"]["address"], item["id"], "absent.md")
    with pytest.raises(share.ShareError, match="not found"):
        store.read(item["target"]["address"], other_id)


def test_outgoing_rejects_missing_corrupt_route_and_id(share, state):
    store = share.Store()
    item = envelope(share)
    route = {"alias": None, "target": None}
    with pytest.raises(share.ShareError, match="no outgoing"):
        store.outgoing(item["id"])
    store.save_outgoing(item, route)
    path = store.path("outbox", item["id"])
    path.write_text("[]")
    with pytest.raises(share.ShareError, match="invalid outgoing"):
        store.outgoing(item["id"])
    path.write_bytes(share.encoded({"envelope": item, "route": {"wrong": 1}}))
    with pytest.raises(share.ShareError, match="invalid saved route"):
        store.outgoing(item["id"])
    other_id = str(share.uuid.uuid4())
    store.path("outbox", other_id).write_bytes(share.encoded({"envelope": item, "route": route}))
    store.path("outbox", other_id).chmod(0o600)
    with pytest.raises(share.ShareError, match="ID mismatch"):
        store.outgoing(other_id)


@pytest.mark.parametrize("attachments", [
    [{"name": "../outside", "text": "x"}],
    [{"name": "/absolute", "text": "x"}],
    [{"name": "same", "text": "a"}, {"name": "same", "text": "b"}],
    [{"name": "bad", "text": "\ud800"}],
])
def test_invalid_attachments_are_rejected_without_writes(share, state, attachments):
    with pytest.raises(share.ShareError):
        share.make_envelope(identity("claude"), identity("codex"), "Summary", attachments)
    assert not (state / "t" / "share").exists()


def test_content_over_ten_megabytes_is_rejected(share, state):
    with pytest.raises(share.ShareError):
        share.make_envelope(identity("claude"), identity("codex"), "x" * (10 * 1024 * 1024 + 1), [])
    assert not (state / "t" / "share").exists()


@pytest.mark.parametrize("change", [
    lambda e: e.update(version=2),
    lambda e: e.update(id="bad"),
    lambda e: e.update(source=None),
    lambda e: e["source"].update(id="other"),
    lambda e: e["source"].update(title="x" * 8193),
    lambda e: e["source"].update(extra=True),
    lambda e: e.update(created_at="yesterday"),
    lambda e: e.update(summary="\n"),
    lambda e: e.update(attachments={}),
    lambda e: e.update(attachments=[{"name": "x"}]),
    lambda e: e.update(digest="0" * 64),
])
def test_invalid_envelope_is_rejected_before_storage(share, state, change):
    item = copy.deepcopy(envelope(share))
    change(item)
    with pytest.raises(share.ShareError):
        share.Store().receive(item)
    assert not (state / "t" / "share" / "inbox").exists()


@pytest.mark.parametrize("bad", ["claude", "other:one", "codex:", "cursor:../x", 5])
def test_address_requires_exact_agent_and_id(share, bad):
    with pytest.raises(share.ShareError):
        share.address(bad)


def test_text_validation_rejects_controls_and_invalid_utf8(share):
    with pytest.raises(share.ShareError):
        share.text_size("hello\x00world")
    with pytest.raises(share.ShareError):
        share.text_size("\ud800")
    with pytest.raises(share.ShareError):
        share.encoded({"text": "\ud800"})


def test_receiver_checks_exact_local_target_before_writing(share, state, monkeypatch):
    target = identity("codex")
    envelope = share.make_envelope(identity("claude"), target, "Summary", [])
    def missing(address):
        raise ValueError("conversation does not exist here")

    monkeypatch.setattr(share, "local_resolve", missing)
    with pytest.raises(ValueError):
        share.dispatch({"version": 1, "op": "receive", "envelope": envelope}, share.Store())
    assert not (state / "t" / "share").exists()


def test_receiver_rejects_resolver_mismatch_and_unknown_operation(share, state, monkeypatch):
    item = envelope(share)
    monkeypatch.setattr(share, "local_resolve", lambda address: identity("codex", "other"))
    with pytest.raises(share.ShareError, match="recipient does not match"):
        share.dispatch({"version": 1, "op": "receive", "envelope": item}, share.Store())
    with pytest.raises(share.ShareError, match="unsupported share operation"):
        share.dispatch({"version": 1, "op": "invented"}, share.Store())


def test_receiver_rejects_unsupported_protocol(share, state):
    with pytest.raises(share.ShareError):
        share.dispatch({"version": 2, "op": "targets"}, share.Store())


def test_private_receiver_returns_framed_success_and_error(share, state, monkeypatch, capsys):
    monkeypatch.setattr(share, "local_targets", lambda: [identity("claude")])
    monkeypatch.setattr(share.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(
        b'{"version":1,"op":"targets"}'
    )))
    assert share.internal() == 0
    line = capsys.readouterr().out.strip()
    assert line.startswith("T_SHARE_V1:")
    response = json.loads(line.split(":", 1)[1])
    assert response == {"version": 1, "ok": True, "result": [identity("claude")]}

    monkeypatch.setattr(share.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"\xff")))
    assert share.internal() == 0
    response = json.loads(capsys.readouterr().out.strip().split(":", 1)[1])
    assert response["version"] == 1
    assert response["ok"] is False
    monkeypatch.setattr(share, "MAX_WIRE", 2)
    monkeypatch.setattr(share.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"much too large")))
    assert share.internal() == 0
    response = json.loads(capsys.readouterr().out.strip().split(":", 1)[1])
    assert response["ok"] is False and "transport limit" in response["error"]


def test_text_input_rejects_symlink_and_binary(share, state, tmp_path):
    content = tmp_path / "history.md"
    content.write_text("Safe history")
    shortcut = tmp_path / "shortcut.md"
    shortcut.symlink_to(content)
    with pytest.raises((share.ShareError, OSError)):
        share.read_text_file(str(shortcut))
    content.write_bytes(b"\xff")
    with pytest.raises(share.ShareError):
        share.read_text_file(str(content))


def test_text_input_supports_stdin_and_respects_remaining_limit(
    share, state, tmp_path, monkeypatch
):
    monkeypatch.setattr(share.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"stdin summary")))
    assert share.read_text_file("-") == "stdin summary"
    content = tmp_path / "history.md"
    content.write_text("five!")
    with pytest.raises(share.ShareError, match="10 MiB"):
        share.read_text_file(str(content), limit=4)
    with pytest.raises((share.ShareError, OSError)):
        share.read_text_file(str(tmp_path))


def test_remote_sends_payload_on_stdin_and_accepts_framed_noisy_response(
    share, state, monkeypatch
):
    request = {"version": 1, "op": "receive", "envelope": share.make_envelope(
        identity("claude"), identity("cursor"), "Secret synthetic summary", []
    )}
    calls = []
    receipt = {"id": request["envelope"]["id"], "digest": request["envelope"]["digest"],
               "target": "cursor:cursor-one", "status": "stored"}

    def fake_run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=("shell login banner\nT_SHARE_V1:" + json.dumps(
                {"version": 1, "ok": True, "result": receipt}
            ) + "\n").encode(),
            stderr=b"",
        )

    monkeypatch.setattr(share.subprocess, "run", fake_run)
    assert share.remote("mini.example", request) == receipt
    assert len(calls) == 1
    argv, kwargs = calls[0]
    assert "Secret synthetic summary" not in repr(argv)
    assert "zsh -lic" in " ".join(argv)
    assert "BatchMode=yes" in " ".join(argv)
    supplied = kwargs["input"]
    if isinstance(supplied, bytes):
        supplied = supplied.decode("utf-8")
    assert json.loads(supplied) == request


def test_remote_rejects_unframed_or_old_version_response(share, state, monkeypatch):
    request = {"version": 1, "op": "targets"}
    outputs = ["ordinary login banner\n", "T_SHARE_V1: " + json.dumps(
        {"version": 2, "ok": True, "result": []}
    )]

    def fake_run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 0, outputs.pop(0).encode(), b"")

    monkeypatch.setattr(share.subprocess, "run", fake_run)
    with pytest.raises(share.ShareError):
        share.remote("mini.example", request)
    with pytest.raises(share.ShareError):
        share.remote("mini.example", request)


@pytest.mark.parametrize("target", ["-ProxyCommand=evil", "bad host", "", 3])
def test_remote_rejects_unconfigured_ssh_target(share, target):
    with pytest.raises(share.ShareError):
        share.remote(target, {"version": 1, "op": "targets"})


@pytest.mark.parametrize("output,code", [
    (b"", 1),
    (b"T_SHARE_V1:garbage\n", 0),
    (b"T_SHARE_V1:\xff\n", 0),
    (b'T_SHARE_V1:{"version":1,"ok":false,"error":"no target"}\n', 0),
    (b'T_SHARE_V1:{"version":1,"ok":true}\n', 0),
])
def test_remote_reports_rejected_or_invalid_receipts(share, monkeypatch, output, code):
    monkeypatch.setattr(share.subprocess, "run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, code, output, b""))
    with pytest.raises(share.ShareError):
        share.remote("mini.example", {"version": 1, "op": "targets"})


def test_remote_timeout_and_response_limit(share, monkeypatch):
    def timeout(argv, **kwargs):
        raise subprocess.TimeoutExpired(argv, 1)

    monkeypatch.setattr(share.subprocess, "run", timeout)
    with pytest.raises(share.ShareError, match="timed out"):
        share.remote("mini.example", {"version": 1, "op": "targets"})
    monkeypatch.setattr(share, "MAX_WIRE", 2)
    monkeypatch.setattr(share.subprocess, "run", lambda argv, **kwargs:
                        subprocess.CompletedProcess(argv, 0, b"oversized", b""))
    with pytest.raises(share.ShareError, match="transport limit"):
        share.remote("mini.example", {"version": 1, "op": "targets"})


def args(**changes):
    values = dict(share_action=None, destination=None, file=None, attach=[], host=None,
                  source=None, recipient=None, message_id=None, attachment=None,
                  retry=None, json=True)
    values.update(changes)
    return SimpleNamespace(**values)


def test_human_output_shows_targets_inbox_summary_attachment_and_receipt(
    share, state, capsys, t_mod
):
    source, target = identity("claude"), identity("codex")
    item = share.make_envelope(source, target, "A short decision", [
        {"name": "history.md", "text": "Selected turns"}
    ])
    store = share.Store()
    store.receive(item)
    share.render([target], "targets", table_lines=t_mod._table_lines, scope='local')
    output = capsys.readouterr().out
    assert all(label in output for label in ('Share targets · local', 'TARGET', 'HOST',
                                            'DIRECTORY', 'TITLE', target['address']))
    share.render([], "targets")
    assert "No recorded" in capsys.readouterr().out
    share.render(store.inbox(target["address"]), "inbox",
                 table_lines=t_mod._table_lines, scope=target['address'] + ' on local')
    output = capsys.readouterr().out
    assert all(label in output for label in ('Share inbox · ' + target['address'],
                                            'MESSAGE ID', 'RECEIVED', 'FROM', 'HOST', item['id']))
    share.render([], "inbox")
    assert "No shared context" in capsys.readouterr().out
    share.render(store.read(target["address"], item["id"]), "read")
    output = capsys.readouterr().out
    assert "A short decision" in output and "history.md" in output
    share.render(store.read(target["address"], item["id"], "history.md"), "read")
    assert "Selected turns" in capsys.readouterr().out
    share.render(dict(store.receive(item), host="local"), None)
    assert "t share read " + item["id"] in capsys.readouterr().out


def test_run_human_targets_uses_shared_table(share, t_mod, state, monkeypatch, capsys):
    target = identity('cursor')
    monkeypatch.setattr(share, 'request_at', lambda route, request, store: [target])
    cfg = SimpleNamespace(hosts={})
    assert share.run(cfg, args(share_action='targets', json=False),
                     api={'_table_lines': t_mod._table_lines}) == 0
    output = capsys.readouterr().out
    assert 'Share targets · local' in output
    assert 'TARGET' in output and 'HOST' in output and 'DIRECTORY' in output
    assert target['address'] in output


def test_run_local_send_then_inbox_and_read(share, state, tmp_path, monkeypatch, capsys):
    source, target = identity("claude"), identity("codex")
    known = {source["address"]: source, target["address"]: target}
    monkeypatch.setattr(share, "local_resolve", lambda address: known[address])
    monkeypatch.setattr(share, "local_self", lambda: source["address"])
    summary = tmp_path / "summary.md"
    summary.write_text("Local decision\n")
    history = tmp_path / "history.md"
    history.write_text("Selected transcript\n")
    cfg = SimpleNamespace(hosts={})

    assert share.run(cfg, args(destination=target["address"], file=str(summary),
                               attach=[str(history)])) == 0
    sent = json.loads(capsys.readouterr().out)["result"]
    assert sent["target"] == target["address"]
    assert share.run(cfg, args(share_action="inbox", recipient=target["address"])) == 0
    inbox = json.loads(capsys.readouterr().out)["result"]
    assert [entry["id"] for entry in inbox] == [sent["id"]]
    assert share.run(cfg, args(share_action="read", recipient=target["address"],
                               message_id=sent["id"])) == 0
    read = json.loads(capsys.readouterr().out)["result"]
    assert read["summary"] == "Local decision\n"
    assert read["attachments"] == ["history.md"]
    assert share.run(cfg, args(share_action="read", recipient=target["address"],
                               message_id=sent["id"], attachment="history.md")) == 0
    assert json.loads(capsys.readouterr().out)["result"]["text"] == "Selected transcript\n"


def test_run_remote_lost_receipt_then_retries_original_bundle(
    share, state, tmp_path, monkeypatch, capsys
):
    source, target = identity("claude"), identity("cursor")
    monkeypatch.setattr(share, "local_resolve", lambda address: source)
    monkeypatch.setattr(share, "local_self", lambda: source["address"])
    summary = tmp_path / "summary.md"
    summary.write_text("Unchanged across retries\n")
    cfg = SimpleNamespace(hosts={"mini": "mini.example"})
    deliveries = []

    def fake_remote(host, request):
        assert host == "mini.example"
        if request["op"] == "resolve":
            return target
        deliveries.append(request["envelope"])
        if len(deliveries) == 1:
            raise share.ShareError("connection ended before receipt")
        envelope = request["envelope"]
        return dict(id=envelope["id"], digest=envelope["digest"],
                    target=target["address"], status="stored")

    monkeypatch.setattr(share, "remote", fake_remote)
    assert share.run(cfg, args(destination=target["address"], file=str(summary), host="mini")) == 1
    error = capsys.readouterr().err
    assert "Delivery unconfirmed" in error
    assert "t share --retry " in error
    outgoing = list((state / "t" / "share" / "outbox").glob("*.json"))
    assert len(outgoing) == 1
    mid = outgoing[0].stem
    assert mid in error
    assert share.run(cfg, args(retry=mid)) == 0
    sent = json.loads(capsys.readouterr().out)["result"]
    assert sent["id"] == mid
    assert deliveries[0] == deliveries[1]
    assert len(outgoing) == 1


def test_run_validates_route_retry_and_send_arguments(
    share, state, tmp_path, monkeypatch, capsys
):
    source, target = identity("claude"), identity("codex")
    monkeypatch.setattr(share, "local_self", lambda: source["address"])
    monkeypatch.setattr(share, "local_resolve", lambda address: {
        source["address"]: source, target["address"]: target
    }[address])
    summary = tmp_path / "summary.md"
    summary.write_text("One decision")
    cfg = SimpleNamespace(hosts={"mini": "mini.example"})

    assert share.run(cfg, args(share_action="targets", host="absent")) == 1
    assert "unknown host alias" in capsys.readouterr().err
    assert share.run(cfg, args()) == 1
    assert "receiving agent with --to" in capsys.readouterr().err
    assert share.run(cfg, args(retry=str(share.uuid.uuid4()), destination=target["address"])) == 1
    assert "do not supply" in capsys.readouterr().err
    assert share.run(cfg, args(destination=target["address"], file=str(summary),
                               attach=[str(summary)] * 101)) == 1
    assert "at most 100" in capsys.readouterr().err
    assert share.run(cfg, args(destination=target["address"], file=str(summary), attach=["-"])) == 1
    assert "stdin is reserved" in capsys.readouterr().err

    item = envelope(share)
    share.Store().save_outgoing(item, {"alias": "mini", "target": "old.example"})
    assert share.run(cfg, args(retry=item["id"])) == 1
    assert "destination has changed" in capsys.readouterr().err


def test_run_rejects_changed_resolution_or_unconfirmed_receipt(
    share, state, tmp_path, monkeypatch, capsys
):
    source, target = identity("claude"), identity("codex")
    summary = tmp_path / "summary.md"
    summary.write_text("Decision")
    cfg = SimpleNamespace(hosts={})
    monkeypatch.setattr(share, "local_self", lambda: source["address"])
    monkeypatch.setattr(share, "local_resolve", lambda address: source)
    monkeypatch.setattr(share, "request_at", lambda route, request, store: source)
    assert share.run(cfg, args(destination=target["address"], file=str(summary))) == 1
    assert "does not match" in capsys.readouterr().err

    def bad_receipt(route, request, store):
        if request["op"] == "resolve":
            return target
        return {"id": "wrong", "digest": "wrong", "target": target["address"], "status": "stored"}

    monkeypatch.setattr(share, "request_at", bad_receipt)
    assert share.run(cfg, args(destination=target["address"], file=str(summary))) == 1
    assert "Delivery unconfirmed" in capsys.readouterr().err
    assert len(list((state / "t" / "share" / "outbox").glob("*.json"))) == 1


def test_run_targets_labels_selected_host(share, state, monkeypatch, capsys):
    monkeypatch.setattr(share, "remote", lambda host, request: [identity("cursor")])
    cfg = SimpleNamespace(hosts={"mini": "mini.example"})
    assert share.run(cfg, args(share_action="targets", host="mini")) == 0
    result = json.loads(capsys.readouterr().out)["result"]
    assert result[0]["host"] == "mini"
