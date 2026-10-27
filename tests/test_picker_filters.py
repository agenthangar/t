"""Filtering uses immutable snapshot metadata, never displayed task text."""
import json
from pathlib import Path

import pytest
from conftest import load_script

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ('local\tsid1\t/code/api\tapi-1\tdetached\tactive\tclaude desktop task\tcodex\n'
          'local\tsid2\t/code/api\tapi-2\tattached\tactive\tFirst task\tclaude\n'
          'mini\tsid3\t/code/web\tweb-3\tapp\tnone\tReview\tcodex\n'
          'mini\tsid4\t/code/web\tweb:abc\tattached\tactive\tWork\tclaude\n')
ROWS = ['local\tapi-1\tdetached\tclaude desktop task',
        'local\tapi-2\tattached\tFirst task',
        'mini\tweb-3\tapp\tReview', 'mini\tweb:abc\tattached\tWork']


@pytest.fixture
def filters(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'libexec'))
    return load_script(ROOT / 'libexec/t_picker_filters.py', 'picker_filters')


def snapshot(tmp_path, profile='open'):
    (tmp_path / 'source').write_text(SOURCE)
    (tmp_path / 'all').write_text('\n'.join(ROWS) + '\n')
    views = {0: ROWS[::2], 1: ROWS}
    if profile == 'kill':
        views = {0: ROWS[:1], 1: ROWS[:2], 2: ROWS[::2], 3: ROWS}
    for mask, rows in views.items():
        (tmp_path / f'rows-{mask}').write_text('\n'.join(rows) + '\n')
    return tmp_path


def test_metadata_keeps_facets_separate_from_task_text_and_identity(filters):
    records = filters.metadata(SOURCE + 'bad\nlocal\tsid\t/root\tsolo\tdetached\tactive\ttask\n', 'open')
    assert records[('local', 'api-1', 'detached')] == {
        'agent': 'codex', 'interface': 'cli', 'host': 'local', 'repo': 'api',
        'model': 'unknown', 'session': 'api-1', 'context': 'active',
        'task': 'claude desktop task', 'progress': 'not marked'}
    assert records[('mini', 'web:abc', 'attached')]['repo'] == 'web'
    assert records[('local', 'solo', 'detached')]['agent'] == 'claude'
    assert filters.choose(ROWS, records, {'agent': 'codex', 'interface': 'cli'}) == ROWS[:1]
    assert filters.choose(['missing\tidentity\trow'], records, {'agent': 'unknown'}) == ['missing\tidentity\trow']
    assert filters.choose(ROWS, records, {'agent': None}) == ROWS


def test_resume_metadata_reports_origin_without_inventing_interface(filters):
    source = ('api\t1\tsid1\t/wt\tdate\ttitle\t-\t-\t\tcodex\n'
              'api\t2\tsid2\t/wt\tdate\ttitle\t-\t-\tmini\tclaude\n'
              'api\t3\tsid3\t/wt\tdate\ttitle\there\t-\t-\tcodex\n'
              'api\t4\tsid4\t/wt\tdate\ttitle\tstudio\t-\t-\t\nshort\n')
    records = filters.metadata(source, 'resume')
    assert [r['host'] for r in records.values()] == ['local', 'mini', 'local', 'studio']
    assert records[('api', '4', 'sid4')]['agent'] == 'unknown'
    assert all('interface' not in r for r in records.values())


@pytest.mark.parametrize('profile', ['kill'])
def test_combined_filters_empty_results_and_state_toggles(filters, tmp_path, profile):
    root = snapshot(tmp_path, profile)
    def render(action):
        filters.render(root, profile, action, 72, 'SESSION  TASK')
        return (root / 'current').read_text().splitlines()
    assert render('current') == (ROWS[::2] if profile == 'open' else ROWS[:1])
    assert render('agent') == []  # Claude owners are attached.
    assert render('first') == ([ROWS[1], ROWS[3]] if profile == 'open' else [ROWS[1]])
    assert render('host') == [ROWS[1]]
    assert render('host') == ([ROWS[3]] if profile == 'open' else [])
    assert render('interface') == ([ROWS[3]] if profile == 'open' else [])
    assert render('interface') == []  # No Claude desktop in this snapshot.
    assert render('agent') == ([ROWS[2]] if profile == 'open' else [])
    if profile == 'kill':
        assert render('second') == [ROWS[2]]
    assert render('repo') == []  # api on mini has no rows.
    assert render('repo') == [ROWS[2]]
    assert render('repo') == [ROWS[2]]  # Return to all repositories.
    assert render('all') == ROWS
    assert all(v is None for v in json.loads((root / 'facets.json').read_text()).values())
    assert render('default') == (ROWS[::2] if profile == 'open' else ROWS[:1])
    assert (root / 'current-header').read_text().endswith('SESSION  TASK\n')


