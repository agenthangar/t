"""Shared renderer navigation and review gates, without changing terminal or disk state."""
import io

import pytest


def make_ui(t, keys, width=80, height=24):
    ui = t._RailUI.__new__(t._RailUI)
    ui.st, ui.out = t.Style(tty=True), io.StringIO()
    ui.rail = f'  {ui.st.y}│{ui.st.r}'
    ui.painted = [0]
    ui.cols, ui.lines = lambda: width, lambda: height
    events = iter(keys)
    ui.read_key = lambda: next(events)
    ui.raw = lambda: None
    ui.restored = False
    ui.restore = lambda: setattr(ui, 'restored', True)
    ui.frames = []
    ui.repaint = lambda lines: ui.frames.append(lines)
    return ui


@pytest.mark.parametrize('width,height', [(80, 24), (44, 20), (32, 12), (20, 8)])
@pytest.mark.parametrize('multi', [False, True])
def test_shared_picker_keeps_focus_and_primary_action_visible(t_mod, width, height, multi):
    ui = make_ui(t_mod, ['j'] * 11 + ['?'] + ['k'] * 11 + ['s'], width, height)
    rows = [(str(i), f'Setting {i}') for i in range(12)]
    assert ui.pick('Settings', rows, multi=multi,
        values={str(i): 'a-long-model-or-host-name' for i in range(10)},
        sections={'0': 'Defaults', '5': 'Hosts', '9': 'Finish'},
        primary=('s', 'SAVE', ''), shortcuts={'s': 'save'}) == 'save'
    for frame in ui.frames:
        assert sum(t_mod._term_rows(line, width) for line in frame) <= height
        assert any('▸' in line for line in frame)
        assert any('SAVE' in line for line in frame)
        assert all(t_mod._vis_len(line) <= width for line in frame)


def test_setup_and_install_share_rows_and_review_without_running_actions(t_mod, monkeypatch):
    items = t_mod._setup_items([('/code/api', 'api')], {'existing': '/code/existing'}, [], {}, None, [], '~/code')
    ui = make_ui(t_mod, ['space', 'enter', 'y'])
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    assert t_mod._setup_wizard(items, set())
    assert t_mod._setup_result(items)[0] == {'api': '/code/api'}
    assert ui.restored
    assert any('REVIEW CHANGES' in line for frame in ui.frames for line in frame)
    assert any('APPLY CHANGES' in line for frame in ui.frames for line in frame)
    # Reuse the same real renderer with a fresh key stream for install.
    events = iter(['space', 'enter', 'n', 'q'])
    ui.read_key = lambda: next(events)
    items = [{'t': 'header', 'label': 'Agent CLIs'},
             {'t': 'locked', 'alias': 'existing', 'value': 'installed'},
             {'t': 'toggle', 'alias': 'codex', 'value': 'install', 'checked': False}]
    assert not t_mod._install_wizard(items, lambda: [{'step': 'install', 'do': 'run', 'label': 'Install codex'}])
    assert items[-1]['checked'] and ui.restored


def test_review_can_scroll_and_back_out_with_action_still_visible(t_mod):
    ui = make_ui(t_mod, ['j'] * 10 + ['n'], 44, 20)
    assert ui.page('Review', [f'      Change {i}' for i in range(40)], ('y', 'n'),
                   keys='n back', primary=('y', 'APPLY', '')) == 'n'
    for frame in ui.frames:
        assert any('APPLY' in line for line in frame)
        assert sum(t_mod._term_rows(line, 44) for line in frame) <= 20


