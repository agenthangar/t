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

        def start(empty=False, width=90, height=26, mode='open', shell='/bin/sh', *, hostile_opts=False, profile='filtered'):
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
                (root / 'fzf-options').write_text('--bind=enter:abort,esc:accept,tab:abort,space:abort,alt-n:accept\n')
                env['FZF_DEFAULT_OPTS'] = '--cycle'
                env['FZF_DEFAULT_OPTS_FILE'] = str(root / 'fzf-options')
                if hostile_opts == 'env':
                    env['FZF_DEFAULT_OPTS'] += ' --bind=enter:abort,esc:accept,tab:abort,space:abort,alt-n:accept'
                    env['FZF_DEFAULT_OPTS_FILE'] = '/dev/null'
            picker_cmd = (mode_env + '_t_fzf_filtered "$(cat "$HOME/default")" "$(cat "$HOME/all")" '
                          + mode + ' unattached "⬡ codex · ✱ claude" "" --prompt="' + mode + ' > "')
            if profile == 'single':
                picker_cmd = '_t_fzf_single open "Saved sessions" --delimiter="$(printf "\\t")" --with-nth=-1 --prompt="single > " < "$HOME/default"'
            elif profile == 'multi':
                picker_cmd = '_t_fzf_multi open "Saved sessions" --delimiter="$(printf "\\t")" --with-nth=-1 --prompt="multi > " < "$HOME/default"'
            prefix = f'. {shlex.quote(str(ROOT / "ui/fzf.sh"))}\n'
            redirect = ' > "$HOME/selected"'
            if profile == 'public-open':
                from test_open_picker import ROWS
                shell = 'zsh -f'
                env.update(T_NO_UPDATE_CHECK='1', T_NO_AUTORELOAD='1',
                           T_LOCAL_RC=str(root / 'local.zsh'))
                raw = ROWS.replace(r'\tapp\tnone', r'\tapp\tactive') if empty else ROWS
                prefix = (f'source {shlex.quote(str(ROOT / "t.plugin.zsh"))}\n'
                          'unset TMUX\n'
                          'DEV_REPOS[api]="$HOME/code/api"\n'
                          'DEV_REPOS[web]="$HOME/code/web"\n'
                          'mkdir -p "$HOME/code/api" "$HOME/code/web"\n'
                          + raw + '\n'
                          '_t_dev() { print -r -- "$*" >> "$HOME/selected"; }\n'
                          ': > "$HOME/selected"\n')
                picker_cmd = 't open api --cli'
                redirect = ''  # Public dispatch must see a real terminal on stdout.
            script = (prefix + 'stty -g > "$HOME/terminal-before"\n'
                      + picker_cmd + redirect + '\n'
                      'printf "%s" "$?" > "$HOME/status"\n'
                      'stty -g > "$HOME/terminal-after"\n'
                      'printf done > "$HOME/done"\n')
            (root / 'run.sh').write_text(script)
            tmux('-f', '/dev/null', 'new-session', '-d', '-x', str(width), '-y', str(height),
                 shell + ' ' + shlex.quote(str(root / 'run.sh')))

        def wait(text):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                screen = tmux('capture-pane', '-p', check=False).stdout
                if text in screen:
                    return screen
                time.sleep(.05)
            pytest.fail(f'Picker never displayed {text!r}:\n{screen}')

        def finish(key='Enter', status=0):
            tmux('send-keys', key)
            deadline = time.monotonic() + 5
            while not (root / 'done').exists() and time.monotonic() < deadline:
                time.sleep(.05)
            assert (root / 'done').exists()
            assert (root / 'status').read_text() == str(status)
            assert (root / 'terminal-before').read_text() == (root / 'terminal-after').read_text()
            assert not list(root.glob('t-picker.*'))
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
    assert finish(key, status=130) == []


@pytest.mark.parametrize('width,height', [(80, 24), (44, 20), (32, 12)])
@pytest.mark.parametrize('profile,prompt', [('single', 'single'), ('multi', 'multi'), ('filtered', 'open')])
def test_primary_action_and_focused_row_visible_in_small_viewports(picker, width, height, profile, prompt):
    start, wait, keys, finish = picker
    start(width=width, height=height, profile=profile)
    screen = wait('▸')
    assert 'Enter open · Esc' in screen
    assert any('▸' in line and 'api' in line for line in screen.splitlines())
    assert f'{prompt} >' in screen
    assert len(finish()) == 1


@pytest.mark.parametrize('width,height', [(80, 24), (44, 20), (32, 12)])
def test_open_new_shortcut_invokes_existing_flow_in_small_viewports(picker, width, height):
    start, wait, keys, finish = picker
    start(profile='public-open', width=width, height=height)
    screen = wait('Alt-N new')
    assert 'Enter open · Esc' in screen
    assert '▸' in screen
    keys('Space')
    wait('1/1 (1)')
    assert finish('M-n') == ['api new']


@pytest.mark.parametrize('empty', [False, True])
def test_open_new_shortcut_with_no_matches_or_hidden_rows(picker, empty):
    start, wait, keys, finish = picker
    start(profile='public-open', empty=empty)
    wait('Alt-N new')
    if empty:
        wait('0/0 (0)')
    else:
        wait('1/1 (0)')
        keys('-l', 'no-such-session')
        wait('0/1 (0)')
    keys('C-a')
    wait('Ctrl-T attached: shown')
    keys('C-f')
    wait('Ctrl-T attached: hidden')
    assert finish('M-n') == ['api new']


@pytest.mark.parametrize('key', ['Escape', 'C-c'])
def test_open_abort_does_not_start_or_attach_sessions(picker, key):
    start, wait, keys, finish = picker
    start(profile='public-open')
    wait('1/1 (0)')
    keys('Space')
    wait('1/1 (1)')
    assert finish(key, status=130) == []


@pytest.mark.parametrize('source', ['env', 'file'])
def test_open_new_shortcut_overrides_hostile_options(picker, source):
    start, wait, keys, finish = picker
    start(profile='public-open', hostile_opts=source)
    wait('1/1 (0)')
    assert finish('M-n') == ['api new']


def test_open_ctrl_n_still_navigates_and_enter_opens_selection(picker):
    start, wait, keys, finish = picker
    start(profile='public-open')
    wait('1/1 (0)')
    keys('C-a')
    wait('2/2 (0)')
    keys('Up', 'C-n')
    wait('2/2 (0)')
    assert finish() == ['api 4 --local']


@pytest.mark.parametrize('width,height', [(80, 24), (44, 20), (32, 12)])
def test_open_last_turn_stays_visible_without_changing_selection(picker, t_mod, width, height):
    start, wait, keys, finish = picker
    start(profile='public-open', width=width, height=height)
    wait('1/1 (0)')
    keys('C-a')
    wait('2/2 (0)')
    stamp = t_mod._session_display_helpers()['format_last_turn']('2026-10-23T16:41:07Z')
    screen = wait(stamp if width >= 60 else stamp[5:16])
    assert 'First' in screen and 'Enter open' in screen
    keys('Up')
    assert finish() == ['api 2 --local']
