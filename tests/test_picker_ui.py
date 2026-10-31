"""Exercise filter keys in real fzf without touching the user's terminal/server."""
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

import pytest


ROOT = Path(__file__).resolve().parents[1]


def terminal_state(text, platform=sys.platform):
    """Ignore Darwin's transient input-reprocessing flag, retaining all saved modes."""
    if platform != 'darwin' or not text.startswith('gfmt1:'):
        return text
    # Darwin sys/termios.h: PENDIN = 0x20000000. The kernel can set it after a
    # successful restore while pending input is being reprocessed (issue #139).
    return re.sub(r'(?<=:)lflag=([0-9a-fA-F]+)(?=:)',
                  lambda match: f'lflag={int(match[1], 16) & ~0x20000000:x}', text)


SAVED_TERMINAL = ('gfmt1:cflag=4b00:iflag=2b02:lflag=5cb:oflag=3:'
                  'eof=4:intr=3:min=1:time=0:ispeed=9600:ospeed=9600\n')


def test_terminal_state_ignores_only_darwin_pendin():
    pending = SAVED_TERMINAL.replace('lflag=5cb:', 'lflag=200005cb:')
    assert terminal_state(SAVED_TERMINAL, 'darwin') == terminal_state(pending, 'darwin')
    for platform in ('linux', 'freebsd'):
        assert terminal_state(SAVED_TERMINAL, platform) != terminal_state(pending, platform)
        assert terminal_state(pending, platform) == pending


@pytest.mark.parametrize('bit', [0x1, 0x2, 0x8, 0x80, 0x100, 0x400])
def test_terminal_state_keeps_persistent_local_flags(bit):
    # Includes Darwin ECHO (0x8), ISIG (0x80), and ICANON (0x100).
    changed = SAVED_TERMINAL.replace('lflag=5cb:', f'lflag={(0x5cb ^ bit) | 0x20000000:x}:')
    assert terminal_state(SAVED_TERMINAL, 'darwin') != terminal_state(changed, 'darwin')


@pytest.mark.parametrize('field', ['cflag', 'iflag', 'oflag', 'eof', 'intr', 'min', 'time', 'ispeed', 'ospeed'])
def test_terminal_state_keeps_other_flags_characters_and_speeds(field):
    changed = re.sub(rf'(?<=:){field}=([0-9a-fA-F]+)',
                     lambda match: f'{field}={int(match[1], 16) ^ 1:x}', SAVED_TERMINAL)
    assert terminal_state(SAVED_TERMINAL, 'darwin') != terminal_state(changed, 'darwin')


def test_terminal_state_keeps_unknown_serializations_exact():
    text = '2b02:3:4b00:200005cb:0:3:1c:7f:15\n'
    assert terminal_state(text, 'darwin') == text