@pytest.mark.parametrize('state,keys,expected,status', [
    ('logged-in', ['space', 'space', 'enter'], ['claude', 'codex'], 0),
    ('logged-in', ['space', 'space', 'q'], [], 130),
    ('logged-in', ['enter', 'q'], [], 130),
    ('logged-out', ['space', 'enter'], ['codex', 'cursor'], 0),
])
def test_login_multiselect_runs_directly(t_mod, monkeypatch, state, keys, expected, status):
    # Healthy tools can be selected; recommended logins can be deselected.
    args = t_mod.build_parser().parse_args(['login'])
    ui = make_ui(t_mod, keys)
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    monkeypatch.setattr(t_mod.sys, 'stdin', type('TTY', (), {'isatty': lambda _: True})())
    monkeypatch.setattr(t_mod.sys, 'stdout', type('TTY', (), {'isatty': lambda _: True})())
    calls = []
    monkeypatch.setattr(t_mod, '_login_probe', lambda agent: {
        'state': 'logged-in' if agent in calls else state,
        'bin': '/fixture/' + agent, 'expires': None})
    def run(cmd, **kwargs):
        assert ui.restored
        calls.append(cmd[0].rsplit('/', 1)[-1])
        return type('Result', (), {'returncode': 0})()
    monkeypatch.setattr(t_mod, '_login_run', run)
    monkeypatch.setattr('builtins.print', lambda *a, **kw: None)
    assert t_mod.cmd_login(None, args) == status
    assert calls == expected
    assert any('Pick agents to log in' in line for frame in ui.frames for line in frame)
    assert any('enter' in line and 'RUN LOGINS' in line for frame in ui.frames for line in frame)
    assert not any('REVIEW' in line or 'review' in line or 'confirm' in line
                   for frame in ui.frames for line in frame)
    assert ui.restored


@pytest.mark.parametrize('key,confirmed', [('enter', True), ('y', True), ('q', False), ('abort', False)])
def test_install_review_defaults_to_enter(t_mod, monkeypatch, key, confirmed):
    ui = make_ui(t_mod, ['enter', key])
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    items = [{'t': 'toggle', 'alias': 'codex', 'value': 'install', 'checked': True}]
    plan = lambda: [{'step': 'install', 'do': 'run', 'label': 'Install codex'}]
    assert t_mod._install_wizard(items, plan) is confirmed
    assert any('enter' in line and 'RUN COMMANDS' in line for frame in ui.frames for line in frame)
    assert ui.restored


@pytest.mark.parametrize('save_key', ['enter', 'y'])
def test_config_review_saves_with_enter_or_y(t_mod, monkeypatch, tmp_path, save_key):
    monkeypatch.setattr(t_mod, 'CONFIG', str(tmp_path / 'config.sh'))
    # Choose Desktop app, review pending changes, and accept using the real page.
    ui = make_ui(t_mod, ['j', 'enter', 'j', 'enter', 's', save_key])
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    edited = t_mod._config_wizard(t_mod.Config())
    assert edited.open_mode_default == 'app'
    assert ui.restored
    review = [line for frame in ui.frames if any('Review local settings' in line for line in frame)
              for line in frame]
    assert any('enter' in line and 'SAVE CHANGES' in line for line in review)
    assert not any('no binding' in line for line in review)


def test_dirty_config_ctrl_c_aborts_and_restores_terminal(t_mod, monkeypatch, tmp_path):
    local = tmp_path / 'local.zsh'
    local.write_text('# keep me\n')
    monkeypatch.setenv('T_LOCAL_RC', str(local))
    monkeypatch.setattr(t_mod, 'CONFIG', str(tmp_path / 'config.sh'))
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(t_mod.sys.stdout, 'isatty', lambda: True)
    ui = make_ui(t_mod, ['j', 'enter', 'j', 'enter', 'abort'])
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    monkeypatch.setattr(t_mod, '_config_write',
                        lambda *a: pytest.fail('aborted config wrote changes'))
    monkeypatch.setattr(t_mod, 'zsh_capture',
                        lambda *a: pytest.fail('aborted config synced settings'))

    cfg = t_mod.Config()
    cfg.open_mode_default = 'cli'
    assert t_mod.cmd_config(cfg, t_mod.build_parser().parse_args(['config'])) == 130
    assert ui.restored
    assert local.read_text() == '# keep me\n'
