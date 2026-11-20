"""Cold scoped discovery observes one bounded refresh across every entry point."""
import copy
import json
from types import SimpleNamespace

import pytest

from test_session_discovery import adapter  # noqa: F401
from test_session_wait import clock  # noqa: F401
from test_agent_seam import zsh  # noqa: F401


def live(sid='after', cwd='/work/api', label='api-3', state='detached', agent='codex'):
    return '\t'.join([sid, cwd, label, state, 'active', sid, agent]) + '\n'


def saved(sid='after', repo='api', slot='3', updated='9999999999', closed='2026-11-20T12:00:00Z'):
    return '\t'.join([updated, repo, slot, sid, '/work/' + repo, 'Nov 20', sid, 'local',
                      'codex', '2026-11-19T12:00:00Z', 'org/' + repo, 'NONE', closed])


def record(sid='after', repo='api', slot='3'):
    return dict(repo='org/' + repo, sid=sid, agent='codex', title=sid, updated=100,
                associations=[dict(workspace='/work/' + repo, host='local', slot=slot)],
                sources=[dict(path='/saved/' + sid, host='local')], prs=[], mentions=[])


def snapshot(*, rows='', candidates=(), records=(), pending=False, host='local'):
    return {'hosts': [dict(host=host, target=host, checked=10000, refreshing=pending, stale=False,
        error='', value=dict(rows=rows, resume=list(candidates), scopes={'api': ['/work/api', '/trees/api'], 'web': ['/work/web']},
        catalog=dict(repositories=[dict(alias='api', repo='org/api', path='/work/api'),
                                   dict(alias='web', repo='org/web', path='/work/web')], conversations=list(records))))]}


@pytest.fixture
def cfg():
    return SimpleNamespace(repos={'api': '/work/api', 'web': '/work/web'}, hosts={}, worktree_root='/trees',
                           repo_of_dir=lambda cwd: ('api', ''), worktree={}, worktree_default='1')


def feed(adapter, monkeypatch, before, after):
    calls = []
    def discover(cfg, **kwargs):
        calls.append(kwargs)
        return before if len(calls) == 1 else after
    monkeypatch.setattr(adapter, 'discover', discover)
    monkeypatch.setattr(adapter, 'request', lambda *a, **kw: pytest.fail('observation reset/cancelled shared discovery'))
    return calls


@pytest.fixture
def routes(adapter, monkeypatch, t_mod, cfg):
    monkeypatch.delenv('T_DISCOVERY_COLLECTOR')
    monkeypatch.delenv('T_DISCOVERY_EXPLICIT_WAIT', raising=False)
    monkeypatch.delenv('T_RESUME_LAST', raising=False)
    monkeypatch.delenv('T_RESUME_CUTOFF', raising=False)
    monkeypatch.setattr(t_mod, 'Config', lambda: cfg)
    monkeypatch.setattr(t_mod, '_session_discovery', lambda: vars(adapter))
    monkeypatch.setattr(adapter, 'require_open_network', lambda *a, **kw: None)
    return t_mod


