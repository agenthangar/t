"""Session colors use per-pane styles and transferable conversation state."""
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

import pytest

from conftest import load_script, REPO_ROOT


@pytest.fixture
def appearance():
    return load_script(REPO_ROOT / 'libexec/t_session_appearance.py', 'session_appearance')


@pytest.fixture
def server():
    if not shutil.which('tmux'):
        pytest.skip('tmux unavailable')
    with tempfile.TemporaryDirectory(prefix='t-colors-', dir='/tmp') as tmp:
        socket = tmp + '/tmux'
        env = {**os.environ, 'HOME': tmp}
        env.pop('TMUX', None)

        def tmux(*args):
            return subprocess.run(['tmux', '-S', socket, '-f', '/dev/null', *args],
                                  env=env, capture_output=True, text=True, timeout=5)

        assert tmux('new-session', '-d', '-s', 'dev-colors-1', '-c', str(REPO_ROOT), 'sleep 20').returncode == 0
        try:
            yield socket, tmux
        finally:
            tmux('kill-server')


def test_apply_restores_local_and_inherited_styles_in_real_tmux(appearance, server):
    _, tmux = server
    assert tmux('set-option', '-w', '-t', 'dev-colors-1:', 'window-style', 'fg=blue,bg=red').returncode == 0
    assert tmux('set-option', '-p', '-t', '%0', 'window-active-style', 'fg=green,bg=yellow,bold').returncode == 0
    assert tmux('new-window', '-d', '-t', 'dev-colors-1:', 'sleep 20').returncode == 0
    appearance.apply(tmux, '%0', '#452563')
    assert appearance.get(tmux, '%0', 'window-style') == 'fg=blue,bg=#452563'
    assert set(appearance.get(tmux, '%0', 'window-active-style').split(',')) == {'fg=green', 'bg=#452563', 'bold'}
    assert not appearance.get(tmux, '%1', 'window-active-style')
    appearance.apply(tmux, '%0', '#344d6a')
    assert set(appearance.get(tmux, '%0', 'window-active-style').split(',')) == {'fg=green', 'bg=#344d6a', 'bold'}
    appearance.apply(tmux, '%0', 'default')
    assert not appearance.get(tmux, '%0', 'window-style')
    assert appearance.get(tmux, '%0', 'window-style', inherited=True) == 'fg=blue,bg=red'
    assert set(appearance.get(tmux, '%0', 'window-active-style').split(',')) == {'fg=green', 'bg=yellow', 'bold'}
    appearance.apply(tmux, '%0', '#17283b')
    assert tmux('set-option', '-p', '-t', '%0', 'window-style', 'fg=cyan').returncode == 0
    appearance.apply(tmux, '%0', 'default')
    assert appearance.get(tmux, '%0', 'window-style') == 'fg=cyan'


def test_priority_toggle_records_transferable_clear(appearance, tmp_path):
    sid = '12345678-1234-1234-1234-123456789abc'
    transcript = tmp_path / f'rollout-{sid}.jsonl'
    transcript.write_text(f'{{"type":"session_meta","payload":{{"id":"{sid}","cwd":"/repo"}}}}\n')
    path = tmp_path / sid / 't-priority.log'
    assert not appearance.priority(path)
    appearance.save_priority(path, True)
    assert appearance.priority(path)
    appearance.save_priority(path, True)
    appearance.save_priority(path, False)
    assert not appearance.priority(path)
    assert path.read_bytes() == b'on\noff\n'
    manifest = appearance.helper('t_lifecycle')['transcript_manifest']('codex', sid, '/repo', transcript)
    assert [asset['name'] for asset in manifest['assets']] == ['assets/t-priority.log']


def test_refresh_restores_priority_in_new_pane_and_skips_other_thread(appearance, server, tmp_path, monkeypatch):
    socket, tmux = server
    path = tmp_path / 'conversation' / 't-priority.log'
    appearance.save_priority(path, True)
    target = {'sid': 'verified-thread', 'pid': '123', 'start': 'new-agent'}
    monkeypatch.setattr(appearance, 'verified_target', lambda *_: target)
    monkeypatch.setattr(appearance, 'priority_path', lambda _: path)
    monkeypatch.setattr(appearance, 'workflow_for', lambda _: (appearance.helper('t_workflow'), {}))
    recovery = appearance.helper('t_recovery')
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    appearance.refresh(recovery, socket, '%0', 'another-thread', '123')
    assert not appearance.get(tmux, '%0', 'window-active-style')
    appearance.refresh(recovery, socket, '%0', 'verified-thread', '123')
    assert appearance.get(tmux, '%0', 'window-active-style').endswith('bg=#452563')


