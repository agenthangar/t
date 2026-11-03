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


@pytest.mark.parametrize('width', [28, 32, 44, 68])
@pytest.mark.parametrize('profile', ['resume', 'kill', 'open'])
def test_every_filter_key_survives_long_values(filters, width, profile):
    facets = dict(agent='codex-long-界面', host='a-very-long-host-alias', repo='long-repository-name')
    text = filters.header(profile, 0, facets, 17, 'SESSION  WORKING ON', width)
    assert all(filters.display_width(line) <= width for line in text.splitlines())
    for letter in ('A', 'F', 'L'):
        assert ('^' if width <= 40 else 'Ctrl-') + letter in text
    assert '17 hidden' in text and text.count('Filters ON') == 1
    assert 'Ctrl-G' not in text and '^G' not in text
    assert len(text.splitlines()) <= (9 if profile == 'resume' else 5)


@pytest.mark.parametrize('width', [28, 32, 68])
def test_open_header_keeps_every_column_shortcut_visible(filters, width):
    # Facet controls live inside settings; the main header advertises one entry.
    text = filters.header('open', 0, dict(agent='codex-long-界面'), 17, 'SESSION  TASK', width)
    assert all(filters.display_width(line) <= width for line in text.splitlines())
    assert ('^F filters' if width <= 40 else 'Ctrl-F filters') in text
    assert 'Filters ON' in text and '17 hidden' in text
    assert 'Ctrl-T' not in text and 'Ctrl-G' not in text
    assert len(text.splitlines()) <= 5


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
    assert render('unattached') == ROWS[::2]
    assert render('unattached') == ROWS[::2]
    assert json.loads((root / 'facets.json').read_text())['agent'] == 'codex'
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


def test_resume_sorts_preserve_snapshot_filters_and_reset_defaults(filters, tmp_path):
    def row(repo, slot, agent, host, turn):
        return '\t'.join([repo, slot, f'{repo}-{slot}', '/wt', 'date', 'task', '-', '-', host, agent, turn, 'Display'])
    rows = [row('web', '10', 'codex', 'mini', '2026-10-20T10:00:00Z'),
            row('api', '2', 'claude', '-', '2026-10-22T10:00:00Z'),
            row('api', '10', 'codex', '-', '2026-10-21T10:00:00Z'),
            row('api', '3', 'codex', '-', 'garbage')]
    rows += ['api\t9\tlegacy\t/wt\tdate\ttask\t-\t-\t-\tcodex\tLegacy']
    for name in ('source', 'all', 'rows-0', 'rows-1'):
        (tmp_path / name).write_text('\n'.join(rows) + '\n')
    def render(action):
        filters.render(tmp_path, 'resume', action, 68)
        return (tmp_path / 'current').read_text().splitlines()
    assert render('current') == [rows[1], rows[2], rows[0], rows[3], rows[4]]
    expected = {'repo': [1, 2, 3, 4, 0], 'agent': [1, 2, 0, 3, 4],
                'host': [1, 2, 3, 4, 0], 'session': [1, 3, 4, 2, 0]}
    for mode, indices in expected.items():
        assert render('sort-' + mode) == [rows[i] for i in indices]
        assert 'Sort: ' + filters.RESUME_SORTS[mode][1] in (tmp_path / 'current-header').read_text()
    render('agent')  # Claude facet remains active while sort changes.
    assert render('sort-repo') == [rows[1]]
    assert json.loads((tmp_path / 'facets.json').read_text())['agent'] == 'claude'
    assert render('all') == [rows[i] for i in expected['repo']]
    assert (tmp_path / 'sort').read_text() == 'repo'
    assert render('default') == [rows[1], rows[2], rows[0], rows[3], rows[4]]
    assert (tmp_path / 'sort').read_text() == 'turn'
    assert render('sort-turn') == render('current')
    assert filters.sort_resume([], {}) == []
    assert filters.turn_epoch(None) == float('-inf')
    assert filters.turn_epoch('2026-10-22T10:00:00') == float('-inf')


