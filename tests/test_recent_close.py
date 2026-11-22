"""Verified closure recency, with private history and disposable picker snapshots."""
import copy
import json
import shutil
from pathlib import Path
import runpy
import shlex
from types import SimpleNamespace

import pytest

from conftest import load_script
from test_history_picker import history, rows, app  # noqa: F401
from test_picker_filters import filters  # noqa: F401
from test_lifecycle_engine import Adapter  # noqa: F401
from test_agent_seam import zsh  # noqa: F401
from test_picker_ui import picker  # noqa: F401

ROOT = Path(__file__).resolve().parents[1]


def closed_rows():
    data = rows(3)
    # Newest turn is session-0, newest close is session-2. File activity disagrees.
    data[0]['updated'] = 9999999999
    for i, stamp in [(1, 300), (2, 400)]:
        data[i]['associations'].append(dict(workspace=f'/gone/{i}', slot=str(i + 1),
                                           host='local', evidence='close', observed=stamp))
    return data


def test_close_evidence_is_not_activity_or_an_old_reopened_session(history):
    data = closed_rows()
    epoch = history._SORT['closed_epoch']
    assert epoch(data[0]) == float('-inf')
    assert epoch(data[2]) == 400
    for evidence in ('launch', 'resume', 'handoff'):
        row = copy.deepcopy(data[2])
        row['associations'].append(dict(evidence=evidence, observed=401))
        assert epoch(row) == float('-inf')
    for change in ({'owners': [{'state': 'detached'}]},
                   {'last_turn': '2026-11-16T01:00:00Z'}):
        assert epoch({**data[2], **change}) == float('-inf')
    for invalid in (None, '400', True, float('nan'), float('inf'), -1, 253402300800):
        assert epoch({'associations': [{'evidence': 'close', 'observed': invalid}]}) == float('-inf')
    assert history._SORT['latest_closed']({}) is None
    tied = {'a': {'closed': '2026-11-16T01:00:00Z'}, 'b': {'closed': '2026-11-16T01:00:00Z'}}
    assert history._SORT['latest_closed'](tied) is None


def test_close_to_discovery_and_exact_resume(history, monkeypatch, tmp_path, zsh):
    engine = load_script(ROOT / 'libexec/t_lifecycle.py', 'recent_close_lifecycle')
    # Exercise the same post-release history write as the lifecycle record effect.
    store = history.Store(tmp_path / 'private-history')
    data = closed_rows()
    for row in data:
        row['sources'] = [{'host': 'local', 'path': '/sample.jsonl'}]
        row['updated'] = 500
        row['associations'] = row['associations'][:1]
        store.merge(row)
    released = []
    monkeypatch.setattr(engine, 'wait_released', lambda *a: released.append(a))
    class ClosingAdapter(Adapter):
        def call(self, action, *args):
            if action == 'record':
                assert released
                event, session, workspace, agent, sid = args
                assert event == 'close' and sid == 'session-2'
                store.merge(dict(repo='org/repo', sid=sid, agent=agent, updated=500,
                                 associations=[dict(workspace=workspace, slot='3', host='local',
                                                    evidence=event, observed=500)]))
            return super().call(action, *args)
    engine.execute('close', ['dev-api-3', str(tmp_path), 'session-2', 'claude'], ClosingAdapter())
    saved = store.records()
    store.close()
    discovery = runpy.run_path(str(ROOT / 'libexec/t_session_discovery.py'))
    adapter = SimpleNamespace(repository_alias=lambda _: 'api', worktree_enabled=lambda _: True,
                              cfg=SimpleNamespace(repos={'api': '/sample'}), pr_states=lambda _: None)
    protocol = list(discovery['_catalog_candidates']({'conversations': saved}, adapter, 'local'))
    view = {'hosts': [{'host': 'local', 'value': {'resume': protocol}}]}
    protocol = list(discovery['candidates'](view, ['api']))
    assert len(protocol) == 3 and len(protocol[0].split('\t')) == 13
    shell = ('_t_resume_history_candidates() { print -r -- ' + shlex.quote('\n'.join(protocol)) + '; }; '
             '_t_fzf_filtered() { print -u2 -- "$1"; print -r -- "${${(f)1}[1]}"; }; '
             '_t_resume_history_launch() { print -u2 -- "EXACT $2 $3"; print dev-api-99; }; '
             '_t_resume api --days all')
    result = zsh(shell, _tty=True)
    assert result.returncode == 0, result.stdout
    assert 'EXACT session-2 claude' in result.stdout
    assert '↶' in result.stdout
    assert 'EXACT session-0' not in result.stdout
    assert 'attach-session -t =dev-api-99:' in zsh.log.read_text()
    # Old peer records remain accepted, with no fabricated closure timestamp.
    legacy = '\t'.join(protocol[0].split('\t')[:12])
    assert list(discovery['candidates']({'hosts': [{'host': 'mini', 'value': {'resume': [legacy]}}]}, ['api']))


