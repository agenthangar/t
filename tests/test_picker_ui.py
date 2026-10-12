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
        env.update(HOME=directory, ZDOTDIR=directory, XDG_CONFIG_HOME=directory, XDG_CACHE_HOME=directory,
                   XDG_STATE_HOME=directory, TMPDIR=directory, TMUX='', SHELL='/bin/sh',
                   TERM='xterm-256color', FZF_DEFAULT_OPTS='', FZF_DEFAULT_OPTS_FILE='/dev/null')

        def tmux(*args, check=True):
            return subprocess.run(['tmux', '-S', socket, *args], env=env, text=True,
                                  capture_output=True, check=check)

        def start(empty=False, width=90, mode='open', shell='/bin/sh', *, hostile_opts=False, profile='filtered'):
            rows = ('local\tapi-2\tdetached\tapi  2  ⬡ codex  ○  Alpha completed task\n'
                    'local\tweb-4\tapp\tweb  4  ⬡ codex  ▣  Beta full task detail END-OF-TITLE\n'
                    'local\tapi-7\tattached\tapi  7  ✱ claude  ●  Pending task\n')
            default = '' if empty else '\n'.join(rows.splitlines()[:2]) + '\n'
            if mode == 'kill':
                rows = ('local\tapi-1\tdetached\tDetached completed\n'
                        'local\tapi-2\tattached\tAttached completed\n'
                        'local\tapi-3\tdetached\tDetached pending\n'
                        'local\tapi-4\tattached\tAttached pending\n')
                default = rows.splitlines()[0] + '\n'
                (root / 'attached').write_text('\n'.join(rows.splitlines()[:2]) + '\n')
                (root / 'pending').write_text('\n'.join(rows.splitlines()[::2]) + '\n')
            (root / 'all').write_text(rows)
            (root / 'default').write_text(default)
            mode_env = 'T_PICKER_SESSION_FILTERS=' + shlex.quote(mode) + ' '
            if mode == 'kill':
                mode_env += 'T_PICKER_WITH_ATTACHED="$(cat "$HOME/attached")" T_PICKER_WITH_PENDING="$(cat "$HOME/pending")" '
            if hostile_opts:
                (root / 'fzf-options').write_text('--bind=enter:abort,esc:accept,tab:abort,space:abort\n')
                env['FZF_DEFAULT_OPTS'] = '--cycle'
                env['FZF_DEFAULT_OPTS_FILE'] = str(root / 'fzf-options')
                if hostile_opts == 'env':
                    env['FZF_DEFAULT_OPTS'] += ' --bind=enter:abort,esc:accept,tab:abort,space:abort'
                    env['FZF_DEFAULT_OPTS_FILE'] = '/dev/null'
            picker_cmd = (mode_env + '_t_fzf_filtered "$(cat "$HOME/default")" "$(cat "$HOME/all")" '
                          + mode + ' unattached "⬡ codex · ✱ claude" "" --prompt="' + mode + ' > "')
            if profile == 'single':
                picker_cmd = '_t_fzf_single open "Saved sessions" --delimiter="$(printf "\\t")" --with-nth=-1 --prompt="single > " < "$HOME/default"'
            script = (f'. {shlex.quote(str(ROOT / "ui/fzf.sh"))}\n'
                      + picker_cmd + ' > "$HOME/selected"\n'
                      'printf done > "$HOME/done"\n')
            (root / 'run.sh').write_text(script)
            tmux('-f', '/dev/null', 'new-session', '-d', '-x', str(width), '-y', '26',
                 shell + ' ' + shlex.quote(str(root / 'run.sh')))

        def wait(text):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                screen = tmux('capture-pane', '-p', check=False).stdout
                if text in screen:
                    return screen
                time.sleep(.05)
            pytest.fail(f'Picker never displayed {text!r}:\n{screen}')

        def finish(key='Enter'):
            tmux('send-keys', key)
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
    assert 'Ctrl-T attached: shown' in screen and 'open > Beta' in screen
    keys('C-f')
    wait('1/2 (0)')
    keys('C-u')
    wait('2/2 (0)')
    keys('Up', 'Space', 'C-a')
    wait('Ctrl-T attached: shown')
    modern = '--id-nth' in subprocess.check_output(['fzf', '--help'], text=True)
    wait('3/3 (1)' if modern else '3/3 (0)')
    keys('Down', 'Space', 'C-t')
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