@pytest.fixture
def picker():
    if not shutil.which('fzf') or not shutil.which('tmux'):
        pytest.skip('real terminal picker requires fzf and tmux')
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

        def start(empty=False, width=90, height=26, mode='open', shell='/bin/sh', *, hostile_opts=False, profile='filtered', monochrome=False, auth_warning=False):
            env.pop('T_PICKER_AUTH_HOSTS', None)
            env.pop('T_PICKER_COLUMNS', None)
            if auth_warning:
                env['T_PICKER_AUTH_HOSTS'] = 'user@host.example'
                env['T_PICKER_COLUMNS'] = 'SESSION  TASK'
            env.pop('NO_COLOR', None)
            if monochrome:
                env['NO_COLOR'] = '1'
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
            if profile in ('resume-rich', 'resume-merged'):
                rows = ('web\t10\tsid1\t/wt\tdate\ttask\t-\t-\tmini\tcodex\t2026-10-22T10:00:00Z\tNewest web\n'
                        'api\t2\tsid2\t/wt\tdate\ttask\t-\t-\t-\tclaude\t2026-10-21T10:00:00Z\tOlder api\n')
                if profile == 'resume-merged':
                    rows = rows.replace('\tNewest web', '\torg/web\tMERGED\t[MERGED] Newest web').replace('\tOlder api', '\torg/api\tOPEN\t[OPEN] Older api')
                default = rows
                env['T_HOME'] = str(ROOT)
            (root / 'all').write_text(rows)
            (root / 'default').write_text(default)
            mode_env = 'T_PICKER_SESSION_FILTERS=' + shlex.quote(mode) + ' '
            if profile in ('resume-rich', 'resume-merged'):
                mode_env += 'T_PICKER_SOURCE_ROWS="$(cat "$HOME/all")" '
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
            if profile in ('public-open', 'public-open-all', 'public-kill'):
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
                picker_cmd = 't open --cli' if profile == 'public-open-all' else 't open api --cli'
                if profile == 'public-kill':
                    prefix += 'cd "$HOME"\n_dev_kill_completed_labels() { print -rl api-4 web-6; }\n'
                    picker_cmd = '_dev_kill_pick'
                redirect = ''  # Public dispatch must see a real terminal on stdout.
            script = (prefix + 'stty -g > "$HOME/terminal-before"\n'
                      + picker_cmd + redirect + '\n'
                      'printf "%s" "$?" > "$HOME/status"\n'
                      'stty -g > "$HOME/terminal-after"\n'
                      'printf done > "$HOME/done"\n')
            (root / 'run.sh').write_text(script)
            tmux('-f', '/dev/null', 'new-session', '-d', '-x', str(width), '-y', str(height),
                 shell + ' ' + shlex.quote(str(root / 'run.sh')))

        def wait(text, absent=(), ordered=(), focused=None):
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                screen = tmux('capture-pane', '-p', check=False).stdout
                positions = [screen.find(value) for value in ordered]
                in_order = all(p >= 0 for p in positions) and positions == sorted(positions)
                has_focus = focused is None or any(
                    '▸' in line and focused in line for line in screen.splitlines())
                if text in screen and not any(value in screen for value in absent) and in_order and has_focus:
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
            before = (root / 'terminal-before').read_text()
            after = (root / 'terminal-after').read_text()
            assert terminal_state(before) == terminal_state(after), (before, after)
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
    # Counts can repaint before the row area after reload-sync.
    screen = wait('2/2 (1)' if modern else '2/2 (0)', absent=('Pending task',))
    assert 'Pending task' not in screen and 'Ctrl-A show all' in screen
    if not modern:
        keys('Up', 'Space')
    selected = finish()
    assert len(selected) == 1 and selected[0].split('\t')[1] == 'api-2'


def test_empty_filter_can_reveal_rows_and_narrow_preview_keeps_details(picker):
    start, wait, keys, finish = picker
    start(empty=True, width=48, mode='resume')
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
@pytest.mark.parametrize('mode,key,label', [('open', 'C-t', 'attached'), ('resume', 'C-l', 'live'), ('kill', 'C-p', 'pending')])
def test_all_session_filter_keys_visible_at_every_size(picker, width, height, mode, key, label):
    start, wait, keys, finish = picker
    start(mode=mode, width=width, height=height)
    screen = wait('▸')
    assert 'Ctrl-A' in screen and 'Ctrl-F' in screen
    assert ('Ctrl-' + key[-1].upper() + ' ' + label + ': hidden') in screen
    if mode == 'kill':
        assert 'Ctrl-T attached: hidden' in screen
    keys(key)
    wait(label + ': shown')
    assert finish('Escape', status=130) == []


@pytest.mark.parametrize('key', ['Escape', 'C-c'])
def test_open_abort_does_not_start_or_attach_sessions(picker, key):
    start, wait, keys, finish = picker
    start(profile='public-open')
    wait('1/1 (0)')
    keys('Space')
    wait('1/1 (1)')
    assert finish(key, status=130) == []


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
@pytest.mark.parametrize('monochrome', [False, True])
def test_open_simple_rows_and_focused_marks(picker, width, height, monochrome):
    start, wait, keys, finish = picker
    start(profile='public-open', width=width, height=height, monochrome=monochrome)
    screen = wait('1/1 (0)')
    assert 'Filters ON' in screen
    assert 'LAST TURN' not in screen and 'details' not in screen and 'Alt-N' not in screen
    keys('C-a')
    wait('2/2 (0)')
    keys('Up', 'Space', 'Up')
    screen = wait('2/2 (1)')
    focused = next(line for line in screen.splitlines() if '▸' in line)
    assert re.search(r'▸\s?✓', focused) and 'Fir' in focused
    keys('Space', 'Up')
    screen = wait('2/2 (0)')
    focused = next(line for line in screen.splitlines() if '▸' in line)
    assert not re.search(r'▸\s?✓', focused) and 'Fir' in focused
    # The full task remains searchable beyond the compact display.
    keys('-l', 'progress')
    wait('1/2 (0)')
    assert finish() == ['api 2 --local']