@pytest.mark.parametrize('state', ['settled-empty', 'failed', 'unavailable', 'usable', 'timeout', 'cancel'])
def test_automatic_wait_only_for_empty_pending_scope(adapter, monkeypatch, clock, state):
    before = snapshot(rows=live(cwd='/work/web'), pending=state in ('usable', 'timeout', 'cancel'))
    if state == 'unavailable':
        before = {'unavailable': True}
    if state == 'failed':
        before['hosts'][0]['error'] = 'offline'
    calls = feed(adapter, monkeypatch, before, before)
    if state == 'cancel':
        monkeypatch.setattr(adapter.time, 'sleep', lambda _: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            adapter.bounded_snapshot(None, explicit=False, usable=lambda v: False)
    else:
        result, notice = adapter.bounded_snapshot(None, explicit=False,
            usable=lambda v: state == 'usable', fields=['rows'])
        assert len(calls) == (120 if state == 'timeout' else 1)
        assert clock[0] == (30 if state == 'timeout' else 0)
        assert bool(result.get('wait_timed_out')) == (state == 'timeout')
        assert bool(notice) == (state != 'settled-empty')


def test_initial_snapshot_and_explicit_wait_preserve_nonempty_rows(adapter, monkeypatch, clock):
    before, after = snapshot(rows=live('before'), pending=True), snapshot(rows=live())
    calls = feed(adapter, monkeypatch, after, after)
    result, _ = adapter.bounded_snapshot(None, initial=before, explicit=True, usable=lambda _: True, fields=['rows'])
    assert result is after and len(calls) == 1 and clock[0] == .25


@pytest.mark.parametrize('remote', [False, True])
def test_public_list_waits_for_requested_repo_only(routes, adapter, monkeypatch, clock, cfg, remote):
    before = snapshot(rows=live('unrelated', '/work/web', 'web-2'), pending=True)
    after = snapshot(rows=live())
    calls = feed(adapter, monkeypatch, before, after)
    shown = []
    monkeypatch.setattr(routes, '_render_remote', lambda rows, *a: shown.extend(rows))
    monkeypatch.setattr(routes, '_render_local', lambda rows, *a: shown.extend(rows))
    args = routes.build_parser().parse_args(['session', 'list', 'api'] + ([] if remote else ['--local']))
    assert routes.cmd_ls(cfg, args) == 0
    assert [row['sid'] for row in shown] == ['after']
    assert len(calls) == 2 and all(call['local'] == (not remote) for call in calls)
    assert clock[0] == .25


@pytest.mark.parametrize('options,before,after', [
    (['--open', '--repo', 'api'], live('unrelated', '/work/web', 'web-2'), live()),
    (['--remote-only', '--repo', 'api'], live(), live()),
    (['--repo', 'api', '--move-only', '--slot', '3', '--session', 'after'], live('wrong', label='api-9'), live()),
    (['--move-only'], live('wrong', state='app'), live()),
    (['--move-only'], live('wrong', agent='cursor'), live()),
    (['--cli-only'], live('wrong', state='app'), live()),
])
def test_private_open_close_move_waits_for_same_eligible_projection(routes, adapter, monkeypatch, clock, capsys, options, before, after):
    initial = snapshot(rows=before, pending=True)
    final = snapshot(rows=after, host='mini' if '--remote-only' in options else 'local')
    calls = feed(adapter, monkeypatch, initial, final)
    assert routes.main(['__cache-rows', *options]) == 0
    output = capsys.readouterr()
    assert '\tafter\t' in output.out and 'wrong' not in output.out and 'unrelated' not in output.out
    assert len(calls) == 2 and clock[0] == .25


def test_foreign_alias_uses_local_identity_before_wait_projection(adapter):
    data = snapshot()
    data['hosts'][0]['value']['catalog']['repositories'] = [dict(alias='ff', repo='org/formflow', path='/work/formflow')]
    remote = snapshot(rows=live(cwd='/different/checkout', label='formflow-3'), host='mini')['hosts'][0]
    remote['value']['scopes'] = {'org/formflow': ['/different/checkout']}
    data['hosts'].append(remote)
    assert 'after' in adapter.rows(data, repo='ff')


@pytest.mark.parametrize('mode', ['unrelated-repo', 'wrong-slot', 'ancient', 'last-unknown', 'last-incomplete'])
def test_saved_protocol_waits_for_scope_age_and_safe_last(routes, adapter, monkeypatch, clock, tmp_path, capsys, mode):
    row = saved(repo='web') if mode == 'unrelated-repo' else saved(slot='9') if mode == 'wrong-slot' else saved(updated='1') if mode == 'ancient' else saved(closed='-') if mode == 'last-unknown' else saved()
    before = snapshot(rows=live('before-owner'), candidates=[row], pending=True)
    after = snapshot(rows=live('after-owner'), candidates=[saved()])
    if mode.startswith('last'):
        monkeypatch.setenv('T_RESUME_LAST', '1')
    if mode == 'ancient':
        monkeypatch.setenv('T_RESUME_CUTOFF', '100')
    owner_file = tmp_path / 'owners'
    monkeypatch.setenv('T_RESUME_ROWS_FILE', str(owner_file))
    calls = feed(adapter, monkeypatch, before, after)
    assert routes.main(['__resume-candidates', 'api', '--slot', '3']) == 0
    assert capsys.readouterr().out.split('\t')[3] == 'after'
    assert 'after-owner' in owner_file.read_text() and 'before-owner' not in owner_file.read_text()
    assert len(calls) == 2 and all(call['fields'] == ['rows', 'resume'] for call in calls)


@pytest.mark.parametrize('json_mode', [False, True])
def test_public_slot_history_redirected_waits_with_real_scope_and_identity(routes, adapter, monkeypatch, clock, cfg, capsys, json_mode):
    before = snapshot(records=[record('wrong-repo', 'web'), record('wrong-slot', slot='9')], pending=True)
    after = snapshot(records=[record()])
    calls = feed(adapter, monkeypatch, before, after)
    monkeypatch.setattr(routes.sys.stdin, 'isatty', lambda: False)
    monkeypatch.setattr(routes.sys.stdout, 'isatty', lambda: False)
    args = routes.build_parser().parse_args(['session', 'history', 'api', '3'] + (['--json'] if json_mode else []))
    assert routes.cmd_history(cfg, args) == 0
    output = capsys.readouterr()
    if json_mode:
        assert [row['sid'] for row in json.loads(output.out)['conversations']] == ['after']
    else:
        assert 'after' in output.out and 'wrong-slot' not in output.out and 'wrong-repo' not in output.out
    assert len(calls) == 2 and all(call['fields'] == ['catalog'] for call in calls)


def test_history_invalid_scope_reports_error_without_traceback(routes, adapter, monkeypatch, clock, cfg, capsys):
    feed(adapter, monkeypatch, snapshot(pending=True), snapshot())
    args = routes.build_parser().parse_args(['history', 'unknown', '3', '--json'])
    assert routes.cmd_history(cfg, args) == 1
    output = capsys.readouterr()
    assert 'specify a registered repository' in output.err and 'Traceback' not in output.err


@pytest.mark.parametrize('snippet,expected', [
    ('_t_open_picker_impl api remote', ['--open', '--repo api']),
    ('_dev_kill_pick api', ['--repo api', '--local']),
    ('_dev_kill_pick api yes remote', ['--remote-only', '--repo api']),
    ('_t_remote_move_rows mini api browse 3 saved', ['--host mini', '--repo api', '--move-only', '--slot 3', '--session saved']),
    ('_t_move_pick api 3', ['--repo api', '--slot 3', '--move-only']),
])
def test_shell_browsing_forwards_private_scope_and_preserves_cancel(zsh, snippet, expected):
    result = zsh('_t_discovery_rows() { print -u2 -- "SCOPE $*"; return 130; }; ' + snippet, T_DISCOVERY_COLLECTOR='0')
    assert result.returncode == 130, result.stdout + result.stderr
    for item in expected:
        assert item in result.stderr


@pytest.mark.parametrize('with_scopes', [False, True])
def test_local_alias_collision_never_exposes_unrelated_remote_repository(adapter, with_scopes):
    data = snapshot()
    data['hosts'][0]['value']['catalog']['repositories'] = [dict(alias='api', repo='org/correct', path='/work/api')]
    remote = snapshot(rows=live('correct', '/correct') + live('wrong', '/other'), host='mini')['hosts'][0]
    remote['value']['catalog']['repositories'] = [dict(alias='server-name', repo='org/correct', path='/correct'),
                                                   dict(alias='api', repo='org/other', path='/other')]
    remote['value']['scopes'] = {'org/correct': ['/correct'], 'api': ['/other']} if with_scopes else {}
    data['hosts'].append(remote)
    result = adapter.rows(data, repo='api', remote_only=True)
    assert 'correct' in result and 'wrong' not in result and '/other' not in result


def test_toggleable_live_visibility_does_not_trigger_wait(adapter, monkeypatch, clock):
    data = snapshot(rows=live('saved-live'), candidates=[saved('saved-live', updated='1')], pending=True)
    calls = feed(adapter, monkeypatch, data, snapshot())
    result, _ = adapter.bounded_snapshot(None, explicit=False,
        usable=lambda value: adapter.resume_usable(value, ['api'], cutoff=100))
    assert result is data and len(calls) == 1 and clock[0] == 0


def test_secondary_local_alias_resolves_identity_by_checkout_path(adapter):
    data = snapshot()
    data['hosts'][0]['value']['catalog']['repositories'] = [dict(alias='dot', repo='org/dotfiles', path='/work/dotfiles')]
    remote = snapshot(rows=live('correct', '/correct') + live('wrong', '/wrong'), host='mini')['hosts'][0]
    remote['value']['scopes'] = {'dotfiles': ['/wrong'], 'org/dotfiles': ['/correct']}
    data['hosts'].append(remote)
    result = adapter.rows(data, repo='dotfiles', local_roots=['/work/dotfiles'], remote_only=True)
    assert 'correct' in result and 'wrong' not in result


def test_cold_local_identity_cannot_expose_colliding_remote_alias(routes, adapter, monkeypatch, clock, capsys):
    before = snapshot(pending=True)
    before['hosts'][0]['value'] = None
    remote = snapshot(rows=live('wrong', '/wrong'), host='mini')['hosts'][0]
    remote['value']['scopes'] = {'api': ['/wrong'], 'org/api': ['/correct']}
    before['hosts'].append(remote)
    after = snapshot()
    remote = copy.deepcopy(remote)
    remote['value']['rows'] += live('correct', '/correct')
    after['hosts'].append(remote)
    calls = feed(adapter, monkeypatch, before, after)
    assert routes.main(['__cache-rows', '--repo', 'api', '--remote-only']) == 0
    output = capsys.readouterr()
    assert 'correct' in output.out and 'wrong' not in output.out and len(calls) == 2


def test_host_only_selector_uses_selected_hosts_registry(adapter):
    data = snapshot(rows=live(cwd='/remote/checkout'), host='mini')
    data['hosts'][0]['value']['scopes'] = {'api': ['/remote/checkout']}
    assert 'after' in adapter.rows(data, repo='api', local_roots=['/different/local/api'])
