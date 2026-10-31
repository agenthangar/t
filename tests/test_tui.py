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


@pytest.mark.parametrize('width', [80, 160, 240])
@pytest.mark.parametrize('selected', [False, True])
def test_shared_row_uses_available_width_for_full_labels(t_mod, width, selected):
    ui = make_ui(t_mod, [], width)
    label = 'Preview approval before PR'
    lines = ui.row(label, 'Required', selected=selected, label_width=len(label))
    plain = '\n'.join(t_mod._ANSI_RE.sub('', line) for line in lines)
    assert label in plain and 'Required' in plain
    assert all(t_mod._vis_len(line) <= width for line in lines)


@pytest.mark.parametrize('width', [32, 44, 60, 80, 160])
@pytest.mark.parametrize('selected', [False, True])
def test_shared_row_bounds_long_labels_and_retains_values(t_mod, width, selected):
    ui = make_ui(t_mod, [], width)
    lines = ui.row('Long setting label ' * 20, 'Required', selected=selected, mark='◼', label_width=380)
    plain = '\n'.join(t_mod._ANSI_RE.sub('', line) for line in lines)
    assert 'Required' in plain
    assert ('▸' in plain) is selected
    assert '◼' in plain
    assert len(lines) == (2 if width < 60 else 1)
    assert all(t_mod._vis_len(line) <= width for line in lines)


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


