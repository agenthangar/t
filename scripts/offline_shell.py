#!/usr/bin/env python3
"""Open an interactive macOS shell with external networking denied for its processes."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time

SCRIPT = Path(__file__).resolve()
SANDBOX = '/usr/bin/sandbox-exec'
PROFILE = '''(version 1)
(allow default)
(deny network-outbound)
(allow network-outbound (remote ip "localhost:*"))
(allow network-outbound (remote unix-socket))
'''


def probe():
    """Require OS denial, not merely an unreachable address or a DNS failure."""
    try:
        connection = socket.create_connection(('1.1.1.1', 443), timeout=3)
    except PermissionError:
        pass
    else:
        connection.close()
        raise RuntimeError('External networking was not blocked by the OS')
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen(1)
        with socket.create_connection(listener.getsockname(), timeout=3):
            accepted, _ = listener.accept()
            accepted.close()
    return {'external': 'blocked by OS', 'localhost': 'allowed'}


def isolated_environment(directory):
    env = {key: value for key, value in os.environ.items()
           if key not in ('TMUX', 'TMUX_PANE') and not key.startswith('COV_CORE_')}
    return dict(env, TMUX_TMPDIR=str(directory), T_OFFLINE_TEST='1')


def check_tmux(sandbox, tmux, root, private_socket, env):
    output = root/'pane-network.json'
    command = shlex.join([sys.executable, str(SCRIPT), '--probe', str(output)])
    subprocess.run(sandbox + [tmux, '-f', '/dev/null', 'new-session', '-d',
                              '-s', 'offline-check', command+'; sleep 15'],
                   env=env, check=True, timeout=10)
    deadline = time.monotonic()+10
    while not output.exists():
        if time.monotonic() >= deadline:
            raise RuntimeError('The private tmux pane did not confirm network isolation within 10 seconds')
        time.sleep(.05)
    result = json.loads(output.read_text())
    if result != {'external': 'blocked by OS', 'localhost': 'allowed'}:
        raise RuntimeError('The private tmux pane did not confirm network isolation')
    actual = subprocess.check_output([tmux, '-S', str(private_socket), 'display-message',
                                      '-p', '#{socket_path}'], env=env, text=True, timeout=3)
    if Path(actual.strip()).resolve() != private_socket.resolve():
        raise RuntimeError('The tmux server did not use the private test socket')
    print('Private tmux pane: external connections blocked; localhost allowed.')
    return 0


def interactive_shell(sandbox, env):
    print('\nOFFLINE SHELL — internet blocked; localhost allowed.', flush=True)
    print('Run your usual commands, including: t open t --new -l', flush=True)
    print('Type exit to leave. This closes only this sandbox\'s private tmux sessions.\n', flush=True)
    process = subprocess.Popen(sandbox + ['/bin/zsh', '-il'], env=env)
    # The shell handles Ctrl-C. Its waiting parent must not tear down tmux on it.
    previous = signal.signal(signal.SIGINT, signal.SIG_IGN)
    try:
        code = process.wait()
        return 128-code if code < 0 else code
    finally:
        signal.signal(signal.SIGINT, previous)
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()


def cleanup(tmux, private_socket, env):
    """Never address the user's default server; retain the path if cleanup fails."""
    if not private_socket.exists():
        return True
    try:
        result = subprocess.run([tmux, '-S', str(private_socket), 'kill-server'], env=env,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
        if result.returncode == 0 or not private_socket.exists():
            return True
    except (OSError, subprocess.SubprocessError):
        pass
    print('Could not stop the private test server. Close it with:\n  '+
          shlex.join([tmux, '-S', str(private_socket), 'kill-server']), file=sys.stderr)
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true', help='Verify isolation in a subprocess and private tmux pane, then exit')
    parser.add_argument('--probe', nargs='?', const='-', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.probe is not None:
        result = json.dumps(probe())
        if args.probe == '-':
            print(result)
        else:
            output = Path(args.probe)
            temporary = output.with_suffix('.tmp')
            temporary.write_text(result)
            temporary.replace(output)
        return 0
    tmux = shutil.which('tmux')
    if sys.platform != 'darwin' or not tmux or not Path(SANDBOX).is_file():
        parser.error('Requires macOS sandbox-exec and tmux')
    if not args.check and not (sys.stdin.isatty() and sys.stdout.isatty()):
        parser.error('Run python3 scripts/offline_shell.py in a new Terminal tab or window')
    root = Path(tempfile.mkdtemp(prefix='t-offline-', dir='/tmp')).resolve()
    env = isolated_environment(root)
    private_socket = root/f'tmux-{os.getuid()}'/'default'
    try:
        profile = root/'network.sb'
        profile.write_text(PROFILE)
        sandbox = [SANDBOX, '-f', str(profile)]
        subprocess.run(sandbox + [sys.executable, str(SCRIPT), '--probe'], env=env,
                       check=True, timeout=10)
        result = (check_tmux(sandbox, tmux, root, private_socket, env) if args.check
                  else interactive_shell(sandbox, env))
    finally:
        cleaned = cleanup(tmux, private_socket, env)
        if cleaned:
            try:
                shutil.rmtree(root)
            except OSError as exc:
                cleaned = False
                print('Could not remove private test directory '+str(root)+': '+str(exc), file=sys.stderr)
    return result if result else (0 if cleaned else 1)


def cli():
    def interrupted(signum, frame):
        raise SystemExit(128+signum)
    for signum in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(signum, interrupted)
    try:
        return main()
    except KeyboardInterrupt:
        return 130
    except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print('Offline shell: '+str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(cli())
