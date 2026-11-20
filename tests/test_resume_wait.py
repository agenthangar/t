"""Pending resume discovery is observable and skippable without changing workers."""
import copy
import json
import os
import sys
from types import SimpleNamespace

import pytest

from conftest import REPO_ROOT
from test_session_discovery import adapter, view  # noqa: F401
from test_tui import make_ui
from test_agent_seam import zsh  # noqa: F401
from test_picker_filters import filters  # noqa: F401
from pty_helpers import run_terminal


def pending(cold=False):
    data = view()
    data['hosts'][0]['refreshing'] = True
    if cold:
        data['hosts'][0]['value'] = None
        data['loading'] = True
    return data


@pytest.mark.parametrize('cold', [True])
@pytest.mark.parametrize('outcome', ['rows', 'empty', 'failed', 'unavailable'])
def test_resume_waits_beyond_thirty_seconds_until_settled(adapter, monkeypatch, cold, outcome):
    initial, done = pending(cold), view()
    if outcome == 'empty':
        done['hosts'][0]['value'] = {'rows': '', 'resume': []}
    elif outcome == 'failed':
        done['hosts'][0].update(error='offline', value=None, retry_at=99999)
    elif outcome == 'unavailable':
        done = {'unavailable': True}
    sequence = iter([initial, *[initial for _ in range(35)], *([done] * 5 if outcome == 'unavailable' else [done])])
    calls, clock = [], [0]
    monkeypatch.setattr(adapter.time, 'monotonic', lambda: clock[0])
    def discover(cfg, **kwargs):
        calls.append((cfg, kwargs))
        return next(sequence)
    monkeypatch.setattr(adapter, 'discover', discover)
    monkeypatch.setattr(adapter, 'request', lambda *a, **k: pytest.fail('reset or cancel'))
    def wait(refresh):
        while not refresh():
            clock[0] += 1
        return '__done__'
    result, notice = adapter.resume_snapshot('cfg', wait, fields=['catalog'])
    assert clock[0] > 30
    assert all(call[1]['fields'] == ['catalog'] for call in calls)
    assert all(call[1]['deadline'] == i + 1 for i, call in enumerate(calls[1:], 1))
    assert bool(notice) == (outcome in ('failed', 'unavailable'))
    if outcome == 'unavailable':
        assert result == dict(initial, unavailable=True, wait_unavailable=True)
    else:
        assert result == done


@pytest.mark.parametrize('cold', [True])
def test_resume_skip_retains_available_snapshot(adapter, monkeypatch, cold):
    data = pending(cold)
    monkeypatch.setattr(adapter, 'discover', lambda *a, **kw: data)
    result, notice = adapter.resume_snapshot(None, lambda refresh: 'skip')
    assert result == dict(data, wait_skipped=True)
    assert 'incomplete' in notice
    assert list(adapter.candidates(result, ['api'])) == list(adapter.candidates(data, ['api']))


@pytest.mark.parametrize('state', ['fresh', 'failed'])
def test_resume_settled_snapshot_never_waits(adapter, monkeypatch, state):
    data = view()
    if state == 'failed':
        data['hosts'][0].update(error='offline', retry_at=99999)
    elif state == 'unavailable':
        data = {'unavailable': True}
    monkeypatch.setattr(adapter, 'discover', lambda *a, **kw: data)
    result, notice = adapter.resume_snapshot(None, lambda *a: pytest.fail('unexpected wait'))
    assert result == data
    assert bool(notice) == (state != 'fresh')


def test_resume_cancel_does_not_mutate_shared_refresh(adapter, monkeypatch):
    data = pending(cold=True)
    before = copy.deepcopy(data)
    monkeypatch.setattr(adapter, 'discover', lambda *a, **kw: data)
    def cancel(refresh):
        assert not refresh()
        raise KeyboardInterrupt
    with pytest.raises(KeyboardInterrupt):
        adapter.resume_snapshot(None, cancel)
    assert data == before


@pytest.mark.parametrize('width,height', [(80, 24), (44, 20), (32, 12), (120, 12)])
def test_resume_wait_rail_layout(t_mod, monkeypatch, width, height):
    ui = make_ui(t_mod, ['enter'], width, height)
    monkeypatch.setattr(t_mod, '_RailUI', lambda: ui)
    monkeypatch.setattr(t_mod.select, 'select', lambda *a: ([True], [], []))
    assert t_mod._resume_discovery_wait(lambda: False) == 'skip'
    assert ui.restored
    frame = ui.frames[0]
    plain = '\n'.join(t_mod._ANSI_RE.sub('', line) for line in frame)
    assert 'Skip waiting' in plain and '▸' in plain and 'cancel' in plain
    assert sum(t_mod._term_rows(line, width) for line in frame) <= height
    assert all(t_mod._vis_len(line) <= width for line in frame)


