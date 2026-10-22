"""History search and picker operate on disposable snapshots, never personal sessions."""
import importlib.util
import json
import os
import re
from pathlib import Path
import shlex
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def history(tmp_path, monkeypatch):
    for name, path in [('HOME', tmp_path / 'home'), ('XDG_STATE_HOME', tmp_path / 'state'),
                       ('XDG_CONFIG_HOME', tmp_path / 'config'), ('XDG_CACHE_HOME', tmp_path / 'cache'),
                       ('XDG_DATA_HOME', tmp_path / 'data'), ('CODEX_HOME', tmp_path / 'codex')]:
        path.mkdir()
        monkeypatch.setenv(name, str(path))
    spec = importlib.util.spec_from_file_location('history_picker_test', ROOT / 'libexec/t_history.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def app(history, tmp_path):
    cfg = SimpleNamespace(repos={'repo': str(tmp_path / 'repo')}, hosts={},
                          repo_of_dir=lambda cwd: ('repo', '19'))
    store = history.Store()
    value = history.HistoryCommand(cfg, {'_t_source_root': lambda: str(ROOT)}, store)
    value.identity = lambda alias: 'org/repo'
    yield value
    store.close()


def rows(count=120):
    return [dict(repo='org/repo', agent='codex' if i % 2 else 'claude', sid=f'session-{i}',
                 title=f'Conversation {i}', updated=200 - i,
                 associations=[dict(workspace=f'/gone/{i}', slot=str(i % 3 + 1), host='local', branch=f'branch-{i}')],
                 sources=[], prs=[], mentions=[]) for i in range(count)]


def test_default_pages_are_50_and_global_search_precedes_slicing(history):
    data = rows()
    selected, total = history.page_rows(data)
    assert total == 120
    assert [r['sid'] for r in selected] == [f'session-{i}' for i in range(50)]
    assert [r['sid'] for r in history.page_rows(data, page=2)[0]] == [f'session-{i}' for i in range(50, 100)]
    assert len(history.page_rows(data, page=3)[0]) == 20
    assert history.page_rows(data, page=4) == ([], 120)
    data[119]['title'] = 'Late unique Needle result'
    selected, total = history.page_rows(data, 'NEEDLE result')
    assert total == 1
    assert selected[0]['sid'] == 'session-119'
    # Search includes branch/SID/workspace metadata and case-insensitive terms.
    assert history.page_rows(data, 'BRANCH-117 session-117')[1] == 1


@pytest.mark.parametrize('limit,page', [(0, 1), (-1, 1), (50, 0), (50, -1)])
def test_invalid_page_parameters_refuse(history, limit, page):
    with pytest.raises(history.HistoryError, match='positive'):
        history.page_rows(rows(), limit=limit, page=page)


def test_deterministic_order_for_identical_dates_and_at_most_three_pr_summaries(history):
    data = rows(3)
    for r in data:
        r['updated'] = 100
    data[1]['agent'] = 'claude'
    assert [r['sid'] for r in history.page_rows(list(reversed(data)))[0]] == ['session-0', 'session-1', 'session-2']
    data[0]['prs'] = [dict(number=i, state='OPEN', url=f'https://github.com/org/repo/pull/{i}') for i in range(1, 7)]
    rendered = history.format_row(data[0])
    assert all(f'#{i}O' in rendered for i in range(1, 4))
    assert all(f'#{i}O' not in rendered for i in range(4, 7))
    assert '+3' in rendered
    assert all(f'#{i}[OPEN]' in history.details(data[0]) for i in range(1, 4))
    assert all(pr['url'] in history.details(data[0]) for pr in data[0]['prs'])
    data[0]['title'] = ''
    data[0]['owners'] = [{'host': 'remote', 'slot': 'repo-4', 'state': 'detached', 'context': ''}]
    assert 'Untitled conversation' in history.format_row(data[0])
    assert 'Host: remote' in history.details(data[0])
    assert 'Session: repo-4' in history.details(data[0])
    assert '·' in history.format_row(data[2])
    assert 'Status: Saved' in history.details(data[2])


def test_picker_indices_remain_global_after_search_and_page_sorting(history):
    data = list(reversed(rows()))
    output = history.picker_rows(data, '', 50, 2).splitlines()
    assert len(output) == 50
    index = int(output[0].split('\t')[0])
    assert data[index]['sid'] == 'session-50'
    result = history.picker_rows(data, 'branch-119', 50, 1)
    assert data[int(result.split('\t')[0])]['sid'] == 'session-119'


def test_picker_actions_page_clamp_search_reset_and_preview(history, tmp_path, capsys):
    snapshot, state = tmp_path / 'records.json', tmp_path / 'page.json'
    data = rows()
    history.private_write(snapshot, data)
    history.private_write(state, dict(page=1, query='', limit=50))
    history.picker([str(snapshot), str(state), 'next', ''])
    out = capsys.readouterr().out
    assert 'Conversation 50' in out
    assert json.loads(state.read_text())['page'] == 2
    assert 'Page 2 · 120 conversations · 50 per page' in state.with_suffix('.header').read_text()
    history.picker([str(snapshot), str(state), 'next', ''])
    assert 'Conversation 100' in capsys.readouterr().out
    history.picker([str(snapshot), str(state), 'next', ''])
    capsys.readouterr()
    assert json.loads(state.read_text())['page'] == 3
    history.picker([str(snapshot), str(state), 'show', 'session-119'])
    out = capsys.readouterr().out
    assert 'Conversation 119' in out
    assert json.loads(state.read_text())['page'] == 1
    history.picker([str(snapshot), str(state), 'previous', 'session-119'])
    capsys.readouterr()
    assert json.loads(state.read_text())['page'] == 1
    history.picker([str(snapshot), str(state), 'details', '119'])
    preview = capsys.readouterr().out
    assert 'ID: session-119' in preview and '(codex)' in preview
    assert 'Working on: Conversation 119' in preview


@pytest.mark.parametrize('code,stdout,expected', [(130, '', None), (1, '', None),
    (0, 'searched\nctrl-r\n', None), (0, 'searched\nenter\n', None),
    (0, 'Conversation\nenter\n0\trow\n', 'session-0')])
def test_picker_explicit_selection_refresh_cancel_and_private_snapshots(history, app, monkeypatch, code, stdout, expected):
    calls = []
    monkeypatch.setenv('COV_CORE_SOURCE', 'private')
    def fzf(argv, **kwargs):
        calls.append(argv)
        assert '_t_fzf_paginated' in argv[2]
        assert not any(part.startswith(('--bind=', '--header=', '--no-multi')) for part in argv)
        assert not any(k.startswith('COV_CORE_') for k in kwargs['env'])
        words = shlex.split(argv[8])
        snapshot = Path(words[3])
        assert snapshot.is_file()
        assert json.loads(snapshot.read_text())[0]['sid'] == 'session-0'
        actual = 'Conversation\nenter\n' + history.picker_rows(rows(1), 'Conversation', 50, 1) if expected else stdout
        return SimpleNamespace(returncode=code, stdout=actual)
    monkeypatch.setattr(history.subprocess, 'run', fzf)
    selected, key, query = app.pick(rows(1), 'repo', query='Conversation' if expected else '')
    assert (selected['sid'] if selected else None) == expected
    assert len(calls) == 1
    assert key == ('ctrl-r' if 'ctrl-r' in stdout else 'enter' if expected else 'cancel')


@pytest.mark.parametrize('code,stdout,error', [(2, '', 'unavailable'), (0, '\nenter\n99\tinvalid\n', 'selection')])
def test_picker_invalid_exit_and_indices_do_not_resume(history, app, monkeypatch, code, stdout, error):
    monkeypatch.setattr(history.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=code, stdout=stdout))
    with pytest.raises(history.HistoryError, match=error):
        app.pick(rows(1), 'repo')


