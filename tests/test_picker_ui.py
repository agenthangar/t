"""Exercise filter keys in real fzf without touching the user's terminal/server."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(not shutil.which('fzf') or not shutil.which('tmux'),
                                reason='real terminal picker requires fzf and tmux')


@pytest.fixture
def picker():
    with tempfile.TemporaryDirectory(prefix='t-picker-ui-', dir='/tmp') as directory:
        root = Path(directory)
        socket = str(root / 'tmux.sock')
        env = {k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_')}
        env.update(HOME=directory, XDG_CONFIG_HOME=directory, XDG_CACHE_HOME=directory,
                   XDG_STATE_HOME=directory, TMPDIR=directory, TMUX='', SHELL='/bin/sh',
                   TERM='xterm-256color', FZF_DEFAULT_OPTS='', FZF_DEFAULT_OPTS_FILE='/dev/null')

        def tmux(*args, check=True):
            return subprocess.run(['tmux', '-S', socket, *args], env=env, text=True,
                                  capture_output=True, check=check)

        def start(empty=False, width=90):
            rows = ('local\tapi-2\tdetached\tapi  2  ⬡ codex  ○  Alpha completed task\n'
                    'local\tweb-4\tapp\tweb  4  ⬡ codex  ▣  Beta full task detail END-OF-TITLE\n'
                    'local\tapi-7\tattached\tapi  7  ✱ claude  ●  Pending task\n')
            (root / 'all').write_text(rows)
            (root / 'default').write_text('' if empty else '\n'.join(rows.splitlines()[:2]) + '\n')
            script = (f'. {shlex.quote(str(ROOT / "ui/fzf.sh"))}\n'
                      '_t_fzf_filtered "$(cat "$HOME/default")" "$(cat "$HOME/all")" '
                      'open unattached "⬡ codex · ✱ claude" "" --prompt="open > " > "$HOME/selected"\n'
                      'printf done > "$HOME/done"\n')
            (root / 'run.sh').write_text(script)
            tmux('-f', '/dev/null', 'new-session', '-d', '-x', str(width), '-y', '26',
                 '/bin/sh ' + shlex.quote(str(root / 'run.sh')))

        def wait(text):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                screen = tmux('capture-pane', '-p', check=False).stdout
                if text in screen:
                    return screen
                time.sleep(.05)
            pytest.fail(f'Picker never displayed {text!r}:\n{screen}')

        def finish():
            tmux('send-keys', 'Enter')
            deadline = time.monotonic() + 5
            while not (root / 'done').exists() and time.monotonic() < deadline:
                time.sleep(.05)
            assert (root / 'done').exists()
            return (root / 'selected').read_text().splitlines()

        yield start, wait, lambda *keys: tmux('send-keys', *keys), finish
        tmux('kill-server', check=False)


def test_filter_toggle_preserves_query_and_removes_hidden_marks(picker):
    start, wait, keys, finish = picker
    start()
    wait('2/2 (0)')
    keys('-l', 'Beta')
    wait('1/2 (0)')
    keys('C-a')
    screen = wait('1/3 (0)')
    assert 'All sessions' in screen and 'open > Beta' in screen
    keys('C-a')
    wait('1/2 (0)')
    keys('C-u')
    wait('2/2 (0)')
    keys('Up', 'Space', 'C-a')
    wait('All sessions')
    modern = '--id-nth' in subprocess.check_output(['fzf', '--help'], text=True)
    wait('3/3 (1)' if modern else '3/3 (0)')
    keys('Down', 'Space', 'C-a')
    screen = wait('2/2 (1)' if modern else '2/2 (0)')
    assert 'Pending task' not in screen and 'Ctrl-A show all' in screen
    if not modern:
        keys('Up', 'Space')
    selected = finish()
    assert len(selected) == 1 and selected[0].split('\t')[1] == 'api-2'


def test_empty_filter_can_reveal_rows_and_narrow_preview_keeps_details(picker):
    start, wait, keys, finish = picker
    start(empty=True, width=48)
    wait('0/0 (0)')
    keys('C-a')
    wait('3/3 (0)')
    keys('Space', 'C-_')
    wait('END-OF-TITLE')
    keys('Tab')
    wait('3/3 (2)')
    assert [row.split('\t')[1] for row in finish()] == ['api-2', 'web-4']
