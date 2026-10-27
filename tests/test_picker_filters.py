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
        'agent': 'codex', 'interface': 'cli', 'host': 'local', 'repo': 'api'}
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


@pytest.mark.parametrize('profile', ['open', 'kill'])
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
@pytest.mark.parametrize('profile', ['open', 'resume', 'kill'])
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