@pytest.mark.parametrize('verified', [True, False])
def test_toggle_paints_before_verification_and_only_persists_verified_changes(
        appearance, server, tmp_path, monkeypatch, verified):
    socket, tmux = server
    path = tmp_path / 'thread' / 't-priority.log'
    target = {'sid': 'thread', 'start': 'current'}
    workflow = appearance.helper('t_workflow')
    appearance.apply_current(tmux, '%0', False, 'session', 'idle', workflow, {})
    monkeypatch.setenv('XDG_CACHE_HOME', str(tmp_path / 'cache'))
    monkeypatch.setattr(appearance, 'priority_path', lambda _: path)
    monkeypatch.setattr(appearance, 'workflow_for', lambda _: (workflow, {}))

    def check_owner(*_):
        assert appearance.get(tmux, '%0', appearance.PRIORITY) == '1'
        assert appearance.get(tmux, '%0', 'window-active-style').endswith('bg=#452563')
        assert not path.exists()
        if not verified:
            tmux('set-option', '-p', '-t', '%0', 'window-style', 'fg=cyan')
        return target if verified else None

    monkeypatch.setattr(appearance, 'verified_target', check_owner)
    if verified:
        appearance.toggle_priority(appearance.helper('t_recovery'), socket, '%0')
        assert appearance.priority(path)
    else:
        with pytest.raises(ValueError, match='verified'):
            appearance.toggle_priority(appearance.helper('t_recovery'), socket, '%0')
        assert not path.exists()
        assert appearance.get(tmux, '%0', appearance.PRIORITY) == '0'
        assert not appearance.get(tmux, '%0', 'window-active-style')
        assert appearance.get(tmux, '%0', 'window-style') == 'fg=cyan'


def test_prefix_binding_preserves_user_keys_and_targets_one_pane(appearance, server):
    socket, tmux = server
    recovery = appearance.helper('t_recovery')
    key = appearance.PRIORITY_KEY
    assert tmux('bind-key', '-N', 'User shortcut', '-T', 'prefix', key, 'display-message', 'user').returncode == 0
    appearance.install(recovery, socket, 'dev-colors-1')
    assert appearance.get(tmux, '%0', appearance.TOGGLE)
    assert not appearance.get(tmux, '%0', appearance.SHORTCUT)
    assert 'User shortcut' in tmux('list-keys', '-N', '-T', 'prefix', key).stdout
    assert tmux('unbind-key', '-T', 'prefix', key).returncode == 0
    appearance.install(recovery, socket, 'dev-colors-1')
    assert appearance.PRIORITY_NOTE in tmux('list-keys', '-N', '-T', 'prefix', key).stdout
    assert appearance.get(tmux, '%0', appearance.SHORTCUT) == appearance.PRIORITY_LABEL
    assert str(Path(appearance.__file__)) in appearance.get(tmux, '%0', appearance.COMMAND)