@pytest.mark.parametrize('key,code', [(b'\r', 0), (b'\x1b', 130), (b'\x03', 130), (None, 0)])
def test_resume_wait_terminal_restores_on_every_exit(tmp_path, key, code):
    script = tmp_path / 'wait.py'
    script.write_text('''import runpy, sys
api = runpy.run_path(sys.argv[1], run_name='test')
count = [0]
def refresh():
    count[0] += 1
    return sys.argv[2] == 'complete' and count[0] > 2
try:
    print('RESULT', api['_resume_discovery_wait'](refresh))
except KeyboardInterrupt:
    sys.exit(130)
''')
    env = dict(os.environ, HOME=str(tmp_path), XDG_CONFIG_HOME=str(tmp_path / 'config'),
               XDG_STATE_HOME=str(tmp_path / 'state'), XDG_CACHE_HOME=str(tmp_path / 'cache'), TERM='xterm-256color')
    result, output = run_terminal([sys.executable, str(script), str(REPO_ROOT / 'bin/t'),
                                  'complete' if key is None else 'wait'], env, tmp_path, (80, 24),
                                 [] if key is None else [('Skip waiting', key)])
    assert result == code, output
    if code == 0:
        assert 'RESULT ' + ('__done__' if key is None else 'skip') in output


@pytest.mark.parametrize('interactive', [False, True])
def test_candidate_protocol_wait_is_explicit_and_stdout_stays_rows(t_mod, monkeypatch, tmp_path, capsys, interactive):
    monkeypatch.delenv('T_DISCOVERY_COLLECTOR')
    monkeypatch.setenv('T_RESUME_WAIT', '1' if interactive else '0')
    notice_file = tmp_path / 'notice'
    monkeypatch.setenv('T_RESUME_NOTICE_FILE', str(notice_file))
    monkeypatch.setattr(sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(sys.stderr, 'isatty', lambda: True)
    calls = []
    def snapshot(cfg, wait, *, choose):
        calls.append('wait')
        print('WAIT UI')
        return view(), 'Older results'
    def discover(cfg):
        calls.append('discover')
        return view()
    monkeypatch.setattr(t_mod, '_session_discovery', lambda: dict(
        resume_snapshot=snapshot, discover=discover, announce=lambda v: None,
        candidates=lambda v, aliases, slot: ['candidate-row']))
    monkeypatch.setattr(t_mod, 'Config', lambda: None)
    assert t_mod.main(['__resume-candidates', 'api']) == 0
    output = capsys.readouterr()
    assert output.out == 'candidate-row\n'
    assert ('WAIT UI' in output.err) == interactive
    assert calls == ['wait' if interactive else 'discover']
    if interactive:
        assert notice_file.read_text() == 'Older results'


@pytest.mark.parametrize('terminal,json_mode', [(False, False), (True, True), (True, False)])
def test_slot_history_wait_gates_and_snapshot(t_mod, monkeypatch, terminal, json_mode):
    monkeypatch.delenv('T_DISCOVERY_COLLECTOR')
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        monkeypatch.setattr(stream, 'isatty', lambda: terminal)
    calls = []
    class History:
        def __init__(self, cfg, api, store): self.store = store
        def execute(self, args):
            assert self.discovery_view == {'hosts': []}
            assert self.discovery_notice == ('incomplete' if terminal and not json_mode else '')
            return 0
    monkeypatch.setattr(t_mod.runpy, 'run_path', lambda *a, **kw: {'HistoryCommand': History})
    def snapshot(cfg, wait, **kwargs):
        calls.append('wait')
        assert kwargs == {'fields': ['catalog'], 'choose': t_mod._open_discovery_choice}
        return {'hosts': []}, 'incomplete'
    monkeypatch.setattr(t_mod, '_session_discovery', lambda: dict(
        resume_snapshot=snapshot, discover=lambda *a, **kw: {'hosts': []},
        announce=lambda v: None, SnapshotStore=lambda v: SimpleNamespace(close=lambda: None)))
    assert t_mod.cmd_history(SimpleNamespace(), SimpleNamespace(json=json_mode, refresh=False)) == 0
    assert calls == (['wait'] if terminal and not json_mode else [])


def test_history_notice_survives_sort_and_page(history, tmp_path):
    # Use the real history callback, which rebuilds the header on every action.
    records, settings = tmp_path / 'records', tmp_path / 'state'
    records.write_text('[]')
    settings.write_text(json.dumps(dict(page=1, query='', limit=50, width=32,
                                       sort='turn', sort_enabled=True, notice='Older or incomplete results')))
    for action in ('next-sort', 'toggle-sort', 'next'):
        history.picker([str(records), str(settings), action, ''])
        assert settings.with_suffix('.header').read_text().startswith('Older or incomplete results\n')


@pytest.fixture
def history(tmp_path, monkeypatch):
    from conftest import load_script
    monkeypatch.setenv('HOME', str(tmp_path))
    monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'state'))
    return load_script(REPO_ROOT / 'libexec/t_history.py', 'resume_wait_history')


