"""Exact last-close selection and one bounded discovery snapshot."""
import copy
import shlex
from types import SimpleNamespace

import pytest

from conftest import load_script, REPO_ROOT
from test_session_discovery import adapter, view  # noqa: F401
from test_agent_seam import zsh  # noqa: F401
from test_history_data import history, store, app, conversation  # noqa: F401


def candidate(sid='old-idle', repo='api', slot='3', closed='2026-11-19T12:00:00Z'):
    # An ancient idle conversation closed today must beat recent activity.
    return '\t'.join(['1', repo, slot, sid, '/former/work', 'Jan 01', 'Saved work',
                      'local', 'codex', '2020-01-01T00:00:00Z', 'org/' + repo, 'NONE', closed])


def snapshot(*rows):
    result = view()
    result['hosts'][0]['value']['resume'] = list(rows)
    return result


def test_last_closed_scope_and_duplicate_evidence(adapter):
    old = candidate()
    recent = candidate('recent-turn', closed='2026-11-18T12:00:00Z').split('\t')
    recent[0] = '9999999999'
    recent[9] = '2026-11-18T11:00:00Z'
    other = candidate('other-repo', 'web', closed='2026-11-20T12:00:00Z')
    data = snapshot(old, '\t'.join(recent), other)
    data['hosts'].append(copy.deepcopy(data['hosts'][0]))
    assert adapter.last_closed_candidate(data, ['api']) == old
    assert adapter.last_closed_candidate(data, ['api'], '3') == old
    assert adapter.last_closed_candidate(data, ['--all']) == other
    assert adapter.last_closed_candidate(data, ['org/api']) == old


@pytest.mark.parametrize('state', ['missing', 'tie', 'unavailable', 'loading', 'refreshing', 'stale', 'error', 'wait_timed_out', 'conflict', 'invalidated'])
def test_last_closed_refuses_incomplete_missing_tied_or_conflicting(adapter, state):
    data = snapshot(candidate())
    if state == 'missing':
        data = snapshot(candidate(closed='-'))
    elif state == 'tie':
        data['hosts'][0]['value']['resume'].append(candidate('another'))
    elif state in ('conflict', 'invalidated'):
        host = copy.deepcopy(data['hosts'][0])
        host['host'] = 'mini'
        host['value']['resume'] = [candidate(closed='-' if state == 'invalidated' else '2026-11-20T12:00:00Z')]
        data['hosts'].append(host)
    elif state in ('refreshing', 'stale', 'error'):
        data['hosts'][0][state] = True
    else:
        data[state] = True
    with pytest.raises(ValueError):
        adapter.last_closed_candidate(data, ['api'])
    with pytest.raises(ValueError):
        adapter.last_closed_candidate(snapshot(candidate()), ['api'], '99')


def test_bounded_wait_uses_settled_snapshot(adapter, monkeypatch):
    before, after = snapshot(candidate('before')), snapshot(candidate('after'))
    before['hosts'][0]['refreshing'] = True
    after['hosts'][0]['value']['rows'] = 'after-owner\t/after\tapi-3\tdetached\tactive\tAfter\tcodex\n'
    calls = []
    monkeypatch.setattr(adapter, 'discover', lambda *a, **kw: calls.append(kw) or (before if len(calls) == 1 else after))
    monkeypatch.setattr(adapter.time, 'sleep', lambda _: None)
    result, notice = adapter.bounded_snapshot(SimpleNamespace())
    assert result is after and not notice and len(calls) == 2
    assert all(call['fields'] == ['rows', 'resume'] for call in calls)
    assert 'after-owner' in adapter.rows(result)
    assert 'after' == adapter.last_closed_candidate(result, ['api']).split('\t')[3]
    monkeypatch.setattr(adapter, 'discover', lambda *a, **kw: after)
    monkeypatch.setattr(adapter.time, 'sleep', lambda _: pytest.fail('settled discovery waited'))
    assert adapter.bounded_snapshot(SimpleNamespace())[0] is after