@pytest.mark.parametrize('empty', [False, True])
@pytest.mark.parametrize('source', ['env', 'file'])
def test_open_removed_shortcuts_never_launch_or_preview(picker, empty, source):
    start, wait, keys, finish = picker
    start(profile='public-open', empty=empty, hostile_opts=source)
    count = '0/0 (0)' if empty else '1/1 (0)'
    wait(count)
    keys('M-n', 'C-_')
    screen = wait(count)
    assert 'Alt-N' not in screen and 'details' not in screen
    assert finish('Escape', status=130) == []


def choose_setting(wait, keys, field, value):
    keys('C-f')
    wait('Filters >')
    keys('-l', field + ':')
    wait('1/')
    keys('Enter')
    wait(field + ' >')
    keys('-l', value)
    wait('1/')
    keys('Enter')
    wait('Filters >')
    keys('Enter')  # Apply filters is the initial focused choice.


@pytest.mark.parametrize('width,height', [(80, 24), (44, 20), (32, 12)])
@pytest.mark.parametrize('profile', ['public-open-all', 'public-kill'])
def test_session_pickers_display_every_filter_key(picker, width, height, profile):
    start, wait, keys, finish = picker
    start(profile=profile, width=width, height=height)
    screen = wait('2/2 (0)')
    for letter in ('A', 'F', 'L'):
        assert 'Ctrl-' + letter in screen or '^' + letter in screen, screen
    assert 'Ctrl-G' not in screen and '^G' not in screen
    assert screen.count('Filters ON') == 1 and '▸' in screen and 'hidden' in screen
    keys('C-f')
    screen = wait('Filters >')
    assert '▸' in screen and 'Enter choose' in screen
    keys('Escape')
    wait('2/2 (0)')
    assert finish('Escape', status=130) == []


def test_public_open_settings_keep_query_restore_saved_choices_and_remove_hidden_marks(picker):
    start, wait, keys, finish = picker
    start(profile='public-open-all')
    wait('2/2 (0)')
    choose_setting(wait, keys, 'agent', 'codex')
    screen = wait('1/1 (0)')
    assert 'agent=codex' in screen
    keys('C-a')
    wait('4/4 (0)')
    keys('-l', 'First')
    wait('1/4 (0)')
    keys('Space')
    wait('1/4 (1)')
    keys('C-l')
    screen = wait('0/1 (0)')
    assert 'open > First' in screen and 'agent=codex' in screen
    keys('C-a')
    wait('1/4 (0)')
    keys('C-f')
    wait('Filters >')
    keys('Escape')
    screen = wait('1/4 (0)')
    assert 'Filters OFF' in screen and 'open > First' in screen
    assert finish() == ['api 2 --local']


@pytest.mark.parametrize('width,height', [(80, 24), (32, 12)])
@pytest.mark.parametrize('field,query,count', [
    ('agent', 'codex', 2), ('progress', 'in progress', 1),
    ('UI', 'desktop', 1), ('model', 'gpt-test-astra', 1),
    ('host', 'local', 4), ('repo', 'api', 2),
    ('session', 'api-2', 1), ('context', 'active', 2),
    ('task', 'First task', 1),
])
def test_open_each_column_opens_searchable_value_picker(picker, field, query, count, width, height):
    start, wait, keys, finish = picker
    start(profile='public-open-all', width=width, height=height)
    wait('2/2 (0)')
    choose_setting(wait, keys, 'State', 'all attachment states')
    wait('4/4 (0)')
    choose_setting(wait, keys, field, query)
    wait(f'{count}/{count} (0)')
    # Cancel a nested value picker and discard the settings: no changes.
    keys('C-f')
    wait('Filters >')
    keys('-l', field + ':')
    wait('1/')
    keys('Enter')
    wait(field + ' >')
    keys('Escape')
    wait('Filters >')
    keys('Escape')
    wait(f'{count}/{count} (0)')
    choose_setting(wait, keys, field, 'All (clear filter)')
    wait('4/4 (0)')
    assert finish('Escape', status=130) == []