def test_ambiguous_bare_repository_slot_choice_then_conversation_never_autoresumes(app, monkeypatch):
    calls = []
    def pick(options, alias, *a):
        calls.append((options, alias))
        return options[0], 'enter', ''
    monkeypatch.setattr(app, 'pick', pick)
    assert app.choose_slot(rows(3), 'repo') in {'1', '2', '3'}
    assert len(calls) == 1
    assert all(r['agent'] == 'slot' for r in calls[0][0])
    monkeypatch.setattr(app, 'pick', lambda *a: (None, '', ''))
    assert app.choose_slot(rows(3), 'repo') is None


def test_scope_bare_slot_and_explicit_repo_slot_are_read_only(history, app):
    assert app.scope(['19']) == ('repo', '19')
    assert app.scope(['repo', '4']) == ('repo', '4')
    assert app.scope(['repo']) == ('repo', '')
    assert app.scope([]) == ('repo', '19')
    for target in (['repo', '0'], ['repo', '-1'], ['repo', 'bad'], ['repo', '2', 'extra'], ['missing']):
        with pytest.raises(history.HistoryError):
            app.scope(target)


@pytest.mark.parametrize('hostile_defaults', [False, True])
def test_real_isolated_tmux_picker_paging_global_search_refresh_and_cancel(history, tmp_path, hostile_defaults):
    """fzf actually reloads its rows under a private PTY; no desktop windows."""
    import shutil
    import subprocess
    import sys
    import tempfile
    import time
    if not shutil.which('tmux') or not shutil.which('fzf'):
        pytest.skip('isolated PTY picker requires tmux and fzf')
    env = {k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_')}
    env.pop('TMUX', None)
    env['TERM'] = 'xterm-256color'
    env['FZF_DEFAULT_OPTS'] = '--bind=enter:abort,ctrl-r:abort,esc:accept' if hostile_defaults else ''
    options_file = tmp_path / 'fzf-defaults'
    options_file.write_text('--bind=enter:abort,ctrl-r:abort,esc:accept\n' if hostile_defaults else '')
    env['FZF_DEFAULT_OPTS_FILE'] = str(options_file)
    env['FZF_DEFAULT_COMMAND'] = ''
    data = rows()
    snapshot, result = tmp_path / 'input.json', tmp_path / 'selection.json'
    snapshot.write_text(json.dumps(data))
    harness = tmp_path / 'picker.py'
    harness.write_text('''import importlib.util, json, pathlib, sys
from types import SimpleNamespace
root, snapshot, result = map(pathlib.Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location('history_pty', root / 'libexec/t_history.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
cfg = SimpleNamespace(repos={'repo':'/unused'}, hosts={})
app = m.HistoryCommand(cfg, {'_t_source_root':lambda: str(root)})
original_run = m.subprocess.run
def capture(*args, **kwargs):
    value = original_run(*args, **kwargs)
    result.with_suffix('.fzf').write_text(value.stdout or '')
    return value
m.subprocess.run = capture
try:
    row, key, query = app.pick(json.loads(snapshot.read_text()), 'repo')
    result.write_text(json.dumps({'sid': row['sid'] if row else None, 'key':key, 'query':query}))
finally:
    app.store.close()
''')
    with tempfile.TemporaryDirectory(prefix='t-history-pty-') as short:
        socket = str(Path(short) / 'tmux.sock')
        def tmux(*argv):
            return subprocess.run(['tmux', '-S', socket, '-f', '/dev/null', *argv], env=env,
                                  text=True, capture_output=True, timeout=10, check=True).stdout
        def screen():
            return tmux('capture-pane', '-p', '-t', 'picker', '-S', '-50')
        def wait_for(predicate, what):
            deadline = time.monotonic() + 10
            latest = ''
            while time.monotonic() < deadline:
                latest = screen()
                if predicate(latest):
                    return latest
                time.sleep(.05)
            pytest.fail(f'{what} did not appear in isolated picker:\n{latest}')
        command = shlex.join([sys.executable, str(harness), str(ROOT), str(snapshot), str(result)])
        try:
            tmux('new-session', '-d', '-s', 'picker', '-x', '220', '-y', '45', command)
            tmux('set-option', '-t', 'picker', 'remain-on-exit', 'on')
            wait_for(lambda text: 'Conversation 0' in text and 'history repo>' in text and 'Page 1 · 120 conversations · 50 per page' in text, 'initial rows')
            tmux('send-keys', '-t', 'picker', 'C-f')
            wait_for(lambda text: 'Conversation 50' in text and 'Page 2 · 120 conversations · 50 per page' in text, 'next page rows')
            tmux('send-keys', '-t', 'picker', 'C-b')
            wait_for(lambda text: 'Conversation 0' in text, 'previous page rows')
            # This exact SID occurs on page 3, beyond fzf's initial 50 rows.
            tmux('send-keys', '-t', 'picker', '-l', 'session-119')
            wait_for(lambda text: 'Conversation 119' in text and 'Page 1 · 1 conversations · 50 per page' in text, 'global off-page search result')
            tmux('send-keys', '-t', 'picker', 'C-r')
            deadline = time.monotonic() + 10
            while not result.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            assert result.exists(), screen()
            assert json.loads(result.read_text()) == {'sid': None, 'key': 'ctrl-r', 'query': 'session-119'}
            tmux('kill-session', '-t', 'picker')
            result.unlink()
            tmux('new-session', '-d', '-s', 'picker', '-x', '220', '-y', '45', command)
            tmux('set-option', '-t', 'picker', 'remain-on-exit', 'on')
            wait_for(lambda text: 'Conversation 0' in text, 'second picker')
            tmux('send-keys', '-t', 'picker', 'Escape')
            deadline = time.monotonic() + 10
            while not result.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            assert result.exists(), screen()
            assert json.loads(result.read_text()) == {'sid': None, 'key': 'cancel', 'query': ''}
            tmux('kill-session', '-t', 'picker')
            result.unlink()
            tmux('new-session', '-d', '-s', 'picker', '-x', '220', '-y', '45', command)
            tmux('set-option', '-t', 'picker', 'remain-on-exit', 'on')
            wait_for(lambda text: 'Conversation 0' in text, 'third picker')
            tmux('send-keys', '-t', 'picker', '-l', 'session-119')
            wait_for(lambda text: re.search(r'│ ▸ [^\n]*Conversation 119', text) is not None and 'Page 1 · 1 conversations · 50 per page' in text, 'exact selected result')
            assert not result.exists(), 'single search result must wait for explicit acceptance'
            tmux('send-keys', '-t', 'picker', 'Enter')
            deadline = time.monotonic() + 10
            while not result.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            assert result.exists(), screen()
            assert json.loads(result.read_text()) == {'sid': 'session-119', 'key': 'enter', 'query': 'session-119'}, result.with_suffix('.fzf').read_text()
        finally:
            subprocess.run(['tmux', '-S', socket, 'kill-server'], env=env, capture_output=True, timeout=10)


@pytest.mark.parametrize('width,height', [(44, 20), (32, 12)])
def test_paginated_history_keeps_action_and_focused_row_visible_on_small_terminals(
        history, tmp_path, width, height):
    import shutil
    import subprocess
    import sys
    import tempfile
    import time
    if not shutil.which('tmux') or not shutil.which('fzf'):
        pytest.skip('isolated PTY picker requires tmux and fzf')
    env = {k: v for k, v in os.environ.items() if not k.startswith('COV_CORE_')}
    env.pop('TMUX', None)
    env.update(TERM='xterm-256color', FZF_DEFAULT_OPTS='', FZF_DEFAULT_COMMAND='')
    snapshot, result = tmp_path / 'input.json', tmp_path / 'selection.json'
    snapshot.write_text(json.dumps(rows(1)))
    harness = tmp_path / 'picker.py'
    harness.write_text('''import importlib.util, json, pathlib, sys
from types import SimpleNamespace
root, snapshot, result = map(pathlib.Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location('history_small_pty', root / 'libexec/t_history.py')
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
app = m.HistoryCommand(SimpleNamespace(repos={'repo':'/unused'}, hosts={}), {'_t_source_root':lambda: str(root)})
try:
    row, key, query = app.pick(json.loads(snapshot.read_text()), 'repo')
    result.write_text(json.dumps({'sid':row['sid'] if row else None, 'key':key}))
finally:
    app.store.close()
''')
    with tempfile.TemporaryDirectory(prefix='t-history-small-pty-') as short:
        socket = str(Path(short) / 'tmux.sock')
        command = shlex.join([sys.executable, str(harness), str(ROOT), str(snapshot), str(result)])
        def tmux(*argv):
            return subprocess.run(['tmux', '-S', socket, '-f', '/dev/null', *argv], env=env,
                                  capture_output=True, text=True, timeout=10, check=True).stdout
        try:
            tmux('new-session', '-d', '-s', 'picker', '-x', str(width), '-y', str(height), command)
            tmux('set-option', '-t', 'picker', 'remain-on-exit', 'on')
            deadline = time.monotonic() + 10
            captured = ''
            while time.monotonic() < deadline:
                captured = tmux('capture-pane', '-p', '-t', 'picker')
                if 'Enter resume' in captured and '▸' in captured and 'Conversat' in captured:
                    break
                time.sleep(.05)
            assert 'Enter resume' in captured and '▸' in captured and 'Conversat' in captured, captured
            tmux('send-keys', '-t', 'picker', 'Escape')
            deadline = time.monotonic() + 10
            while not result.exists() and time.monotonic() < deadline:
                time.sleep(.05)
            assert json.loads(result.read_text()) == {'sid': None, 'key': 'cancel'}
        finally:
            subprocess.run(['tmux', '-S', socket, 'kill-server'], env=env, capture_output=True, timeout=10)


def test_one_slot_can_be_inferred_without_accepting_a_conversation(app, monkeypatch):
    data = rows(1)
    monkeypatch.setattr(app, 'pick', lambda *a: pytest.fail('one slot is inferred before conversation picker'))
    assert app.choose_slot(data, 'repo') == '1'


def test_stale_query_reload_selection_retries_without_substituting_or_resuming(history, app, monkeypatch):
    data = rows()
    stale = history.picker_rows(data, 'session-11', 50, 1).splitlines()[0]
    monkeypatch.setattr(history.subprocess, 'run', lambda *a, **k: SimpleNamespace(returncode=0, stdout='session-119\nenter\n' + stale + '\n'))
    selected, key, query = app.pick(data, 'repo', query='session-119')
    assert selected is None
    assert key == 'retry'
    assert query == 'session-119'


def test_picker_hidden_metadata_retains_full_sid_for_native_exact_filtering(history):
    row = rows(120)[119]
    raw = history.picker_rows([row], 'session-119', 50, 1).strip().split('\t')
    assert len(raw) == 3
    assert json.loads(raw[2])['sid'] == 'session-119'
    assert 'session-119' not in raw[1]