@pytest.mark.parametrize('outcome', ['timeout', 'failure', 'cancel'])
def test_bounded_wait_retains_partial_and_cancels(adapter, monkeypatch, outcome):
    data = snapshot(candidate())
    data['hosts'][0]['refreshing'] = True
    monkeypatch.setattr(adapter.time, 'sleep', lambda _: None)
    ticks = iter([0, 0, 31])
    monkeypatch.setattr(adapter.time, 'monotonic', lambda: next(ticks, 31))
    if outcome == 'cancel':
        monkeypatch.setattr(adapter.time, 'sleep', lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            adapter.wait_for_refresh(SimpleNamespace(), data)
    else:
        if outcome == 'failure':
            monkeypatch.setattr(adapter.time, 'monotonic', lambda: 0)
            monkeypatch.setattr(adapter, 'discover', lambda *a, **kw: {'unavailable': True})
        result = adapter.wait_for_refresh(SimpleNamespace(), data)
        assert result['hosts'] == data['hosts']
        assert result['wait_timed_out' if outcome == 'timeout' else 'wait_unavailable']


@pytest.mark.parametrize('target', ['api', 'api 3', '--all'])
def test_last_shell_bypasses_picker_age_and_pending_slot(zsh, target):
    row = candidate()
    script = ('_t_resume_history_candidates() { print -r -- ' + shlex.quote(row) + '; '
              'print -r -- "local\tother-owner\t/other\tapi-3\tdetached\tactive\tOther\tcodex" > "$T_RESUME_ROWS_FILE"; }; '
              '_t_fzf_filtered() { print -u2 UNEXPECTED_PICKER; return 9; }; '
              '_t_resume_history_launch() { print -u2 -- "EXACT $2 $3 $T_RESUME_LAST_CLOSED"; print dev-api-99; }; '
              '_t_resume ' + target + ' --last --wait')
    result = zsh(script, T_DISCOVERY_COLLECTOR='0')
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'EXACT old-idle codex 2026-11-19T12:00:00Z' in result.stderr
    assert 'UNEXPECTED_PICKER' not in result.stderr


@pytest.mark.parametrize('flags', ['--live', '--days all', '--days=2'])
def test_last_rejects_incompatible_filters_before_discovery(zsh, flags):
    result = zsh('_t_resume_history_candidates() { print UNEXPECTED_DISCOVERY; }; _t_resume api --last ' + flags)
    assert result.returncode == 2
    assert 'cannot be combined' in result.stderr and 'UNEXPECTED_DISCOVERY' not in result.stdout


@pytest.mark.parametrize('wait_flag', ['--wait', '-w'])
def test_public_wait_routes_and_plain_slot_alias(wait_flag):
    cli = load_script(REPO_ROOT / 'bin/t', 'resume_last_cli')
    parser = cli.build_parser()
    for command in ('session open', 'session close', 'session resume', 'session move', 'session list', 'session history',
                    'remote open', 'remote resume', 'remote move', 'remote kill'):
        assert parser.parse_args(command.split() + [wait_flag]).wait
    assert cli._plain_slot_resume(['resume', 'api', '3', wait_flag])
    assert not cli._plain_slot_resume(['resume', 'api', '3', '--last', wait_flag])


@pytest.mark.parametrize('change', ['resume', 'close', 'missing'])
def test_last_launch_refresh_refuses_invalidated_identity(app, history, store, monkeypatch, change):
    row = conversation(associations=[dict(workspace='/old/work', slot='3', host='local', evidence='close', observed=100)])
    store.merge(row)
    monkeypatch.setenv('T_RESUME_LAST_CLOSED', history._SORT['closed_time'](row))
    calls = []
    def refresh(alias):
        calls.append(alias)
        if change != 'missing':
            store.merge(conversation(associations=[dict(workspace='/old/work', slot='3', host='local', evidence=change, observed=200)]))
    monkeypatch.setattr(app, 'discover', lambda *a, **kw: None)
    monkeypatch.setattr(app, 'refresh', refresh)
    if change == 'missing':
        monkeypatch.setattr(app, 'records', lambda *a: [])
    monkeypatch.setattr(app, 'owners', lambda *a: pytest.fail('invalidated close reached owner/launch'))
    with pytest.raises(history.HistoryError, match='last close changed'):
        app.resume('repo', row, detached=True)
    assert calls == ['repo']


@pytest.mark.parametrize('wait_flag', ['--wait', '-w'])
def test_remote_last_wait_forwards_exact_flags_without_terminal(zsh, wait_flag):
    result = zsh("REMOTE_HOSTS=(mini user@mini); "
                 "ssh() { print -r -- \"SSH $*\"; return 17; }; "
                 "t remote resume mini api --last " + wait_flag)
    assert result.returncode == 17, result.stdout + result.stderr
    assert 'zsh -lic' in result.stdout and '--last' in result.stdout and '--wait' in result.stdout


def test_close_wait_cancel_keeps_130(zsh):
    result = zsh('_t_discovery_rows() { return 130; }; _dev_kill_pick api')
    assert result.returncode == 130


def test_unregistered_display_alias_refreshes_exact_repository(app, history, store, monkeypatch):
    row = conversation(associations=[dict(workspace='/old/work', slot='3', host='mini', evidence='close', observed=100)])
    store.merge(row)
    monkeypatch.setenv('T_RESUME_LAST_CLOSED', history._SORT['closed_time'](row))
    calls = []
    monkeypatch.setattr(app, 'discover_catalog', lambda: calls.append('catalog'))
    monkeypatch.setattr(app, 'owners', lambda *a: [{'sid': row['sid'], 'agent': row['agent']}])
    # Reaching the existing owner guard proves the unregistered display alias
    # did not hide the exact identity or cause materialization before validation.
    with pytest.raises(history.HistoryError, match='became live'):
        app.resume('producer-only-alias', row, detached=True)
    assert calls == ['catalog']


@pytest.mark.parametrize('catalog', [False, True])
def test_last_remote_metadata_failure_never_reuses_retained_close(app, history, store, monkeypatch, catalog):
    row = conversation(associations=[dict(workspace='/old/work', slot='3', host='mini', evidence='close', observed=100)])
    store.merge(row)
    app.cfg.hosts = {'mini': 'user@mini'}
    monkeypatch.setenv('T_RESUME_LAST_CLOSED', history._SORT['closed_time'](row))
    monkeypatch.setattr(app, 'discover', lambda *a, **kw: None)
    monkeypatch.setattr(app, 'local_catalog', lambda: {'repositories': []})
    monkeypatch.setattr(history, 'run', lambda *a, **kw: (_ for _ in ()).throw(history.HistoryError('metadata failed')))
    monkeypatch.setattr(app, 'owners', lambda *a: pytest.fail('metadata failure reached launch/ownership'))
    with pytest.raises(history.HistoryError, match='metadata unavailable'):
        app.resume('producer-only-alias' if catalog else 'repo', row, detached=True)


def test_private_wait_uses_same_post_snapshot_for_candidates_and_owners(adapter, monkeypatch, t_mod, tmp_path, capsys):
    data = snapshot(candidate('after'))
    data['hosts'][0]['value']['rows'] = 'after-owner\t/after\tapi-3\tdetached\tactive\tAfter\tcodex\n'
    calls = []
    def bounded(cfg, **kwargs):
        calls.append('bounded')
        return data, ''
    cache = {name: getattr(adapter, name) for name in ('announce', 'rows', 'candidates', '_partial', 'last_closed_candidate', 'resume_usable')}
    cache['bounded_snapshot'] = bounded
    cache['resume_snapshot'] = lambda *a, **kw: pytest.fail('bounded wait entered interactive rail')
    monkeypatch.setattr(t_mod, '_session_discovery', lambda: cache)
    monkeypatch.setattr(t_mod, 'Config', lambda: SimpleNamespace())
    monkeypatch.delenv('T_DISCOVERY_COLLECTOR', raising=False)
    monkeypatch.setenv('T_DISCOVERY_EXPLICIT_WAIT', '1')
    monkeypatch.setenv('T_RESUME_LAST', '1')
    output = tmp_path / 'owners'
    monkeypatch.setenv('T_RESUME_ROWS_FILE', str(output))
    assert t_mod._main(['__resume-candidates', 'api']) == 0
    assert calls == ['bounded']
    assert 'after-owner' in output.read_text()
    assert capsys.readouterr().out.split('\t')[3] == 'after'


@pytest.mark.parametrize('wait_flag', ['--wait', '-w'])
def test_remote_close_wait_crosses_host_worker_boundary(zsh, tmp_path, wait_flag):
    import sys
    stub = tmp_path / 'stubbin/t'
    stub.write_text(f'#!{sys.executable}\nimport sys\nprint("REMOTE " + " ".join(sys.argv[1:]))\n')
    stub.chmod(0o755)
    result = zsh('REMOTE_HOSTS=(mini user@mini); t remote kill mini api ' + wait_flag)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'remote --host mini session close api --wait' in result.stdout


@pytest.mark.parametrize('command,helper', [('t session open api', '_t_open'), ('t open api', '_t_open'),
                                          ('t session resume api --last', '_t_resume'),
                                          ('t session move api', '_t_session_move'), ('t move api', '_t_session_move')])
@pytest.mark.parametrize('wait_flag', ['--wait', '-w'])
def test_public_wait_reaches_shell_discovery_scope(zsh, command, helper, wait_flag):
    result = zsh(helper + '() { print -r -- "WAIT=$T_DISCOVERY_EXPLICIT_WAIT ARGS=$*"; }; ' + command + ' ' + wait_flag)
    assert result.returncode == 0 and 'WAIT=1' in result.stdout
    assert wait_flag not in result.stdout