def test_open_attachment_views_are_exclusive_and_idempotent(picker):
    start, wait, keys, finish = picker
    start(profile='public-open-all')
    wait('2/2 (0)')
    wait('api-4', absent=('api-2',))
    choose_setting(wait, keys, 'State', '^attached$')
    wait('2/2 (0)')
    wait('api-2', absent=('api-4',))
    keys('C-a')
    wait('4/4 (0)')
    keys('C-l')
    wait('2/2 (0)')
    wait('api-2', absent=('api-4',))
    keys('C-l')
    wait('api-2', absent=('api-4',))
    choose_setting(wait, keys, 'State', '^unattached$')
    wait('2/2 (0)')
    wait('api-4', absent=('api-2',))
    assert finish('Escape', status=130) == []


@pytest.mark.parametrize('width,height', [(80, 24), (44, 20), (32, 12)])
@pytest.mark.parametrize('auth_warning', [False, True])
def test_resume_sort_controls_keep_focused_row_and_change_order(picker, width, height, auth_warning):
    start, wait, keys, finish = picker
    start(profile='resume-rich', mode='resume', width=width, height=height, auth_warning=auth_warning)
    screen = wait('2/2 (0)')
    assert 'Sort: last turn' in screen and 'Newest web' in screen
    if auth_warning:
        assert 'GitHub auth failed:' in screen
    assert '▸' in screen
    for letter in ('T', 'R', 'G', 'O', 'V'):
        assert 'Alt-' + letter in screen or 'M-' + letter in screen
    keys('M-r')
    # Headers can repaint before reload-sync applies the new rows. Wait for
    # the actual order before moving focus, then verify its rendered identity.
    loading = tuple('⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏')
    if height > 12:
        wait('Sort: repo', absent=loading, ordered=('Older api', 'Newest web'))
    else:
        wait('Sort: repo', absent=loading, focused='Older api')
    keys('Up', 'Up')
    screen = wait('Older api', focused='Older api')
    assert 'Older api' in screen and '▸' in screen
    keys('M-t')
    if height > 12:
        wait('Sort: last turn', absent=loading, ordered=('Newest web', 'Older api'))
    else:
        wait('Sort: last turn', absent=loading, focused='Newest web')
    keys('Up', 'Up')
    wait('Newest web', focused='Newest web')
    assert finish()[0].split('\t')[2] == 'sid1'


@pytest.mark.parametrize('width,height', [(80, 24), (44, 20), (32, 12)])
@pytest.mark.parametrize('monochrome', [False, True])
def test_resume_merged_key_preserves_query_and_removes_hidden_marks(picker, width, height, monochrome):
    start, wait, keys, finish = picker
    start(profile='resume-merged', mode='resume', width=width, height=height,
          auth_warning=True, monochrome=monochrome)
    screen = wait('2/2 (0)', focused='[MERGED]')
    assert 'Alt-M' in screen or 'M-M' in screen
    keys('Space')
    wait('2/2 (1)')
    keys('-l', 'e')  # Both titles match; the query must survive each reload.
    keys('M-m')
    screen = wait('1/1 (0)', focused='[OPEN]')
    assert '[MERGED]' not in screen
    assert 'resume > e' in screen
    keys('C-a')
    wait('2/2 (0)')
    keys('C-l')
    wait('1/1 (0)')
    keys('M-m')
    wait('2/2 (0)')
    keys('M-m')
    wait('1/1 (0)', focused='[OPEN]')
    assert finish()[0].split('\t')[2] == 'sid2'
