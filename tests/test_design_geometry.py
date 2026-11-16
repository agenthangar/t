"""Coordinate contracts: rendered columns, shared gutters and real terminal frames."""
import pytest
from test_tui import make_ui
from test_picker_ui import picker  # noqa: F401

SIZES = [(80, 24), (44, 20), (32, 12), (120, 12), (160, 40), (59, 20), (60, 20), (61, 20)]


@pytest.mark.parametrize('width,height', SIZES)
@pytest.mark.parametrize('plain', [False, True])
def test_rail_columns_keep_coordinates(t_mod, monkeypatch, width, height, plain):
    if plain:
        monkeypatch.setenv('NO_COLOR', '1')
    else:
        monkeypatch.delenv('NO_COLOR', raising=False)
    ui = make_ui(t_mod, [], width, height)
    labels = ['Longest setting label', '界面 café e\u0301', 'Short']
    label_width = max(t_mod._display_width(label) for label in labels)
    coordinates, frames = [], []
    # Sorting/filtering changes rows, not the snapshot's shared label width.
    for selected, mark, label in [(False, '✓', labels[0]), (True, '✓', labels[1]),
                                   (False, '☐', labels[2]), (True, '☐', labels[2])]:
        frame = ui.row(label, 'READY', selected=selected, mark=mark, label_width=label_width)
        frames.append(frame)
        clean = [t_mod._ANSI_RE.sub('', line) for line in frame]
        row = next(i for i, line in enumerate(clean) if 'READY' in line)
        coordinates.append((row, t_mod._display_width(clean[row].split('READY')[0])))
        assert all(t_mod._vis_len(line) <= width for line in frame), frame
        assert ('▸' in clean[0]) == selected
        if selected: assert t_mod._display_width(clean[0].split('▸')[0]) == 4
    assert len(set(coordinates)) == 1, (coordinates, frames)
    assert coordinates[0][0] == (1 if width < 60 else 0)
    if width < 60: assert coordinates[0][1] == 8


@pytest.mark.parametrize('width,height', SIZES)
def test_table_coordinates_and_narrow_stacking(t_mod, width, height):
    headings = ('SESSION', 'STATE', 'TASK')
    rows = [('界面-longest-session-name', 'busy', 'First'), ('short', '-', 'Second')]
    frame = t_mod._table_lines(headings, rows, width=width)
    assert all(t_mod._display_width(line) <= width for line in frame), frame
    if frame[0].strip().startswith('SESSION:'):
        assert all(any(label + ':' in line for line in frame) for label in headings)
        assert all(any(value in line for line in frame) for value in ('First', 'Second'))
    else:
        tokens = [('SESSION', 'STATE', 'TASK'), *rows]
        coordinates = [[t_mod._display_width(line[:line.index(cell)]) for cell in cells]
                       for line, cells in zip(frame, tokens)]
        assert coordinates[0] == coordinates[1] == coordinates[2], (coordinates, frame)
        assert coordinates[0][0] == 2
    assert t_mod._table_lines(headings, [], width=width)


@pytest.mark.parametrize('width,height', SIZES[:5])
@pytest.mark.fzf_compat
def test_real_picker_columns_do_not_shift_on_search(picker, width, height):
    start, wait, keys, finish = picker
    start(profile='list-rich', mode='list', width=width, height=height)
    wait('2/2 (0)')
    keys('Down')
    screen = wait('Older task')
    def coordinates(frame):
        line = next(line for line in frame.splitlines() if 'api-2' in line and 'Older task' in line)
        return (line.index('api-2'), line.index('Older task'))
    expected = coordinates(screen)
    keys('-l', 'Older')
    filtered = wait('1/2 (0)')
    assert coordinates(filtered) == expected, (expected, coordinates(filtered), screen, filtered)
    keys('M-v')
    sorted_frame = wait('Sort: session')
    assert coordinates(sorted_frame) == expected, (expected, sorted_frame)
    resized_width = 80 if width != 80 else 44
    keys.resize(resized_width, 24)
    resized = wait('Older task', columns=resized_width)
    assert '▸' in resized
    finish('Escape', 130)
