"""Explicit runtime installation, independent of routing and model downloads."""
import fcntl
import os
from pathlib import Path
import platform
import runpy
import shutil
import signal
import subprocess
import sys
import tempfile

LIB = Path(__file__).resolve().parent
RUNTIMES = ('mlx-vlm', 'lmstudio', 'ollama')


def helper(name):
    return runpy.run_path(str(LIB / (name + '.py')))


def binary(provider):
    if provider == 'mlx-vlm':
        return helper('t_mlx_runtime')['python']()
    name = 'lms' if provider == 'lmstudio' else 'ollama'
    candidates = [shutil.which(name)]
    if provider == 'lmstudio':
        candidates += [Path.home() / '.lmstudio/bin/lms', Path.home() / '.cache/lm-studio/bin/lms']
    else:
        candidates += [Path('/opt/homebrew/bin/ollama'), Path('/usr/local/bin/ollama')]
    return next((Path(p) for p in candidates if p and Path(p).is_file() and os.access(p, os.X_OK)), None)


def installed(provider):
    if provider not in RUNTIMES:
        raise ValueError('Choose mlx-vlm, lmstudio or ollama')
    if provider == 'mlx-vlm':
        try:
            helper('t_mlx_runtime')['available']()
            return True
        except (OSError, ValueError, subprocess.SubprocessError):
            return False
    return binary(provider) is not None


def inventory():
    manager = helper('t_local_manager')
    return [dict(runtime=p, enabled=manager['runtime_enabled'](p), installed=installed(p)) for p in RUNTIMES]


def plan(provider):
    if provider not in RUNTIMES:
        raise ValueError('Choose mlx-vlm, lmstudio or ollama')
    if installed(provider):
        return dict(runtime=provider, installed=True, steps=[])
    if sys.platform not in ('darwin', 'linux'):
        raise ValueError('Runtime installation supports macOS and Linux')
    if provider == 'mlx-vlm':
        if sys.platform != 'darwin' or platform.machine() != 'arm64':
            raise ValueError('MLX VLM requires Apple Silicon macOS')
        if not any(shutil.which(p) for p in ('python3.14', 'python3.13', 'python3.12')):
            raise ValueError('Install Python 3.12–3.14 before installing MLX VLM')
        steps = ['Install mlx-vlm ' + helper('t_mlx_runtime')['VERSION'] + ' in a private Python environment']
    elif provider == 'ollama' and sys.platform == 'darwin':
        if not shutil.which('brew'):
            raise ValueError('Install Homebrew first, or install Ollama from https://ollama.com/download')
        steps = ['Install the Ollama CLI using Homebrew']
    else:
        if not shutil.which('curl') or not shutil.which('bash' if provider == 'lmstudio' else 'sh'):
            raise ValueError('Install curl and a POSIX shell before installing this runtime')
        steps = (['Install LM Studio CLI and llmster using https://lmstudio.ai/install.sh',
                  'Preserve shell startup files'] if provider == 'lmstudio' else
                 ['Install Ollama using https://ollama.com/install.sh',
                  'The official installer may request administrator access and start its service'])
    return dict(runtime=provider, installed=False, steps=steps)


def run(command, timeout=900):
    """Stream installation output and terminate our entire child group on cancellation."""
    child = subprocess.Popen(command, start_new_session=True)
    try:
        code = child.wait(timeout=timeout)
        if code:
            raise ValueError('Runtime installer failed with exit code ' + str(code))
    except BaseException:
        try:
            os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            os.killpg(child.pid, signal.SIGKILL)
            child.wait(timeout=5)
        except ProcessLookupError:
            pass
        raise


def install(provider):
    proposed = plan(provider)
    if proposed['installed']:
        return dict(runtime=provider, installed=True, changed=False)
    root = Path.home() / '.local/share/t/runtime-install'
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (root / (provider + '.lock')).open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another installation of this runtime is in progress') from None
        if installed(provider):
            return dict(runtime=provider, installed=True, changed=False)
        print('Installing ' + provider, flush=True)
        if provider == 'mlx-vlm':
            helper('t_mlx_runtime')['install'](runner=run)
        elif provider == 'ollama' and sys.platform == 'darwin':
            run([shutil.which('brew'), 'install', 'ollama'])
        else:
            url = 'https://lmstudio.ai/install.sh' if provider == 'lmstudio' else 'https://ollama.com/install.sh'
            with tempfile.TemporaryDirectory(prefix='t-runtime-install-') as directory:
                script = str(Path(directory) / 'install.sh')
                run(['curl', '--fail', '--show-error', '--location', '--proto', '=https', '--proto-redir', '=https',
                     '--max-time', '120', '--output', script, url], timeout=130)
                command = ['bash', script, '--no-modify-path'] if provider == 'lmstudio' else ['sh', script]
                run(command)
        if not installed(provider):
            raise ValueError('Installer finished but the runtime could not be verified; retry installation')
    return dict(runtime=provider, installed=True, changed=True)
