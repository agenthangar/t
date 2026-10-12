"""The Cursor chat list is a read-only display at common terminal widths."""

import errno
import json
import pty
import select
import os
from pathlib import Path
import subprocess

import pytest


SCRIPT = Path(__file__).resolve().parents[1] / "bin" / "cursor-beam"


@pytest.mark.parametrize("width", [80, 44, 32])
@pytest.mark.parametrize("terminal", [True, False])
def test_cursor_list_keeps_complete_identities_in_terminal_and_redirects(tmp_path, width, terminal):
    home = tmp_path / "home"
    identity = "0123456789abcdef-0123456789abcdef"
    workspace = "/long/workspace/with/a/project/name"
    title = "A useful Cursor conversation"
    chat = home / ".cursor" / "chats" / "workspace" / identity
    chat.mkdir(parents=True)
    (chat / "meta.json").write_text(json.dumps({"cwd": workspace, "title": title}))
    env = {key: value for key, value in os.environ.items() if not key.startswith("COV_CORE_")}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / ".config"), COLUMNS=str(width))
    if terminal:
        master, slave = pty.openpty()
        try:
            result = subprocess.run([str(SCRIPT), "--ls"], env=env, stdout=slave,
                                    stderr=subprocess.PIPE, text=True)
            chunks = []
            while select.select([master], [], [], 0.2)[0]:
                try:
                    chunk = os.read(master, 4096)
                except OSError as error:
                    if error.errno != errno.EIO:
                        raise
                    break
                if not chunk:
                    break
                chunks.append(chunk)
            output = b"".join(chunks).decode()
        finally:
            os.close(master)
            if slave is not None:
                os.close(slave)
    else:
        result = subprocess.run([str(SCRIPT), "--ls"], env=env, capture_output=True, text=True)
        output = result.stdout
    assert result.returncode == 0, result.stderr
    lines = output.splitlines()
    for heading in ("CHAT ID", "WORKSPACE", "LAST USED", "TITLE"):
        assert heading in output
    if terminal:
        assert all(len(line) <= width for line in lines)
    else:
        assert len(lines) == 2
        assert identity in output and workspace in output and title in output
    flattened = "".join(line.strip() for line in lines)
    assert identity in flattened and workspace in flattened
    assert "\x1b" not in output


def test_cursor_list_empty_state(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith("COV_CORE_")}
    env["HOME"] = str(home)
    result = subprocess.run([str(SCRIPT), "--ls"], env=env, capture_output=True, text=True)
    assert result.returncode == 0
    assert result.stdout.strip() == "No cursor chats here (~/.cursor/chats is empty)."
