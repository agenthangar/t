"""Exercise actual rsync transport with an isolated local fake SSH endpoint."""

import os
from pathlib import Path
import shlex
import shutil

import pytest

from test_agent_seam import zsh  # noqa: F401 -- shared sandbox fixture


@pytest.mark.parametrize("with_origin", [True, False])
def test_pull_rollout_between_rsync_versions(zsh, tmp_path, with_origin):
    if not shutil.which("rsync") or not Path("/usr/bin/rsync").exists():
        pytest.skip("rsync is not installed")
    remote = tmp_path / "remote"
    rel = "sessions/2026/10/02/rollout-2026-10-02T10-00-00-demo.jsonl"
    source = remote / ".codex" / rel
    source.parent.mkdir(parents=True)
    source.write_text('{"message":"remote continuation"}\n')
    if with_origin:
        source.with_suffix(".origin").write_text("demo-host\n")
    ssh = tmp_path / "stubbin/ssh"
    ssh.write_text("""#!/usr/bin/env python3
import os, sys
args = sys.argv[1:]
if args[:2] == ['-o', 'BatchMode=yes']:
    print(os.environ['REMOTE_ROLLOUT'])
else:
    assert args[0] == 'demo-host' and args[1] == 'rsync', args
    os.chdir(os.environ['REMOTE_HOME'])
    os.execv('/usr/bin/rsync', ['rsync', *args[2:]])
""")
    ssh.chmod(0o755)
    env = {"REMOTE_ROLLOUT": "/Users/demo/.codex/" + rel, "REMOTE_HOME": str(remote)}
    result = zsh("_tbeam_pull_transcript /unused demo-host codex demo", **env)
    assert result.returncode == 0, result.stderr
    destination = zsh.home / ".codex" / rel
    assert destination.read_bytes() == source.read_bytes()
    assert destination.with_suffix(".origin").exists() == with_origin
    assert not (zsh.home / ".codex/.codex").exists()
    # --update preserves a local transcript that is newer than the remote copy.
    destination.write_text('newer local work\n')
    timestamp = source.stat().st_mtime + 60
    os.utime(destination, (timestamp, timestamp))
    result = zsh("_tbeam_pull_transcript /unused demo-host codex demo", **env)
    assert result.returncode == 0, result.stderr
    assert destination.read_text() == 'newer local work\n'


@pytest.mark.parametrize("remote_path", [
    "/Users/demo/.codex/sessions/../../outside/rollout-demo.jsonl",
    "/Users/demo/.codex/sessions/2026/rollout-$(touch bad).jsonl",
    "/tmp/rollout-demo.jsonl",
])
def test_pull_rejects_unsafe_remote_paths(zsh, tmp_path, remote_path):
    ssh = tmp_path / "stubbin/ssh"
    ssh.write_text("#!/bin/sh\nprintf '%s\\n' " + shlex.quote(remote_path) + "\n")
    ssh.chmod(0o755)
    rsync = tmp_path / "stubbin/rsync"
    rsync.write_text("#!/bin/sh\ntouch \"$HOME/rsync-was-called\"\n")
    rsync.chmod(0o755)
    result = zsh("_tbeam_pull_transcript /unused demo-host codex demo")
    assert result.returncode == 1
    assert "unsupported rollout path" in result.stderr
    assert not (zsh.home / "rsync-was-called").exists()