def test_marker_survives_sort_and_filter(history, filters, tmp_path):
    data = closed_rows()
    assert history.page_rows(data, sort='closed')[0][0]['sid'] == 'session-2'
    assert history.page_rows(data, sort='turn')[0][0]['sid'] == 'session-0'
    # Searching out the newest close does not promote an older close to latest.
    text = history.picker_rows(data, 'Conversation 1', 50, 1, sort='closed')
    assert '↶' not in text
    text = history.picker_rows(data, '', 50, 1, sort='turn')
    assert text.splitlines()[2].split('\t')[1].startswith('↶ ')
    assert '↶' not in history.picker_rows(data, '', 50, 1, notice='Discovery incomplete')
    lines = []
    for i, row in enumerate(data):
        lines.append('\t'.join(['api', str(i + 1), row['sid'], '/wt', 'date', row['title'], '-',
                                'history', '-', row['agent'], row['last_turn'], row['repo'],
                                'OPEN', history._SORT['closed_time'](row), f'api-{i + 1}  {row["title"]}']))
    source = filters.mark_resume('\n'.join(lines))
    for name in ('source', 'all', 'rows-0', 'rows-1'):
        (tmp_path / name).write_text(source + '\n')
    filters.render(tmp_path, 'resume', 'current', 68)
    assert (tmp_path / 'sort').read_text() == 'closed'
    assert 'session-2' in (tmp_path / 'current').read_text().splitlines()[0]
    assert '↶ last closed' in (tmp_path / 'current-header').read_text()
    filters.render(tmp_path, 'resume', 'next-sort', 68, 'WORKING ON')
    assert (tmp_path / 'current-header').read_text().splitlines()[-1] == '  WORKING ON'
    assert (tmp_path / 'sort').read_text() == 'turn'
    output = (tmp_path / 'current').read_text().splitlines()
    assert output[0].split('\t')[2] == 'session-0'
    assert output[2].split('\t')[-1].startswith('↶ ')
    filters.render(tmp_path, 'resume', 'match-agent', 68, identity=('api', '2', 'session-1'))
    assert '↶ ' not in (tmp_path / 'current').read_text()
    filters.render(tmp_path, 'resume', 'reset-filters', 68)
    assert (tmp_path / 'sort').read_text() == 'turn'
    assert '↶ ' in (tmp_path / 'current').read_text()
    assert '↶' not in filters.mark_resume('\n'.join(lines), incomplete=True)
    assert 'unknown' in filters.closure_legend(filters.metadata(source, 'resume'), True)


@pytest.mark.parametrize('width', [20, 28, 32, 44, 59, 60, 61, 62, 80, 100, 120])
def test_history_marker_columns_align(history, width):
    data = closed_rows()
    header = history.picker_header(data, '', 50, 1, width, sort='closed').splitlines()[-1]
    lines = [history.picker_row(row, data, width) for row in data]
    assert all(history.display_width(line) <= width for line in [header, *lines])
    assert lines[2].startswith('↶ ') and all(line.startswith('  ') for line in lines[:2])
    columns = history.picker_columns(width - 2)
    gap = 2 if width - 2 >= 60 else 1
    offset = 2 + sum(cap for _, cap in columns) + len(columns) * gap
    if offset < width:
        assert header[offset:].startswith('WORK')
        assert all(line[offset:].startswith('C') for line in lines)
    assert all(line[2:] == history.format_row(row, width - 2) for line, row in zip(lines, data))


@pytest.mark.parametrize('width,height', [(80, 24), (44, 20), (32, 12)])
@pytest.mark.parametrize('plain', [False, True])
@pytest.mark.fzf_compat
def test_real_recent_close_marker_keeps_focus(picker, width, height, plain):
    start, wait, keys, finish = picker
    start(profile='resume-closed', mode='resume', width=width, height=height, monochrome=plain)
    screen = wait('Sort: last closed', focused='Older api')
    marked = next(line for line in screen.splitlines() if 'Older api' in line)
    assert '↶' in marked
    frames = Path('artifacts/recent-close')
    frames.mkdir(parents=True, exist_ok=True)
    (frames / f'{width}x{height}-{"plain" if plain else "color"}.txt').write_text(screen)
    before = marked.index('Older api')
    heading = next(line for line in screen.splitlines() if 'WORKING ON' in line)
    assert heading.index('WORKING ON') == before
    keys('C-y')
    screen = wait('Sort: turn' if width <= 44 else 'Sort: last turn', ordered=['Newest web', 'Older api'])
    assert '↶' in next(line for line in screen.splitlines() if 'Older api' in line)
    keys('-l', 'Older')
    # The viewport helper searches canonical rows before reloading fzf, so its
    # input count is the single matching row rather than the full snapshot.
    screen = wait('1/1 (0)', focused='Older api')
    matched = next(line for line in screen.splitlines() if 'Older api' in line)
    assert '↶' in matched
    assert matched.index('Older api') == before
    finish('Escape', 130)


def test_history_accepts_last_closed_after_fzf_strips_color(history, app, monkeypatch):
    if not shutil.which('fzf'):
        pytest.skip('fzf required')
    data = closed_rows()
    original = history.subprocess.run
    monkeypatch.delenv('NO_COLOR', raising=False)
    monkeypatch.setenv('TERM', 'xterm-256color')
    def select(argv, **kwargs):
        helper = shlex.split(argv[8])
        state = Path(helper[4])
        settings = json.loads(state.read_text())
        assert settings['sort'] == 'closed'
        # Force the real ANSI input path and let fzf produce its actual result.
        plain = state.with_suffix('.rows').read_text()
        colored = plain.replace('↶', '\033[36m↶\033[0m')
        result = original(['fzf', '--ansi', '--no-extended', '--exact', '--filter=Conversation 2'], input=colored,
                          text=True, capture_output=True, env=kwargs['env'])
        assert result.returncode == 0 and '\033[' not in result.stdout
        return SimpleNamespace(returncode=0, stdout='\nenter\n' + result.stdout)
    monkeypatch.setattr(history.subprocess, 'run', select)
    selected, key, query = app.pick(data, 'repo', multi=True)
    assert selected == [data[2]] and key == 'enter' and query == ''
    assert app.picker_sort == ('closed', True)
