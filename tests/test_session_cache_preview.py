"""Exercise the preview through the real keyboard renderer on a private PTY."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
PREVIEW = ROOT / 'scripts/preview_session_cache.py'


@pytest.mark.parametrize('scenario', ['warm', 'expired', 'offline', 'cold'])
def test_list_preview_renders_sample_data(scenario, tmp_path):
    result = subprocess.run([sys.executable, str(PREVIEW), '--scenario', scenario],
                            cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert f'Sample data ({scenario})' in result.stdout
    assert 'Review retry behavior' in result.stdout


@pytest.mark.parametrize('width,height', [(100, 28), (40, 14)])
def test_management_preview_keyboard(width, height):
    tmux = shutil.which('tmux')
    if not tmux:
        pytest.skip('tmux required for keyboard preview')
    with tempfile.TemporaryDirectory(prefix='t-preview-', dir='/tmp') as temporary:
        address = str(Path(temporary) / 'tmux.sock')
        env = {k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_')}
        env.update(HOME=temporary, ZDOTDIR=temporary, TERM='xterm-256color')
        for name in ('TMUX', 'TMUX_PANE'):
            env.pop(name, None)
        def run(*args, check=True):
            return subprocess.run([tmux, '-S', address, '-f', '/dev/null', *args], cwd=temporary,
                                  env=env, capture_output=True, text=True, check=check, timeout=5)
        def screen(*needles):
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline:
                frame = run('capture-pane', '-p', '-t', 'preview:').stdout
                if all(needle.lower() in frame.lower() for needle in needles):
                    return frame
                time.sleep(.025)
            pytest.fail('Expected ' + str(needles) + '\n' + frame)
        def keys(*names):
            run('send-keys', '-t', 'preview:', *names)
        command = shlex.join([sys.executable, str(PREVIEW), '--manage'])
        try:
            # Retain the last frame, including the exit status, until verified.
            run('new-session', '-d', '-s', 'preview', '-x', str(width), '-y', str(height),
                command + '; echo preview-exit=$?; sleep 30')
            screen('Local settings')
            keys('Down', 'Down', 'Enter')
            screen('Finished session cleanup')
            keys('Down', 'Down', 'Enter')
            screen('Installed, stopped', 'Start')
            keys('Enter')
            screen('Installed, running', 'PID 4101', '4,096')
            keys('Down', 'Enter')
            screen('Installed, running', 'PID 4102', '4,096')
            keys('Enter')
            screen('Installed, stopped', 'Start')
            keys('Down', 'Enter')
            screen('Background agents', 'Session cache')
            keys('Escape')
            screen('Local settings')
            keys('Escape')
            screen('preview-exit=0')
        finally:
            run('kill-server', check=False)
