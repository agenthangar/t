"""Private, versioned MLX VLM servers for memory-mapped Apple Silicon models."""
import fcntl
import os
from pathlib import Path
import platform
import runpy
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import time

VERSION = '0.7.8'
LIB = Path(__file__).resolve().parent


def helper(name):
    return runpy.run_path(str(LIB / (name + '.py')))


def python():
    return Path.home() / '.local/share/t/mlx-vlm' / VERSION / 'bin/python'


def available():
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise ValueError('MLX VLM requires Apple Silicon macOS')
    binary = python()
    if not binary.is_file():
        raise ValueError('MLX VLM runtime missing; install the model with --provider mlx-vlm --yes')
    result = subprocess.run([str(binary), '-c', 'from importlib.metadata import version; print(version("mlx-vlm"))'],
                            capture_output=True, text=True, timeout=10)
    if result.returncode or result.stdout.strip() != VERSION:
        raise ValueError('MLX VLM runtime version is unverified; reinstall with --provider mlx-vlm --yes')
    return binary


def install():
    if sys.platform != 'darwin' or platform.machine() != 'arm64':
        raise ValueError('MLX VLM requires Apple Silicon macOS')
    destination = python().parent.parent
    destination.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (destination.parent / 'install.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another MLX VLM runtime installation is in progress') from None
        try:
            return available()
        except ValueError:
            pass
        binary = next((shutil.which(name) for name in ('python3.14', 'python3.13', 'python3.12') if shutil.which(name)), None)
        if not binary:
            raise ValueError('Install Python 3.12–3.14 before installing MLX VLM')
        subprocess.run([binary, '-m', 'venv', str(destination)], check=True, timeout=120)
        subprocess.run([str(python()), '-m', 'pip', 'install', 'mlx-vlm==' + VERSION], check=True, timeout=900)
        return available()


def alive(instance):
    identity = instance.get('process_identity')
    return bool(identity and helper('t_local_manager')['process_identity'](instance.get('pid')) == identity)


def stop(instance):
    if not instance.get('owned'):
        raise ValueError('Cannot stop an unowned MLX server')
    if not alive(instance):
        return
    for sig, seconds in ((signal.SIGTERM, 5), (signal.SIGKILL, 5)):
        if not alive(instance):
            return
        try:
            os.killpg(instance['pid'], sig)
        except ProcessLookupError:
            return
        deadline = time.monotonic() + seconds
        while alive(instance) and time.monotonic() < deadline:
            # Reap a direct child; recovery also handles orphaned processes.
            try:
                os.waitpid(instance['pid'], os.WNOHANG)
            except ChildProcessError:
                pass
            time.sleep(.05)
    if alive(instance):
        raise ValueError('Owned MLX server did not stop')


def ready(instance):
    if not alive(instance):
        return False
    runtime = helper('t_local_runtime')
    view = runtime['request'](instance['base'], '/health', timeout=2,
                              headers={'Authorization': 'Bearer ' + instance['api_key']})
    return (view.get('loaded_model') == instance['alias'] and
            view.get('configured_context_limit') == instance['context'] and not view.get('preload_failures'))


def start(instance, persist):
    binary = available()
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    instance.update(base='http://127.0.0.1:' + str(port), api_key=secrets.token_urlsafe(32), owned=True)
    env = dict(os.environ, HF_HUB_OFFLINE='1', TRANSFORMERS_OFFLINE='1',
               MLX_VLM_SERVER_API_KEY=instance['api_key'])
    # Explicit loopback, one generation, bounded context; never download or trust repository code.
    command = [str(binary), '-m', 'mlx_vlm.server', '--host', '127.0.0.1', '--port', str(port),
               '--model', instance['alias'], '--max-num-seqs', '1', '--max-kv-size', str(instance['context'])]
    proc = subprocess.Popen(command, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL, start_new_session=True)
    instance.update(pid=proc.pid, process_identity=helper('t_local_manager')['process_identity'](proc.pid))
    try:
        if not instance['process_identity']:
            raise ValueError('Could not establish MLX server ownership')
        persist()  # Journal ownership before waiting for model loading.
        deadline = time.monotonic() + 300
        while proc.poll() is None and time.monotonic() < deadline:
            try:
                if ready(instance):
                    return
            except (OSError, ValueError):
                pass
            time.sleep(.2)
        raise ValueError('MLX VLM did not confirm the selected model and context')
    except BaseException:
        if instance.get('process_identity'):
            stop(instance)
        else:
            proc.terminate()
            proc.wait(timeout=10)
        raise