@pytest.mark.parametrize('shell', ['/bin/sh', 'zsh -f'])
def test_kill_filters_are_independent_and_show_all_is_idempotent(picker, shell):
    start, wait, keys, finish = picker
    start(mode='kill', shell=shell)
    wait('1/1 (0)')
    keys('C-t')
    screen = wait('2/2 (0)')
    assert 'Attached completed' in screen and 'Detached pending' not in screen
    assert 'Ctrl-T attached: shown' in screen and 'Ctrl-P pending: hidden' in screen
    keys('C-p')
    screen = wait('4/4 (0)')
    assert 'Ctrl-T attached: shown' in screen and 'Ctrl-P pending: shown' in screen
    keys('C-t')
    screen = wait('2/2 (0)')
    assert 'Detached pending' in screen and 'Attached completed' not in screen
    keys('C-a')
    wait('4/4 (0)')
    # A repeated show-all command must not restore either filter.
    keys('C-a')
    keys('-l', 'pending')
    screen = wait('2/4 (0)')
    assert 'Attached pending' in screen and 'Detached pending' in screen
    keys('C-f')
    screen = wait('0/1 (0)')
    assert 'kill > pending' in screen
    keys('C-p')
    wait('1/2 (0)')
    assert [row.split('\t')[1] for row in finish()] == ['api-3']


def test_resume_live_hotkey_restores_defaults_without_losing_search(picker):
    start, wait, keys, finish = picker
    start(mode='resume')
    wait('2/2 (0)')
    keys('-l', 'Pending')
    wait('0/2 (0)')
    keys('C-l')
    screen = wait('1/3 (0)')
    assert 'Ctrl-L live: shown' in screen
    keys('C-f')
    screen = wait('0/2 (0)')
    assert 'Ctrl-L live: hidden' in screen and 'resume > Pending' in screen
    keys('C-a')
    wait('1/3 (0)')
    assert [row.split('\t')[1] for row in finish()] == ['api-7']


def test_generic_filtered_picker_uses_explicit_show_all_and_defaults(picker):
    start, wait, keys, finish = picker
    start(mode='generic')
    wait('2/2 (0)')
    keys('-l', 'Pending')
    wait('0/2 (0)')
    keys('C-a')
    screen = wait('1/3 (0)')
    assert 'Ctrl-F defaults' in screen and 'generic > Pending' in screen
    keys('C-a')
    wait('1/3 (0)')
    keys('C-f')
    wait('0/2 (0)')
    keys('C-a')
    wait('1/3 (0)')
    assert [row.split('\t')[1] for row in finish()] == ['api-7']


def test_filtered_picker_remains_operable_at_32_columns(picker):
    start, wait, keys, finish = picker
    start(width=32)
    wait('2/2 (0)')
    keys('C-a')
    wait('3/3 (0)')
    keys('-l', 'Pending')
    wait('1/3 (0)')
    assert [row.split('\t')[1] for row in finish()] == ['api-7']


@pytest.mark.parametrize('source', ['env', 'file'])
def test_filtered_picker_controls_override_hostile_fzf_options(picker, source):
    start, wait, keys, finish = picker
    start(hostile_opts=source)
    wait('2/2 (0)')
    keys('Space')
    wait('2/2 (1)')
    assert len(finish()) == 1


@pytest.mark.parametrize('source', ['env', 'file'])
def test_single_picker_enter_override_hostile_fzf_options(picker, source):
    start, wait, keys, finish = picker
    start(hostile_opts=source, profile='single')
    wait('2/2')
    keys('Tab')
    wait('2/2')
    assert len(finish()) == 1


@pytest.mark.parametrize('source', ['env', 'file'])
@pytest.mark.parametrize('key', ['Escape', 'C-c'])
def test_single_picker_abort_keys_override_hostile_fzf_options(picker, source, key):
    start, wait, keys, finish = picker
    start(hostile_opts=source, profile='single')
    wait('2/2')
    assert finish(key) == []