def test_status_badge_is_idempotent_and_preserves_status_controls(appearance, server):
    socket, tmux = server
    recovery = appearance.helper('t_recovery')
    option = 'status-format[0]'
    template = tmux('show-options', '-gv', option).stdout.rstrip('\n')
    existing_mouse = tmux('list-keys', '-T', 'root', 'MouseDown1Status').stdout
    assert tmux('set-option', '-g', 'status-right', 'Custom clock %H:%M').returncode == 0
    for _ in range(2):
        appearance.install(recovery, socket, 'dev-colors-1')
    configured = tmux('show-options', '-v', '-t', 'dev-colors-1:', option).stdout.rstrip('\n')
    managed = lambda content, fallback: f'#{{?#{{{appearance.COMMAND}}},{content},{fallback}}}'
    left = managed(appearance.MANAGED_LEADING, appearance.STATUS_LEFT)
    right = managed(appearance.MANAGED_TRAILING, appearance.STATUS_RIGHT)
    assert configured.count(left) == configured.count(right) == 1
    assert (configured.replace(left, appearance.STATUS_LEFT)
            .replace(right, appearance.STATUS_RIGHT)
            .replace(appearance.MANAGED_CURRENT_WINDOW, appearance.CURRENT_WINDOW)) == template
    assert tmux('show-options', '-qv', '-t', 'dev-colors-1:', 'status').stdout.strip() == '2'
    assert tmux('show-options', '-gv', 'status').stdout.strip() == 'on'
    assert tmux('show-options', '-qv', '-t', 'dev-colors-1:', 'status-format[1]').stdout.strip() == appearance.STATUS_MODELS
    # Recreate the old session-local layout, including the previous clock filter.
    for badge, hint in ((appearance.LEGACY_CLICK_BADGE, appearance.LEGACY_HINT),
                        (appearance.LEGACY_PANE_BADGE, appearance.STATUS_HINT)):
        legacy = template.replace(appearance.STATUS_LEFT, appearance.STATUS_IDENTITY + badge + hint)
        legacy = legacy.replace(appearance.STATUS_RIGHT, appearance.LEGACY_RIGHT)
        assert tmux('set-option', '-t', 'dev-colors-1:', option, legacy).returncode == 0
        appearance.install(recovery, socket, 'dev-colors-1')
        assert tmux('show-options', '-v', '-t', 'dev-colors-1:', option).stdout.rstrip('\n') == configured
    assert tmux('show-options', '-gv', 'status-right').stdout.rstrip('\n') == 'Custom clock %H:%M'
    previous = template.replace(appearance.STATUS_LEFT, appearance.STATUS_IDENTITY + appearance.STATUS_BADGE + appearance.STATUS_TITLE)
    previous = previous.replace(appearance.STATUS_RIGHT, appearance.STATUS_TRAILING.replace(
        appearance.RIGHT_CONTENT, appearance.CLOCK_RIGHT_CONTENT))
    previous = previous.replace(appearance.CURRENT_WINDOW, appearance.MANAGED_CURRENT_WINDOW)
    assert tmux('set-option', '-t', 'dev-colors-1:', option, previous).returncode == 0
    appearance.install(recovery, socket, 'dev-colors-1')
    assert tmux('show-options', '-v', '-t', 'dev-colors-1:', option).stdout.rstrip('\n') == configured
    assert tmux('list-keys', '-T', 'root', 'MouseDown1Status').stdout == existing_mouse
    assert appearance.STATUS_NOTE in tmux('list-keys', '-N', '-T', 'root', appearance.STATUS_KEY).stdout


def test_rendered_action_and_title_follow_the_active_pane(appearance, server):
    socket, tmux = server
    appearance.install(appearance.helper('t_recovery'), socket, 'dev-colors-1')
    line = tmux('show-options', '-v', '-t', 'dev-colors-1:', 'status-format[0]').stdout.rstrip()

    def render(width=80):
        return tmux('display-message', '-p', '-t', 'dev-colors-1:', line.replace('#{client_width}', str(width))).stdout

    tmux('select-pane', '-t', '%0', '-T', '⠋ Color terminal stages | colors-1')
    text = render()
    assert text.index('colors-1') < text.index('Color terminal stages') < text.index('Mark priority')
    assert text.count('Color terminal stages') == 1
    assert not re.search(r'\d\d:\d\d', text)
    assert not re.search(r'\d\d-\w{3}-\d\d', text)
    assert all(item not in text for item in ('0:sleep', 'Shift-P', '⠋', '[colors-1]', '[ ★', '[ ☆'))
    appearance.set_option(tmux, '%0', appearance.PRIORITY, '1')
    compact = render(36)
    assert '★ Priority' in compact and 'Col' in compact and '…' in compact
    tmux('select-pane', '-t', '%0', '-T', 'Revised | other')
    assert 'Revised | other' in render()
    tmux('new-window', '-d', '-n', 'shell', '-t', 'dev-colors-1:', 'sleep 20')
    assert '1:shell' in render()
    tmux('select-window', '-t', 'dev-colors-1:1')
    assert 'Mark priority' not in render() and '1:shell' in render()


def test_status_badge_respects_existing_mouse_binding(appearance, server):
    socket, tmux = server
    recovery = appearance.helper('t_recovery')
    assert tmux('bind-key', '-N', 'User mouse shortcut', '-T', 'root', appearance.STATUS_KEY,
                'display-message', 'user').returncode == 0
    appearance.install(recovery, socket, 'dev-colors-1')
    assert not appearance.get(tmux, '%0', appearance.CLICK)
    assert appearance.get(tmux, '%0', appearance.SHORTCUT) == appearance.PRIORITY_LABEL
    assert 'User mouse shortcut' in tmux('list-keys', '-N', '-T', 'root', appearance.STATUS_KEY).stdout
    assert appearance.PRIORITY_ACTION in tmux('show-options', '-v', '-t', 'dev-colors-1:', 'status-format[0]').stdout