def test_resume_cycles_and_invalid_controls(filters, tmp_path):
    root = snapshot(tmp_path)
    (root / 'source').write_text('api\t1\tsid1\t/wt\tdate\ttitle\t-\t-\t-\tcodex\n')
    row = 'api\t1\tsid1\tDisplay'
    for name in ('rows-0', 'rows-1', 'all'):
        (root / name).write_text(row + '\n')
    filters.render(root, 'resume', 'agent', 32)
    assert json.loads((root / 'facets.json').read_text()) == dict(agent='codex', host=None, repo=None)
    assert 'Ctrl-D' not in (root / 'current-header').read_text()
    filters.render(root, 'resume', 'agent', 32)
    assert json.loads((root / 'facets.json').read_text())['agent'] is None
    with pytest.raises(ValueError, match='unknown picker filter'):
        filters.render(root, 'resume', 'interface', 32)
    with pytest.raises(ValueError, match='unsupported state filter'):
        filters.render(root, 'resume', 'second', 32)


@pytest.mark.parametrize('width', [32, 44, 72])
@pytest.mark.parametrize('profile', ['resume', 'kill'])
def test_every_filter_key_survives_long_values(filters, width, profile):
    facets = dict(agent='codex-long-界面', host='a-very-long-host-alias', repo='long-repository-name')
    if profile != 'resume':
        facets['interface'] = 'desktop'
    text = filters.header(profile, 0, facets, 17, 'SESSION  WORKING ON', width)
    assert all(filters.display_width(line) <= width for line in text.splitlines())
    for key in ('Ctrl-G', 'Ctrl-O', 'Ctrl-R', 'Ctrl-A', 'Ctrl-F'):
        assert key in text
    assert ('Ctrl-D' in text) == (profile != 'resume')
    assert '17 hidden' in text
    assert 'Ctrl-L live: hidden' in text if profile == 'resume' else 'Ctrl-T attached: hidden' in text
    assert ('Ctrl-P' in text) == (profile == 'kill')


@pytest.mark.parametrize('width', [28, 32, 68])
def test_open_header_keeps_every_column_shortcut_visible(filters, width):
    text = filters.header('open', 0, dict(agent='codex-long-界面'), 17, 'SESSION  TASK', width)
    assert all(filters.display_width(line) <= width for line in text.splitlines())
    for letter in ('T', 'F', 'A', 'G', 'P', 'D', 'B', 'O', 'R', 'V', 'E', 'W'):
        assert ('^' + letter if width <= 36 else 'Ctrl-' + letter) in text
    assert 'View: unattached' in text and '17 hidden' in text
    if width <= 36:
        assert '^ = Ctrl' in text and '1 filters' in text
        assert len(text.splitlines()) <= 9
    else:
        assert 'agent=codex-long-界面' in text
    assert 'View: all' in filters.header('open', 1, {}, 0, '', width)


def test_open_exclusive_views_combine_column_choices_and_clear(filters, tmp_path, monkeypatch):
    root = snapshot(tmp_path)
    def render(action, facet=None):
        filters.render(root, 'open', action, 72, 'SESSION  TASK', facet)
        return (root / 'current').read_text().splitlines()
    assert render('current') == ROWS[::2]
    assert render('first') == ROWS[1::2]
    assert render('first') == ROWS[1::2]
    assert render('all') == ROWS
    monkeypatch.setattr(filters, 'pick_value', lambda *args: ('codex',))
    assert render('pick', 'agent') == ROWS[::2]
    assert render('first') == []
    monkeypatch.setattr(filters, 'pick_value', lambda *args: None)
    assert render('pick', 'agent') == []  # Esc leaves the existing filter alone.
    monkeypatch.setattr(filters, 'pick_value', lambda *args: (None,))
    assert render('pick', 'agent') == ROWS[1::2]
    assert render('default') == ROWS[::2]
    assert all(v is None for v in json.loads((root / 'facets.json').read_text()).values())
    with pytest.raises(ValueError, match='unknown picker filter'):
        render('pick', 'missing')


def test_open_progress_matches_displayed_marker_without_claiming_completion(filters):
    source = SOURCE.replace('First task', 'First task · #12 merged, still in progress')
    records = filters.metadata(source, 'open')
    assert records[('local', 'api-2', 'attached')]['progress'] == 'in progress'
    assert records[('local', 'api-1', 'detached')]['progress'] == 'not marked'


@pytest.mark.parametrize('code,output,expected', [(0, '0\tAll (clear filter)\n', (None,)),
    (0, '1\tclaude\n', ('claude',)), (1, '', None), (130, '', None)])
def test_column_value_picker_selection_and_cancellation(filters, tmp_path, monkeypatch, code, output, expected):
    from types import SimpleNamespace
    def run(argv, **kwargs):
        assert '_t_fzf_single' in argv[2]
        assert argv[-1] == 'agent'
        assert (tmp_path / 'values').read_text() == '0\tAll (clear filter)\n1\tclaude\n2\tcodex\n'
        return SimpleNamespace(returncode=code, stdout=output)
    monkeypatch.setattr(filters.subprocess, 'run', run)
    assert filters.pick_value(tmp_path, 'agent', filters.metadata(SOURCE, 'open'), 'codex') == expected


def test_progress_value_picker_explains_marker_and_reports_failure(filters, tmp_path, monkeypatch):
    from types import SimpleNamespace
    def run(argv, **kwargs):
        assert 'not marked is not proof of completion' in argv[-2]
        return SimpleNamespace(returncode=2, stdout='')
    monkeypatch.setattr(filters.subprocess, 'run', run)
    with pytest.raises(RuntimeError, match='picker failed'):
        filters.pick_value(tmp_path, 'progress', filters.metadata(SOURCE, 'open'), None)
