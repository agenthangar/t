"""A real terminal update prompt resumes the original CLI in the new release."""

import hashlib
import json
import os
from pathlib import Path
import pty
import select
import shutil
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]


def test_release_prompt_updates_then_reexecs_original_argv(tmp_path):
    home = tmp_path / "home"
    (home / "bin").mkdir(parents=True)
    release = tmp_path / "releases" / "v1.2.3"
    (release / "bin").mkdir(parents=True)
    (release / "libexec").mkdir()
    (release / "scripts").mkdir()
    binary = release / "bin" / "t"
    shutil.copy2(ROOT / "bin" / "t", binary)
    shutil.copy2(ROOT / "libexec" / "t_updates.py", release / "libexec" / "t_updates.py")
    (release / ".t-release-version").write_text("v1.2.3\n")
    (release / ".t-install-version").write_text("1\n")
    live = home / "bin" / "t"
    live.symlink_to(binary)

    calls = tmp_path / "updated-argv.jsonl"
    updated = tmp_path / "updated-t.py"
    updated.write_text(
        "import json, os, sys\n"
        "with open(os.environ['T_E2E_CALLS'], 'a', encoding='utf-8') as out:\n"
        "    out.write(json.dumps(sys.argv[1:]) + '\\n')\n"
        "print('UPDATED_EXECUTABLE_RAN')\n"
    )
    installer = release / "scripts" / "install-release.py"
    installer.write_text(
        "import os, pathlib\n"
        "link = pathlib.Path(os.environ['HOME']) / 'bin' / 't'\n"
        "link.unlink()\n"
        "link.symlink_to(os.environ['T_E2E_UPDATED'])\n"
        "print('FAKE_INSTALL_DONE')\n"
    )

    cache = home / ".cache" / "t" / "updates"
    cache.mkdir(parents=True, mode=0o700)
    digest = hashlib.sha256(("release\0" + str(release.resolve())).encode()).hexdigest()
    now = time.time()
    (cache / (digest + ".json")).write_text(json.dumps({
        "identity": {"kind": "release", "source": str(release.resolve()), "current": "v1.2.3"},
        "checked_at": now, "expires_at": now + 3600,
        "available": True, "latest": "v1.2.4", "snoozed_until": 0,
        "pending": False, "error": False,
    }))

    env = {key: value for key, value in os.environ.items()
           if not key.startswith("COV_CORE_") and key not in (
               "CI", "CLAUDECODE", "CODEX_THREAD_ID", "T_NO_UPDATE_CHECK",
               "T_UPDATE_PROMPTED", "T_LOCAL_RC", "TMUX", "TMUX_PANE")}
    env.update(HOME=str(home), XDG_CACHE_HOME=str(home / ".cache"),
               XDG_CONFIG_HOME=str(home / ".config"),
               XDG_STATE_HOME=str(home / ".local" / "state"),
               T_E2E_UPDATED=str(updated), T_E2E_CALLS=str(calls),
               TMUX_TMPDIR=str(tmp_path / "tmux"), TERM="dumb")
    argv = ["repos", "ls"]
    master, slave = pty.openpty()
    output = bytearray()
    proc = None
    sent = False
    try:
        proc = subprocess.Popen([sys.executable, str(binary), *argv],
                                stdin=slave, stdout=slave, stderr=slave,
                                env=env, cwd=home, start_new_session=True)
        os.close(slave)
        slave = -1
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.1)[0]:
                try:
                    chunk = os.read(master, 4096)
                except OSError:
                    chunk = b""
                if not chunk:
                    break
                output.extend(chunk)
                if not sent and b"Choose [l]:" in output:
                    os.write(master, b"u\n")
                    sent = True
            if proc.poll() is not None:
                break
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
            raise AssertionError("t timed out: " + output.decode(errors="replace"))
        assert proc.returncode == 0, output.decode(errors="replace")
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=2)
        if slave >= 0:
            os.close(slave)
        os.close(master)

    terminal = output.decode(errors="replace")
    assert sent, terminal
    assert terminal.count("A t update is available") == 1, terminal
    assert "FAKE_INSTALL_DONE" in terminal
    assert "UPDATED_EXECUTABLE_RAN" in terminal
    assert json.loads(calls.read_text().strip()) == argv
    assert len(calls.read_text().splitlines()) == 1
    assert live.resolve() == updated