def test_workflow_line_uses_resolved_models_and_active_palette(appearance, server):
    socket, tmux = server
    appearance.install(appearance.helper('t_recovery'), socket, 'dev-colors-1')
    workflow = appearance.helper('t_workflow')
    policy = workflow['defaults']()['workflows']['standard']
    policy['model_stages'] = ['planning', 'implementation']
    policy['planning'].update(model='gpt-6.2-astra', effort='high')
    policy['implementation'].update(model='gpt-6.2-sol', effort='high')
    policy.setdefault('behaviors', {})['pr_review'] = 'skip'
    appearance.paint_workflow(tmux, '%0', workflow, policy, 'codex', 'workflow', 'planning')

    def render(theme, width):
        expression = appearance.STATUS_MODELS.replace('#{theme}', theme)
        # The stored pane format is dynamically evaluated at the client width.
        stage_format = appearance.get(tmux, '%0', appearance.WORKFLOW_STATUS)
        expression = expression.replace('#{E:@t-appearance-workflow}', stage_format)
        expression = expression.replace('#{theme}', theme).replace('#{client_width}', str(width))
        return tmux('display-message', '-p', '-t', '%0', expression).stdout

    dark = render('dark', 80)
    assert '#[fg=#eef2fa bg=#22423f bold underscore]Plan' in dark
    assert '#[fg=#8ee2cd bg=#22423f bold]Astra' in dark
    assert re.sub(r'#\[[^\]]*\]', '', dark).strip() == 'Plan Astra high   Build Sol high   Deliver Sol high'
    light = render('light', 44)
    assert '#[fg=#006458 bg=#d5eee7 bold]Astra' in light
    assert re.sub(r'#\[[^\]]*\]', '', light).strip() == 'Plan Astra Build Sol Ship Sol'
    appearance.paint_workflow(tmux, '%0', workflow, policy, 'codex', 'session', 'waiting')
    waiting = render('dark', 44)
    assert 'Waiting' in waiting and 'bg=#22423f' not in waiting
    assert ' Wait ' in render('dark', 36)
    appearance.paint_workflow(tmux, '%0', workflow, policy, 'codex', 'session', 'idle')
    assert 'Waiting' not in render('dark', 44)


@pytest.mark.parametrize('status', ['off', 'on', '2', '3'])
def test_custom_status_rows_are_not_replaced(appearance, server, status):
    socket, tmux = server
    target = 'dev-colors-1:'
    tmux('set-option', '-t', target, 'status', status)
    tmux('set-option', '-t', target, 'status-format[1]', 'User status')
    appearance.install(appearance.helper('t_recovery'), socket, 'dev-colors-1')
    assert tmux('show-options', '-qv', '-t', target, 'status').stdout.strip() == status
    assert tmux('show-options', '-qv', '-t', target, 'status-format[1]').stdout.strip() == 'User status'


def test_failed_two_row_install_restores_inherited_status(appearance, server):
    _, tmux = server
    target = '=dev-colors-1:'
    before = tmux('show-options', '-Aqv', '-t', target, 'status-format').stdout

    def fail_final_write(*args):
        if args == ('set-option', '-t', target, 'status', '2'):
            return subprocess.CompletedProcess(args, 1, '', 'Test failure')
        return tmux(*args)

    appearance.install_status_badge(fail_final_write, 'dev-colors-1', '%0')
    assert tmux('show-options', '-Aqv', '-t', target, 'status').stdout.strip() == 'on'
    assert tmux('show-options', '-qv', '-t', target, 'status-format').stdout == ''
    assert tmux('show-options', '-Aqv', '-t', target, 'status-format').stdout == before


def test_local_activity_uses_the_managed_title_and_keeps_its_activity(appearance, server, tmp_path, monkeypatch):
    socket, tmux = server
    local = load_script(REPO_ROOT / 'libexec/t_local_status.py', 'local_status')
    monkeypatch.setattr(local, 'tmux', lambda _socket, *args: tmux(*args))
    monkeypatch.setattr(local, 'watcher_status', lambda *_: 'Result ready')
    tmux('select-pane', '-t', '%0', '-T', 'Color terminal stages | colors-1')
    assert 'Color terminal stages' in local.render('left', tmp_path, socket, '%0')
    local.install(socket, 'dev-colors-1', tmp_path)
    appearance.install(appearance.helper('t_recovery'), socket, 'dev-colors-1')
    assert local.render('left', tmp_path, socket, '%0') == ''
    assert 'Result ready' in local.render('right', tmp_path, socket, '%0')
    tmux('set-option', '-t', 'dev-colors-1', 'status', 'on')
    assert 'Color terminal stages' in local.render('left', tmp_path, socket, '%0')