@pytest.mark.parametrize('outcome', ['skip', 'empty', 'cancel'])
def test_shell_wait_exposes_controls_without_spinner_and_does_not_autoresume(zsh, outcome):
    setup = r'''
      python3() {
        [[ $2 == __cache-rows ]] && return 0
        command python3 "$@"
      }
      fzf() { return 0; }
      _t_resume_history_candidates() {
        [[ $T_RESUME_WAIT == 1 && -t 0 && -t 2 && ${_T_SPINNING:-0} != 1 ]] || return 97
        print -u2 'WAIT CONTROLS VISIBLE'
        [[ $OUTCOME == cancel ]] && return 130
        print -r -- 'Older or incomplete results' > "$T_RESUME_NOTICE_FILE"
        [[ $OUTCOME == empty ]] && return 0
        printf '10\tapi\t4\tsaved\t/tree\tdate\tSaved task\tlocal\tcodex\t-\torg/api\tNONE\n'
      }
      _t_fzf_filtered() { print -u2 "PICKER $T_PICKER_NOTICE"; return 130; }
      _t_resume_history_launch() { print -u2 'UNEXPECTED LAUNCH'; return 99; }
      _t_resume api --days all
      print -r -- "rc=$?"
    '''
    result = zsh(setup, _tty=True, T_DISCOVERY_COLLECTOR='0', OUTCOME=outcome)
    assert 'WAIT CONTROLS VISIBLE' in result.stdout, result.stdout
    assert 'UNEXPECTED LAUNCH' not in result.stdout
    assert 'rc=' + ('0' if outcome == 'empty' else '130') in result.stdout, result.stdout
    if outcome == 'skip':
        assert 'PICKER Older or incomplete results' in result.stdout
    elif outcome == 'empty':
        assert 'Discovery is incomplete' in result.stdout
    else:
        assert 'PICKER' not in result.stdout and 'No matching' not in result.stdout


def test_resume_discovery_notice_survives_auth_and_filter_changes(filters, tmp_path):
    row = 'api\t4\tsaved\t/tree\tdate\tSaved task\t-\thistory\tpeer\tcodex\t-'
    for name in ('source', 'all', 'rows-0', 'rows-1'):
        (tmp_path / name).write_text(row + '\n')
    (tmp_path / 'auth-hosts').write_text('peer\n')
    (tmp_path / 'discovery-notice').write_text('Older or incomplete results')
    for action in ('current', 'agent', 'off', 'on', 'sort-repo', 'default'):
        filters.render(tmp_path, 'resume', action, 32)
        header = (tmp_path / 'current-header').read_text()
        assert header.startswith('GitHub auth failed:')
        assert 'Older or incomplete results' in header


