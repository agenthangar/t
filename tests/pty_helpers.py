"""Shared pseudo-terminal harness for isolated shell tests."""
import os
import pty
import select
import subprocess
import time


# compinit's one-keystroke question when a dir on $fpath is group-writable. It is asked
# ONLY on a tty (without one `read -q` hits EOF and compinit aborts, silently under the
# `source … >/dev/null 2>&1`) — which is why the non-pty tests never met it and the pty
# ones hung on it: GitHub's ubuntu runner ships such a dir. Answered `y`, what a person
# types; the sandbox never uses completion.
COMPINIT_PROMPT = b"Ignore insecure directories and continue [y] or abort compinit [n]? "


def _run_under_pty(argv, env, timeout=60):
    """Run argv under a pseudo-terminal — for the branches gated on `-t 0 && -t 1` (the
    fzf pickers). stdout + stderr come back merged as .stdout; EOF once the child closes
    its side (an empty read, or EIO on Linux). A compinit prompt is answered `y` once."""
    master, slave = pty.openpty()
    proc = subprocess.Popen(argv, env=env, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
    os.close(slave)
    out, deadline, answered = bytearray(), time.monotonic() + timeout, False
    while True:
        ready, _, _ = select.select([master], [], [], max(0.0, deadline - time.monotonic()))
        if not ready:
            proc.kill()
            break
        try:
            chunk = os.read(master, 65536)
        except OSError:
            break
        if not chunk:
            break
        out += chunk
        if not answered and out.rstrip().endswith(COMPINIT_PROMPT.rstrip()):
            os.write(master, b"y")
            answered = True
    os.close(master)
    return subprocess.CompletedProcess(argv, proc.wait(timeout=10), out.decode(errors="replace"), "")