def test_resume_turn_enrichment_uses_exact_sources_and_never_remote_local_copy(filters, tmp_path):
    def row(slot, sid, agent, location='-', path='-'):
        return '\t'.join(['api', slot, sid, '/wt', '● active' if location != '-' else 'old mtime',
                          'Task', location, '-', '-', agent, str(path)])
    claude = tmp_path / 'exact.jsonl'
    claude.write_text(json.dumps({'type': 'user', 'timestamp': '2026-10-20T10:00:00Z',
                                 'message': {'content': 'First task'}}) + '\n' +
                      json.dumps({'type': 'assistant', 'timestamp': '2026-10-25T10:00:00Z'}) + '\n')
    codex = tmp_path / 'rollout.jsonl'
    codex.write_text(json.dumps({'type': 'event_msg', 'timestamp': '2026-10-22T10:00:00Z',
                                'payload': {'type': 'task_started'}}) + '\n')
    rows = [row('1', 'claude-id', 'claude', path=claude),
            row('2', 'codex-id', 'codex', path=codex),
            row('3', 'codex-id', 'codex', 'mini'),
            row('4', 'unknown', 'claude', 'here')]
    result = filters.resume_turns('\n'.join(rows), str(tmp_path), str(tmp_path))
    assert [r.split('\t')[1] for r in result] == ['2', '1', '3', '4']
    assert result[0].split('\t')[10] == '2026-10-22T10:00:00Z'
    assert result[1].split('\t')[10] == '2026-10-20T10:00:00Z'
    assert all(r.split('\t')[10] == '-' for r in result[2:])
    assert '● active · Last: -' in result[2]
    assert '● active · Last: -' in result[3]
    # A local known owner may resolve activity without upgrading the action id.
    project = tmp_path / '.claude/projects/-wt'
    project.mkdir(parents=True)
    (project / 'owner-id.jsonl').write_text(claude.read_text())
    owner = 'local\towner-id\t/wt\tapi-5\tdetached\tactive\tTask\tclaude'
    result = filters.resume_turns(row('5', '-', 'claude', 'here'), str(tmp_path), str(tmp_path), owner)
    assert result[0].split('\t')[2] == '-'
    assert result[0].split('\t')[10] == '2026-10-20T10:00:00Z'


@pytest.mark.parametrize('width', [28, 32, 68])
def test_resume_header_retains_sort_controls_and_row_space(filters, width):
    text = filters.header('resume', 0, dict.fromkeys(['agent', 'host', 'repo']), 2, '', width)
    assert all(filters.display_width(line) <= width for line in text.splitlines())
    for letter in ('T', 'R', 'G', 'O', 'V'):
        assert ('M-' if width <= 36 else 'Alt-') + letter in text
    for letter in ('L', 'A', 'F', '/'):
        assert ('^' if width <= 36 else 'Ctrl-') + letter in text
    assert 'Sort: last turn' in text
    if width <= 36:
        assert len(text.splitlines()) <= 9


def test_resume_remote_history_retains_exported_turn_without_local_transcript(filters, tmp_path):
    row = 'repo\t4\tsaved\t/other/home/tree\told date\tSaved task\t-\thistory\tremote\tcodex\t-\t2026-10-28T10:11:12Z'
    result = filters.resume_turns(row, str(tmp_path), str(tmp_path))
    assert result[0].split('\t')[10] == '2026-10-28T10:11:12Z'
    invalid = row.replace('2026-10-28T10:11:12Z', 'invalid')
    assert filters.resume_turns(invalid, str(tmp_path), str(tmp_path))[0].split('\t')[10] == '-'


@pytest.mark.parametrize('profile', ['open', 'kill', 'resume'])
def test_filters_off_on_preserve_saved_choices_and_sort(filters, tmp_path, profile):
    root = snapshot(tmp_path, profile if profile != 'resume' else 'open')
    if profile == 'resume':
        (root / 'source').write_text('api\t1\tsid1\t/wt\tdate\ttitle\t-\t-\t-\tcodex\n')
    filters.render(root, profile, 'agent', 68)
    saved_rows = (root / 'current').read_text()
    saved = (root / 'facets.json').read_text()
    filters.render(root, profile, 'off', 68)
    assert set((root / 'current').read_text().splitlines()) == set(ROWS)
    assert (root / 'facets.json').read_text() == saved
    assert 'Filters OFF' in (root / 'current-header').read_text()
    filters.render(root, profile, 'off', 68)
    filters.render(root, profile, 'on', 68)
    assert (root / 'current').read_text() == saved_rows
    assert (root / 'facets.json').read_text() == saved


@pytest.mark.parametrize('profile', ['open', 'kill', 'resume'])
def test_settings_apply_cancel_and_reset_preserve_sort(filters, tmp_path, monkeypatch, profile):
    root = snapshot(tmp_path, profile if profile != 'resume' else 'open')
    filters.render(root, profile, 'current', 68)
    names = list(json.loads((root / 'facets.json').read_text()))
    # A column edit and state edit remain staged until Apply.
    picks = iter([2, 1, 1, 0])
    monkeypatch.setattr(filters, 'pick_menu', lambda *args: next(picks))
    monkeypatch.setattr(filters, 'pick_value', lambda *args: ('codex',))
    filters.render(root, profile, 'settings', 68)
    assert json.loads((root / 'facets.json').read_text())['agent'] == 'codex'
    saved_mask = (root / 'show-all').read_text()
    saved = (root / 'facets.json').read_text()
    # Reset staged values then cancel the settings screen: nothing commits.
    picks = iter([len(names) + 2, None])
    filters.render(root, profile, 'settings', 68)
    assert (root / 'facets.json').read_text() == saved
    assert (root / 'show-all').read_text() == saved_mask
    # Cancel the nested value and state screens, then Apply unchanged settings.
    picks = iter([2, 1, None, 0])
    monkeypatch.setattr(filters, 'pick_value', lambda *args: None)
    filters.render(root, profile, 'settings', 68)
    assert (root / 'facets.json').read_text() == saved
    assert (root / 'show-all').read_text() == saved_mask
    if profile == 'resume':
        filters.render(root, profile, 'sort-repo', 68)
    picks = iter([len(names) + 2, 0])
    filters.render(root, profile, 'settings', 68)
    assert all(v is None for v in json.loads((root / 'facets.json').read_text()).values())
    assert (root / 'show-all').read_text() == '0'
    if profile == 'resume':
        assert (root / 'sort').read_text() == 'repo'