def test_dirty_config_ctrl_c_offers_discard_and_restores_terminal(t_mod, monkeypatch, tmp_path):
    local = tmp_path / 'local.zsh'
    local.write_text('# keep me\n')
    monkeypatch.setenv('T_LOCAL_RC', str(local))
    monkeypatch.setattr(t_mod, 'CONFIG', str(tmp_path / 'config.sh'))
    monkeypatch.setattr(t_mod.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(t_mod.sys.stdout, 'isatty', lambda: True)
    ui = make_ui(t_mod, ['j', 'enter', 'j', 'enter', 'abort', 'j', 'j', 'enter'])
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    monkeypatch.setattr(t_mod, '_config_write',
                        lambda *a: pytest.fail('aborted config wrote changes'))
    monkeypatch.setattr(t_mod, 'zsh_capture',
                        lambda *a: pytest.fail('aborted config synced settings'))

    cfg = t_mod.Config()
    cfg.open_mode_default = 'cli'
    assert t_mod.cmd_config(cfg, t_mod.build_parser().parse_args(['config'])) == 130
    assert ui.restored
    assert any('unsaved' in line for frame in ui.frames for line in frame)
    assert any('Discard changes and exit' in line for frame in ui.frames for line in frame)
    assert local.read_text() == '# keep me\n'


@pytest.mark.parametrize('key', ['q', 'esc'])
@pytest.mark.parametrize('multi,keys,expected', [(True, ['space'], ['a']),
                                                (False, ['j'], 'b')])
def test_picker_back_snapshots_current_choices_without_accepting(t_mod, key, multi, keys, expected):
    ui = make_ui(t_mod, [*keys, key])
    snapshots = []
    assert ui.pick('Setup', [('a', 'Alpha'), ('b', 'Beta')], multi=multi,
                   on_back=snapshots.append, back_label='back') is None
    assert snapshots == [expected]
    assert any('q/esc back' in line for frame in ui.frames for line in frame)
    assert any('ctrl-c cancel' in line for frame in ui.frames for line in frame)


def test_picker_ctrl_c_cancels_without_back_callback(t_mod):
    ui = make_ui(t_mod, ['space', 'abort'])
    snapshots = []
    with pytest.raises(KeyboardInterrupt):
        ui.pick('Setup', [('a', 'Alpha')], multi=True, on_back=snapshots.append,
                back_label='cancel')
    assert snapshots == []
    assert any('q/esc cancel' in line for frame in ui.frames for line in frame)


@pytest.mark.parametrize('sections', [{}, {'frontend': 'Change types'}])
def test_unset_choice_has_one_nonselectable_gap_before_other_choices(t_mod, sections):
    ui = make_ui(t_mod, ['j', 'enter'])
    frames = []
    ui.frame = lambda label, display, cur, *args: frames.append((list(display), cur))
    assert ui.pick('Applies to', [('', 'Not set'), ('frontend', 'Frontend'), ('cli', 'CLI')],
                   sections=sections) == 'frontend'
    display, selected = frames[-1]
    first_end = max(i for i, (_, index) in enumerate(display) if index == 0)
    second_start = min(i for i, (_, index) in enumerate(display) if index == 1)
    gap = display[first_end + 1:second_start]
    assert selected == 1
    assert sum(line == ui.rail and index is None for line, index in gap) == 1
    assert all(index is None for _, index in gap)
    assert len(gap) == (2 if sections else 1)


@pytest.mark.parametrize('marks,expected', [(None, ['']), ({0}, ['']), ({1}, ['frontend']),
    ({0, 1}, ['frontend']), ({1, 2}, ['frontend', 'cli'])])
def test_checklist_unset_initialization_normalizes_marks_by_index(t_mod, marks, expected):
    ui = make_ui(t_mod, ['enter'])
    assert ui.pick('Applies to', [('', 'Not set'), ('frontend', 'Frontend'), ('cli', 'CLI')],
                   multi=True, marked=marks, empty_choice='') == expected


@pytest.mark.parametrize('keys,marks,expected', [
    (['space', 'enter'], {1, 2}, ['']),
    (['j', 'space', 'enter'], {0}, ['frontend']),
    (['j', 'space', 'enter'], {1}, ['']),
    (['j', 'space', 'space', 'enter'], {0}, ['frontend', 'cli']),
    (['space', 'enter'], {0}, ['']),
])
def test_checklist_unset_reset_and_real_choices_are_exclusive(t_mod, keys, marks, expected):
    ui = make_ui(t_mod, keys)
    assert ui.pick('Applies to', [('', 'Not set'), ('frontend', 'Frontend'), ('cli', 'CLI')],
                   multi=True, marked=marks, empty_choice='') == expected


def test_checklist_without_empty_choice_preserves_independent_marks(t_mod):
    ui = make_ui(t_mod, ['space', 'enter'])
    assert ui.pick('Legacy checklist', [('', 'Optional'), ('a', 'Alpha')],
                   multi=True, marked={1}) == ['', 'a']
    ui = make_ui(t_mod, ['enter'])
    assert ui.pick('No reset row', [('a', 'Alpha')], multi=True, empty_choice='') == []


@pytest.mark.parametrize('key', ['q', 'esc', 'abort'])
def test_unset_checklist_cancel_does_not_mutate_caller_marks(t_mod, key):
    marks = {1, 2}
    ui = make_ui(t_mod, ['space', key])
    snapshots = []
    if key == 'abort':
        with pytest.raises(KeyboardInterrupt):
            ui.pick('Applies to', [('', 'Not set'), ('frontend', 'Frontend'), ('cli', 'CLI')],
                    multi=True, marked=marks, empty_choice='', on_back=snapshots.append)
        assert snapshots == []
    else:
        assert ui.pick('Applies to', [('', 'Not set'), ('frontend', 'Frontend'), ('cli', 'CLI')],
                       multi=True, marked=marks, empty_choice='', on_back=snapshots.append) is None
        assert snapshots == [['']]
    assert marks == {1, 2}


@pytest.mark.parametrize('width,height', [(80, 24), (32, 12), (20, 8)])
def test_unset_gap_checklist_keeps_viewport_focus_and_action_visible(t_mod, width, height):
    ui = make_ui(t_mod, ['j'] * 10 + ['k'] * 10 + ['enter'], width, height)
    rows = [('', 'Not set')] + [(str(i), 'Long choice label ' + str(i)) for i in range(10)]
    assert ui.pick('Applies to', rows, multi=True, empty_choice='',
                   sections={'0': 'Change types'}, primary=('enter', 'SELECT', '')) == ['']
    for frame in ui.frames:
        assert sum(t_mod._term_rows(line, width) for line in frame) <= height
        assert any('▸' in line for line in frame)
        assert any('SELECT' in line for line in frame)
        assert all(t_mod._vis_len(line) <= width for line in frame)