@pytest.mark.parametrize('outcome', ['cancel', 'unavailable', 'timeout', 'complete'])
def test_explicit_resume_refresh_preserves_snapshot_on_failure(t_mod, monkeypatch, tmp_path, outcome):
    (tmp_path / 'discovery-status').write_text(json.dumps(dict(hosts=[dict(host='local', status='current')])))
    for name in ('source', 'rows-0', 'columns'):
        (tmp_path / name).write_text(f'old {name}')
    running = view()
    running['hosts'][0]['refreshing'] = True
    finished = view()
    replies = {'cancel': [running], 'unavailable': [{'unavailable': True}],
               'timeout': [running], 'complete': [running, finished]}
    sequence, calls, clock = iter(replies[outcome]), [], [100]

    def discover(cfg, **kwargs):
        calls.append(kwargs['refresh'])
        return next(sequence)

    def wait(refresh):
        assert not refresh()
        if outcome == 'cancel':
            raise KeyboardInterrupt
        if outcome in ('unavailable', 'timeout'):
            clock[0] += 31
        assert refresh()
        return '__done__'

    def rebuild(command, **kwargs):
        # The spawner receives original flags and operates without input or a
        # controlling terminal. It writes solely to the private staging directory.
        import shlex
        from pathlib import Path
        options = shlex.split(command[-1])
        destination = Path(next(part.partition('=')[2] for part in options if part.startswith('_T_RESUME_PICKER_SNAPSHOT=')))
        assert options[-5:] == ['_t_resume', 'api', '4', '--days', 'all']
        assert kwargs['stdin'] == t_mod.subprocess.DEVNULL and kwargs['start_new_session']
        assert (destination / 'history').read_text() == 'new history\n'
        for name in ('source', 'rows-0', 'columns'):
            (destination / name).write_text(f'new {name}')
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(t_mod, 'Config', lambda: None)
    monkeypatch.setattr(t_mod, '_resume_discovery_wait', wait)
    monkeypatch.setattr(t_mod.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(t_mod.subprocess, 'run', rebuild)
    monkeypatch.setattr(t_mod, '_session_discovery', lambda: dict(
        discover=discover, resume_health=lambda v: dict(hosts=[], timed_out=v.get('wait_timed_out', False)),
        rows=lambda _: 'local\tlive\n',
        candidates=lambda v, aliases, slot: ['new history'] if aliases == ['api'] and slot == '4' else []))
    assert t_mod._resume_picker_refresh(tmp_path, ['api'], '4', ['api', '4', '--days', 'all']) == 0
    status = json.loads((tmp_path / 'discovery-status').read_text())
    if outcome in ('cancel', 'unavailable'):
        assert (tmp_path / 'source').read_text() == 'old source'
        assert status['cancelled' if outcome == 'cancel' else 'failed']
    else:
        assert (tmp_path / 'source').read_text() == 'new source'
        assert status['timed_out'] == (outcome == 'timeout')
    assert calls == ([True, False] if outcome == 'complete' else [True])


def test_observer_repaints_a_settled_host_before_a_pending_peer(t_mod, monkeypatch, tmp_path):
    status_file = tmp_path / 'discovery-status'
    def status(local, mini):
        return dict(hosts=[dict(host='local', status=local), dict(host='mini', status=mini)])
    status_file.write_text(json.dumps(status('refreshing', 'refreshing')))
    clock, observed = [0], []
    def discover(_, *, observe, deadline):
        assert observe and deadline > clock[0]
        return status('current', 'refreshing' if not observed else 'unreachable')
    def stage(snapshot, *args):
        (snapshot / 'source').write_text('New local row')
    class Picker:
        def __init__(self, count): pass
        def request(self, actions):
            if actions.startswith('execute-silent('):
                t_mod._resume_picker_adopt(tmp_path)
                (tmp_path / 'observed/ready').touch()
                observed.append(json.loads(status_file.read_text()))
    monkeypatch.setitem(sys.modules, 't_picker_close', SimpleNamespace(Picker=Picker))
    monkeypatch.setattr(t_mod.signal, 'signal', lambda *a: None)
    monkeypatch.setattr(t_mod, 'Config', lambda: None)
    monkeypatch.setattr(t_mod, '_resume_picker_stage', stage)
    monkeypatch.setattr(t_mod, '_session_discovery', lambda: dict(discover=discover, resume_health=lambda view: view))
    monkeypatch.setattr(t_mod.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(t_mod.time, 'sleep', lambda dt: clock.__setitem__(0, clock[0] + dt))
    assert t_mod._resume_picker_observe(tmp_path, ['api'], None, []) == 0
    assert observed == [status('current', 'refreshing'), status('current', 'unreachable')]
    assert (tmp_path / 'source').read_text() == 'New local row'
    assert not (tmp_path / 'observed').exists()


def test_queued_observer_does_not_overwrite_a_superseding_manual_refresh(t_mod, tmp_path):
    status = tmp_path / 'discovery-status'
    status.write_text('new manual state')
    source = tmp_path / 'source'
    source.write_text('new manual rows')
    observed = tmp_path / 'observed'
    observed.mkdir()
    (observed / 'base').write_text('old state')
    (observed / 'discovery-status').write_text('settled old state')
    (observed / 'source').write_text('older observed rows')
    assert t_mod._resume_picker_adopt(tmp_path) == 0
    assert status.read_text() == 'new manual state'
    assert source.read_text() == 'new manual rows'
