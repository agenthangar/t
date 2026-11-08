"""Synthetic saved Codex histories shared by CLI resume tests."""
import json
import os

SID = "01234567-89ab-cdef-0123-456789abcdef"
OTHER = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"

def saved_rollout(zsh, sid=SID, slot="3", age=0):
    cwd = zsh.home / "code/.worktrees/api" / slot
    cwd.mkdir(parents=True, exist_ok=True)
    sessions = zsh.home / ".codex/sessions/2026/10/13"
    sessions.mkdir(parents=True, exist_ok=True)
    rollout = sessions / f"rollout-2026-10-13T10-00-00-{sid}.jsonl"
    rollout.write_text(json.dumps({"type": "session_meta", "payload": {
        "id": sid, "cwd": str(cwd), "cli_version": "0.160.1"}}) + "\n" +
        json.dumps({"type": "response_item", "payload": {"type": "message", "role": "user",
                   "content": [{"type": "input_text", "text": f"Task {sid}"}]}}) + "\n")
    if age:
        os.utime(rollout, (age, age))
    return cwd, rollout