@pytest.mark.parametrize('code,expected', [(0, 1), (1, None), (130, None)])
def test_settings_shared_picker_selection_and_cancel(filters, tmp_path, monkeypatch, code, expected):
    from types import SimpleNamespace
    def run(argv, **kwargs):
        assert '_t_fzf_single' in argv[2]
        assert (tmp_path / 'settings').read_text() == '0\tApply\n1\tState\n'
        return SimpleNamespace(returncode=code, stdout='1\tState\n')
    monkeypatch.setattr(filters.subprocess, 'run', run)
    assert filters.pick_menu(tmp_path, ['Apply', 'State'], 'Filters', 'Esc discard') == expected


def test_settings_picker_failure(filters, tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(filters.subprocess, 'run', lambda *args, **kwargs: SimpleNamespace(returncode=2))
    with pytest.raises(RuntimeError, match='settings picker failed'):
        filters.pick_menu(tmp_path, ['Apply'], 'Filters', '')


def test_header_color_clips_before_ansi_and_plain_has_explicit_state(filters):
    import re
    for enabled in (True, False):
        plain = filters.header('resume', 0, {'agent': '界' * 40}, 3, '', 28, enabled=enabled)
        colored = filters.header('resume', 0, {'agent': '界' * 40}, 3, '', 28, enabled=enabled, color=True)
        assert re.sub(r'\x1b\[[0-9;]*m', '', colored) == plain
        assert ('\x1b' in colored) == enabled
        assert 'Filters ' + ('ON' if enabled else 'OFF') in plain
        assert all(filters.display_width(line) <= 28 for line in plain.splitlines())


def test_resume_merged_toggle_uses_metadata_and_preserves_choices(filters, tmp_path, monkeypatch):
    states = ['MERGED', 'OPEN', 'CLOSED', 'UNKNOWN', 'NONE', 'MIXED', 'invalid']
    rows = ['\t'.join(['api', str(i), f'sid{i}', '/wt', 'date', 'Task · #12 merged',
                      '-', 'history', '-', 'codex', '-', 'org/api', state, 'Display'])
            for i, state in enumerate(states)]
    for name in ('source', 'all', 'rows-0', 'rows-1'):
        (tmp_path / name).write_text('\n'.join(rows) + '\n')
    def render(action):
        filters.render(tmp_path, 'resume', action, 68)
        return (tmp_path / 'current').read_text().splitlines()
    assert render('current') == rows
    assert render('merged') == rows[1:]
    assert 'Alt-M show merged' in (tmp_path / 'current-header').read_text()
    assert render('sort-repo') == rows[1:]
    assert render('off') == rows
    assert render('on') == rows[1:]
    assert render('merged') == rows
    assert render('merged') == rows[1:]
    render('off')
    assert render('merged') == rows[1:]  # Turning it on while OFF enables filters.
    monkeypatch.setattr(filters, 'pick_menu', lambda *args: None)
    assert render('settings') == rows[1:]
    picks = iter([5, 0])  # Reset staged choices, then Apply.
    monkeypatch.setattr(filters, 'pick_menu', lambda *args: next(picks))
    assert render('settings') == rows
    assert (tmp_path / 'sort').read_text() == 'repo'
    render('merged')
    assert render('default') == rows
    render('merged')
    assert render('all') == rows
    for name in ('source', 'all', 'rows-0', 'rows-1'):
        (tmp_path / name).write_text(rows[0] + '\n')
    assert render('merged') == []
    assert '1 hidden' in (tmp_path / 'current-header').read_text()
    assert render('merged') == [rows[0]]


def test_resume_turns_preserve_explicit_pr_and_repository_identity(filters, tmp_path):
    row = 'api\t2\tsid\t/wt\tdate\ttitle merged\t-\thistory\tmini\tcodex\t-\t-\torg/api\tMERGED'
    result = filters.resume_turns(row, str(tmp_path), str(tmp_path))[0].split('\t')
    assert result[11:] == ['org/api', 'MERGED']
    assert filters.resume_turns(row.replace('MERGED', 'invalid'), str(tmp_path), str(tmp_path))[0].split('\t')[12] == 'UNKNOWN'
