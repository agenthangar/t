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
        done['hosts'][0].update(error='offline', value=None, refreshing=False)
    elif outcome == 'unavailable':
        done = {'unavailable': True}
    calls, now = [], [0]
    monkeypatch.setattr(adapter.time, 'monotonic', lambda: now[0])
    monkeypatch.setattr(adapter.time, 'sleep', lambda seconds: now.__setitem__(0, now[0] + seconds))
    def discover(cfg, **kwargs):
        calls.append(kwargs)
        return initial if len(calls) == 1 else done
    monkeypatch.setattr(adapter, 'discover', discover)
    result, notice = adapter.resume_snapshot(None, lambda *a: pytest.fail('unbounded rail entered'), fields=['catalog'])
    assert now[0] == .25
    assert all(call['fields'] == ['catalog'] for call in calls)
    assert result.get('wait_unavailable') if outcome == 'unavailable' else result is done
    assert bool(notice) == (outcome in ('failed', 'unavailable'))


@pytest.mark.parametrize('cold', [True])
def test_resume_skip_retains_available_snapshot(adapter, monkeypatch, cold):
    data = pending(cold)
    monkeypatch.setattr(adapter, 'discover', lambda *a, **kw: data)
    monkeypatch.setattr(adapter, 'wait_for_refresh', lambda *a, **kw: dict(data, wait_timed_out=True))
    result, notice = adapter.resume_snapshot(None, lambda *a: pytest.fail('timeout entered skip rail'))
    assert result == dict(data, wait_timed_out=True) and 'incomplete' in notice


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
    monkeypatch.setattr(adapter.time, 'sleep', lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    monkeypatch.setattr(adapter, 'request', lambda *a, **kw: pytest.fail('cancel mutated refresh'))
    with pytest.raises(KeyboardInterrupt):
        adapter.resume_snapshot(None, lambda *a: pytest.fail('cancel entered rail'))
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
    assert 'Refreshing sessions' in plain and 'Skip waiting' in plain and '▸' in plain and 'cancel' in plain
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
def test_candidate_protocol_wait_is_explicit_and_stdout_stays_rows(t_mod, adapter, monkeypatch, tmp_path, capsys, interactive):
    monkeypatch.delenv('T_DISCOVERY_COLLECTOR')
    monkeypatch.setenv('T_RESUME_WAIT', '1' if interactive else '0')
    monkeypatch.setenv('T_RESUME_NOTICE_FILE', str(tmp_path / 'notice'))
    calls = []
    def bounded(cfg, **kwargs):
        calls.append(kwargs)
        assert kwargs['usable'](view())
        return view(), ''
    monkeypatch.setattr(t_mod, '_session_discovery', lambda: dict(bounded_snapshot=bounded,
        announce=lambda v: None, candidates=adapter.candidates, _partial=adapter._partial, resume_usable=adapter.resume_usable))
    monkeypatch.setattr(t_mod, 'Config', lambda: None)
    assert t_mod.main(['__resume-candidates', 'api']) == 0
    output = capsys.readouterr()
    assert output.out.startswith('10000\tapi') and not output.err
    assert len(calls) == 1 and not calls[0]['explicit']


@pytest.mark.parametrize('terminal,json_mode', [(False, False), (True, True), (True, False)])
def test_slot_history_wait_gates_and_snapshot(t_mod, adapter, monkeypatch, terminal, json_mode):
    monkeypatch.delenv('T_DISCOVERY_COLLECTOR')
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        monkeypatch.setattr(stream, 'isatty', lambda: terminal)
    calls = []
    class History:
        def __init__(self, cfg, api, store): self.store = store
        def execute(self, args):
            assert self.discovery_view == {'hosts': []}
            assert self.discovery_notice == 'incomplete'
            return 0
    monkeypatch.setattr(t_mod.runpy, 'run_path', lambda *a, **kw: {'HistoryCommand': History})
    def snapshot(cfg, **kwargs):
        calls.append(kwargs)
        return {'hosts': []}, 'incomplete'
    monkeypatch.setattr(t_mod, '_session_discovery', lambda: dict(bounded_snapshot=snapshot,
        announce=lambda v: None, SnapshotStore=lambda v: SimpleNamespace(close=lambda: None)))
    assert t_mod.cmd_history(SimpleNamespace(), SimpleNamespace(json=json_mode, refresh=False)) == 0
    assert len(calls) == 1 and calls[0]['fields'] == ['catalog'] and not calls[0]['explicit']


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
        assert any(line.startswith('GitHub auth failed:') for line in header.splitlines())
        assert 'Older or incomplete results' in header


@pytest.mark.parametrize('outcome', ['unavailable', 'timeout', 'complete', 'slow'])
def test_manual_picker_refresh_uses_inline_updates(t_mod, monkeypatch, tmp_path, outcome):
    (tmp_path / 'discovery-status').write_text(json.dumps(dict(hosts=[dict(host='local', status='current')])))
    (tmp_path / 'refresh-enabled').touch()
    (tmp_path / 'refresh-request').touch()
    source = tmp_path / 'source'
    source.write_text('old source')
    updates, calls, clock = [], [], [100]

    def discover(cfg, **kwargs):
        calls.append(kwargs)
        if outcome == 'unavailable':
            return {'unavailable': True}
        return dict(hosts=[dict(host='local', status='refreshing' if len(calls) == 1 else 'current')])

    def repaint(directory, picker, status, rebuild=None):
        updates.append(status)
        (directory / 'discovery-status').write_text(json.dumps(status))
        if rebuild:
            rebuild(directory)
        if len(updates) == 2:
            (directory / 'refresh-enabled').unlink()

    def stage(directory, cache, view, aliases, slot, shell_args):
        assert (aliases, slot, shell_args) == (['api'], '4', ['api', '4', '--days', 'all'])
        (directory / 'source').write_text('new source')

    monkeypatch.setitem(sys.modules, 't_picker_close', SimpleNamespace(Picker=lambda _: None))
    monkeypatch.setattr(t_mod.signal, 'signal', lambda *a: None)
    monkeypatch.setattr(t_mod, 'Config', lambda: None)
    monkeypatch.setattr(t_mod, '_resume_discovery_wait', lambda *_: pytest.fail('picker entered a refresh dialog'))
    monkeypatch.setattr(t_mod, '_resume_picker_update', repaint)
    monkeypatch.setattr(t_mod, '_resume_picker_stage', stage)
    monkeypatch.setattr(t_mod, '_session_discovery', lambda: dict(discover=discover, resume_health=lambda v: v))
    monkeypatch.setattr(t_mod.time, 'monotonic', lambda: clock[0])
    delay = {'unavailable': 31, 'timeout': 126, 'slow': 45}
    monkeypatch.setattr(t_mod.time, 'sleep', lambda dt: clock.__setitem__(0, clock[0] + delay.get(outcome, dt)))

    assert t_mod._resume_picker_observe(tmp_path, ['api'], '4', ['api', '4', '--days', 'all']) == 0
    assert updates[0]['hosts'][0]['status'] == 'refreshing'
    complete = outcome in ('complete', 'slow')
    assert updates[1]['hosts'][0]['status'] == ('current' if complete else 'timed out')
    assert source.read_text() == ('new source' if complete else 'old source')
    assert calls[0]['refresh']
    if complete:
        assert calls[1]['observe']


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
