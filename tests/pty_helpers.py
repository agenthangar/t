"""Shared pseudo-terminal harness for isolated shell tests."""
import fcntl
import os
import pty
import select
import signal
import struct
import subprocess
import termios
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


def run_terminal(command, env, cwd, size, actions):
    master, slave = pty.openpty()
    before = termios.tcgetattr(slave)
    fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack('HHHH', size[1], size[0], 0, 0))
    process = subprocess.Popen(command, stdin=slave, stdout=slave, stderr=slave,
                               cwd=cwd, env=env, start_new_session=True)
    output, index, searched_from = b'', 0, 0
    deadline = time.monotonic() + 30
    try:
        while time.monotonic() < deadline:
            if select.select([master], [], [], 0.05)[0]:
                output += os.read(master, 65536)
            if index < len(actions):
                needle, keys = actions[index]
                if needle.encode() in output[searched_from:]:
                    searched_from = len(output)
                    if keys == b"\x03":
                        process.send_signal(signal.SIGINT)
                    else:
                        os.write(master, keys)
                    index += 1
            if process.poll() is not None:
                while select.select([master], [], [], 0)[0]:
                    try:
                        chunk = os.read(master, 65536)
                    except OSError:
                        break
                    if not chunk:
                        break
                    output += chunk
                break
        pending = repr(actions[index]) if index < len(actions) else 'process exit'
        diagnostic = (f'Completed {index}/{len(actions)} terminal actions; waiting for {pending}\n'
                      + output.decode(errors='replace'))
        assert process.poll() is not None, diagnostic
        assert index == len(actions), diagnostic
        after = termios.tcgetattr(slave)
        flags = termios.ECHO | termios.ICANON | termios.ISIG | termios.IEXTEN
        assert after[3] & flags == before[3] & flags, 'terminal mode was not restored'
        return process.returncode, output.decode(errors='replace')
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        os.close(master)
        os.close(slave)

