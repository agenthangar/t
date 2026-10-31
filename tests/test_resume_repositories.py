"""Repository discovery is read-only; selected identities alone can be landed."""
import json
import os
from pathlib import Path
import shlex
import time
from types import SimpleNamespace

import pytest

from test_history_data import history, store, app, conversation, codex_rows, transcript_file
from test_agent_seam import zsh  # noqa: F401


def catalog(repo='org/remote', sid='saved'):
    return dict(version=1, repositories=[dict(repo=repo, alias='remote-alias', path='/other/home/code/remote')],
                conversations=[conversation(repo=repo, sid=sid,
                    associations=[dict(workspace='/other/home/trees/remote/remote-3', host='local', slot='3')],
                    sources=[dict(host='local', path='/other/home/.codex/sessions/rollout-saved.jsonl')])])


def proof(identity='R_same', canonical='new/repo'):
    return json.dumps(dict(id=identity, nameWithOwner=canonical))


def test_orphan_codex_metadata_catalog_without_registered_repositories(app, history, store, monkeypatch):
    app.cfg.repos = {}
    rows = codex_rows('/gone/trees/project/project-8', sid='orphan')
    rows[0]['payload']['git']['repository_url'] = 'git@github.com:org/project.git'
    path = transcript_file(Path(os.environ['CODEX_HOME']) / 'archived_sessions/rollout-orphan.jsonl', rows)
    monkeypatch.setattr(history, 'run', lambda *a, **kw: '')
    result = app.local_catalog()
    assert result['repositories'] == [dict(repo='org/project', alias='org/project', path='')]
    row, = result['conversations']
    assert row['sid'] == 'orphan' and row['archived']
    assert row['associations'][0]['slot'] == '8'
    assert row['sources'][0]['path'] == str(path)
    assert not Path('/gone/trees/project/project-8').exists()


@pytest.mark.parametrize('metadata', [{}, {'git': {'repository_url': 'https://elsewhere.invalid/org/repo'}},
                                      {'git': {'repository_url': 'https://github.com/org/repo'}, 'thread_source': 'subagent'},
                                      {'git': {'repository_url': 'https://github.com/org/repo'}, 'source': {'subagent': 'x'}}])
def test_orphan_discovery_requires_exact_supported_root_metadata(app, history, monkeypatch, metadata):
    app.cfg.repos = {}
    rows = codex_rows('/gone/tree', sid='orphan')
    rows[0]['payload'].update(metadata)
    transcript_file(Path(os.environ['CODEX_HOME']) / 'sessions/rollout-orphan.jsonl', rows)
    monkeypatch.setattr(history, 'run', lambda *a, **kw: '')
    assert app.local_catalog()['conversations'] == []


def test_catalog_only_exports_host_local_evidence(app, store, history, monkeypatch):
    monkeypatch.setattr(app, 'discover', lambda *a, **kw: None)
    monkeypatch.setattr(history, 'run', lambda *a, **kw: '')
    store.merge(conversation(associations=[dict(workspace='/remote/tree', host='peer')],
                             sources=[dict(host='peer', path='/remote/rollout.jsonl')]))
    assert app.local_catalog()['conversations'] == []


def test_remote_catalog_queries_distinct_targets_and_deduplicates_identity(app, history, monkeypatch):
    app.cfg.repos = {}
    app.cfg.hosts = {'first': 'host.example', 'same': 'host.example', 'second': 'second.example'}
    calls = []
    def command(argv, **kw):
        if argv[0] == 'gh':
            return ''
        calls.append(argv)
        assert shlex.split(argv[-1]) == ['zsh', '-lic', 'command t __history-catalog']
        return json.dumps(catalog())
    monkeypatch.setattr(history, 'run', command)
    output = list(app.resume_candidates(['--all']))
    assert len(calls) == 2 and len(output) == 1
    fields = output[0].split('\t')
    assert fields[1] == fields[10] == 'org/remote'
    assert {s['host'] for s in app.store.records()[0]['sources']} == {'first', 'second'}
    assert not app.cfg.repos


def test_catalog_prefers_local_alias_and_never_matches_same_basename(app, history, monkeypatch):
    monkeypatch.setattr(history, 'run', lambda *a, **kw: '')
    monkeypatch.setattr(app, 'local_catalog', lambda: dict(version=1, repositories=[], conversations=[]))
    app.cfg.hosts = {'remote': 'host'}
    monkeypatch.setattr(history, 'run', lambda argv, **kw: json.dumps(catalog('other/repo')) if argv[0] == 'ssh' else '')
    rows = list(app.resume_candidates(['--all']))
    assert rows[0].split('\t')[1] == 'other/repo'
    app.import_catalog(catalog('org/repo', 'local-key'), 'remote')
    assert app.repository_alias('org/repo') == 'repo'
    assert app.repository_alias('other/repo') is None


def test_old_or_offline_host_falls_back_to_scoped_protocol(app, history, monkeypatch):
    app.cfg.hosts = {'legacy': 'old', 'duplicate': 'old'}
    events = []
    monkeypatch.setattr(app, 'local_catalog', lambda: dict(version=1, repositories=[], conversations=[]))
    monkeypatch.setattr(history, 'run', lambda *a, **kw: '[]')
    monkeypatch.setattr(app, 'refresh', lambda alias, **kw: events.append((alias, kw)))
    app.discover_catalog()
    assert events == [('repo', {'hosts': {'legacy': 'old'}})]


@pytest.mark.parametrize('change', ['version', 'repositories', 'conversations', 'repo', 'alias', 'path', 'source-host', 'association-host', 'sid'])
def test_catalog_rejects_malicious_protocol_without_ingesting(app, history, change):
    value = catalog()
    if change == 'version': value['version'] = 9
    elif change in ('repositories', 'conversations'): value[change] = 'not-list'
    elif change == 'repo': value['repositories'][0]['repo'] = 4
    elif change in ('alias', 'path'): value['repositories'][0][change] = 'bad\nvalue'
    elif change == 'source-host': value['conversations'][0]['sources'][0]['host'] = 'recursive-peer'
    elif change == 'association-host': value['conversations'][0]['associations'][0]['host'] = 'recursive-peer'
    else: value['conversations'][0]['sid'] = '../unsafe'
    with pytest.raises((history.HistoryError, TypeError)):
        app.import_catalog(value, 'remote')
    assert not app.store.records()


def test_remote_asserted_github_id_is_ignored(app, history, monkeypatch):
    monkeypatch.setattr(history, 'run', lambda *a, **kw: '')
    value = catalog('other/repo')
    value['repositories'][0]['id'] = 'R_trusted'
    app.import_catalog(value, 'remote')
    assert not app.same_repository('other/repo', 'org/repo')


@pytest.mark.parametrize('response', ['', '[]', 'null', '{}', '{"id":42,"nameWithOwner":"new/repo"}',
                                     '{"id":"unsafe ID","nameWithOwner":"new/repo"}',
                                     '{"id":"R_ok","nameWithOwner":"../bad"}'])
def test_invalid_or_unavailable_repository_proofs_never_create_equivalence(app, history, monkeypatch, response):
    calls = []
    monkeypatch.setattr(history, 'run', lambda argv, **kw: (calls.append(argv), response)[1])
    for _ in range(3):
        assert not app.same_repository('old/repo', 'new/repo')
    assert len(calls) == 2
    assert not (app.store.directory / 'repository-proofs.json').exists()


def test_transferred_names_deduplicate_and_keep_raw_transcript_identity(app, history, monkeypatch):
    calls = []
    monkeypatch.setattr(history, 'run', lambda argv, **kw: (calls.append(argv), proof())[1])
    value = catalog('old/repo')
    value['conversations'][0]['recorded_repo'] = 'old/repo'
    app.import_catalog(value, 'remote')
    app.import_catalog(catalog('new/repo'), 'other')
    for _ in range(100):
        assert app.same_repository('old/repo', 'new/repo')
    assert len(calls) == 2
    row, = app.records('new/repo')
    assert row['repo'] == 'new/repo' and row['recorded_repo'] == 'old/repo'
    source = dict(agent='codex', sid='saved', recorded_repo='old/repo', associations=[dict(workspace='/other/home/trees/remote/remote-3')])
    assert app.verify_source(source, row)


def test_fresh_cache_allows_offline_browse_but_selected_reused_name_is_rechecked(app, history, monkeypatch):
    monkeypatch.setattr(history, 'run', lambda *a, **kw: proof())
    assert app.same_repository('old/repo', 'new/repo')
    app.repository_proofs.clear()
    monkeypatch.setattr(history, 'run', lambda *a, **kw: pytest.fail('fresh browse cache should be used'))
    assert app.same_repository('old/repo', 'new/repo')
    app.strict_identity = True
    monkeypatch.setattr(history, 'run', lambda argv, **kw: proof('R_other' if argv[3] == 'old/repo' else 'R_same'))
    assert not app.same_repository('old/repo', 'new/repo')


def test_expired_cache_offline_and_distinct_ids_refuse_equivalence(app, history, monkeypatch):
    path = app.store.directory / 'repository-proofs.json'
    path.write_text(json.dumps({slug: dict(id='R_same', canonical='new/repo', verified=time.time() - 90000)
                                for slug in ('old/repo', 'new/repo')}))
    monkeypatch.setattr(history, 'run', lambda *a, **kw: '')
    assert not app.same_repository('old/repo', 'new/repo')
    app.repository_proofs.clear()
    monkeypatch.setattr(history, 'run', lambda argv, **kw: proof(argv[3].replace('/', '_')))
    assert not app.same_repository('old/repo', 'new/repo')


def test_alias_lookup_reads_each_local_origin_once(app, monkeypatch):
    calls = []
    monkeypatch.setattr(app, 'identity', lambda alias: (calls.append(alias), 'org/repo')[1])
    for _ in range(100):
        assert app.repository_alias('org/repo') == 'repo'
    assert calls == ['repo']


def materialization_api(app, history, tmp_path, monkeypatch, *, state=None, registration=('run', None)):
    destination = tmp_path / 'code' / 'remote'
    events = []
    state = state or dict(exists=False, nonempty=False, is_repo=False)
    app.cfg.repos = {}
    app.api.update({
        '_new_path': lambda name: str(destination), '_current_local_rc': lambda: str(tmp_path / 'local.zsh'),
        '_local_entries': lambda text: ({}, {}, None), '_new_sh_whence': lambda alias: None,
        '_new_alias_check': lambda *args: registration, '_new_sh_state': lambda path: state,
        '_new_clone_url': lambda owner, name: f'git@github.com:{owner}/{name}.git',
        '_config_read': lambda path: Path(path).read_text() if Path(path).exists() else '',
        '_config_write': lambda path, before, after: events.append(('register', before, after)),
        '_setup_block': lambda *args, **kw: 'selected registration\n',
        'zsh_capture': lambda snippet: events.append(('sync', snippet)),
    })
    def plan(name, alias, url, path, status, register):
        events.append(('plan', url))
        if register[0] == 'error': return [dict(do='error', label=register[1])]
        return [dict(do='run', step='clone', cmd=['git', 'clone', url, path])] if not status['is_repo'] else []
    app.api['_new_land_plan'] = plan
    def command(argv, **kw):
        events.append(tuple(argv))
        if argv[:2] == ['git', 'clone']:
            destination.mkdir(parents=True)
            (destination / '.git').mkdir()
            return ''
        if 'get-url' in argv: return 'git@github.com:org/remote.git'
        return ''
    monkeypatch.setattr(history, 'run', command)
    return destination, events


def test_only_selected_repository_materializes_with_shared_checkout_guards(app, history, tmp_path, monkeypatch):
    destination, events = materialization_api(app, history, tmp_path, monkeypatch)
    assert app.materialize_repository('org/remote') == 'remote'
    assert app.cfg.repos == {'remote': str(destination)}
    assert [e for e in events if e[0] == 'plan'] == [('plan', 'git@github.com:org/remote.git')]
    assert len([e for e in events if e[0] == 'register']) == 1
    assert app.materialize_repository('org/remote') == 'remote'
    assert len([e for e in events if e[0] == 'register']) == 1


@pytest.mark.parametrize('obstacle', ['alias', 'symlink', 'linked', 'wrong-identity', 'local-identity'])
def test_materialization_fails_closed_without_overwriting(app, history, tmp_path, monkeypatch, obstacle):
    state = dict(exists=True, nonempty=True, is_repo=True, origin_url='https://github.com/org/remote') if obstacle == 'linked' else None
    destination, events = materialization_api(app, history, tmp_path, monkeypatch, state=state,
        registration=('error', 'alias collision') if obstacle == 'alias' else ('run', None))
    if obstacle == 'symlink':
        destination.parent.mkdir(parents=True)
        destination.symlink_to(tmp_path)
    if obstacle == 'wrong-identity':
        original = history.run
        monkeypatch.setattr(history, 'run', lambda argv, **kw: 'git@github.com:other/remote.git' if 'get-url' in argv else original(argv, **kw))
    with pytest.raises(history.HistoryError):
        app.materialize_repository('local:/unclonable' if obstacle == 'local-identity' else 'org/remote')
    assert not [e for e in events if e[0] == 'register']


def test_cancel_remoteonly_picker_never_prepares_or_registers(zsh):
    row = '\t'.join([str(int(time.time())), 'org/remote', '3', 'saved', '/gone/tree', 'Today', 'Remote work', 'peer', 'codex', '-', 'org/remote'])
    setup = 'DEV_REPOS=(); _t_resume_history_candidates() { print -r -- ' + shlex.quote(row) + '; }; '
    result = zsh(setup + '_T_RESUME_REQUIRE_PICKER=1; _t_fzf_filtered() { return 130; }; _t_resume_history_launch() { print UNEXPECTED; }; _t_resume --all', _tty=True)
    assert result.returncode == 130 and 'UNEXPECTED' not in result.stdout


def test_remoteonly_prepare_refreshes_actual_alias_before_host_route(zsh):
    row = '\t'.join([str(int(time.time())), 'org/remote', '3', 'saved', '/gone/tree', 'Today', 'Remote work', 'peer', 'codex', '-', 'org/remote'])
    rc = zsh.home / 'selected-registration.zsh'
    setup = ('DEV_HANDOFF[codex]=t; DEV_REPOS=(); T_LOCAL_RC=' + shlex.quote(str(rc)) + '; '
        '_t_resume_history_candidates() { print -r -- ' + shlex.quote(row) + '; }; '
        '_t_resume_history_launch() { print -r -- "DEV_REPOS[remote]=$HOME/code/remote" > "$T_LOCAL_RC"; print "dev-remote-17\t/prepared/remote-17"; }; '
        '_t_resume_on_host() { [[ -n ${DEV_REPOS[remote]:-} ]] || return 99; print -u2 -- "LAND $*"; print dev-remote-17; }; ')
    result = zsh(setup + '_t_resume --all --host destination')
    assert result.returncode == 0, result.stderr
    assert 'LAND destination saved /prepared/remote-17 codex detach' in result.stderr


def test_remote_resume_all_forwards_explicit_scope(zsh):
    result = zsh('REMOTE_HOSTS=(peer user@peer); _t_infer_repo() { print api; }; '
                 'ssh() { print -r -- "SSH ${(j:|:)@}"; }; t remote resume --all --host peer', _tty=True)
    assert result.returncode == 0, result.stderr
    worker = shlex.split(result.stdout.split('SSH -t|user@peer|', 1)[1].splitlines()[0])[2]
    assert shlex.split(worker)[-2:] == ['_t_resume', '--all']


def test_remote_resume_forwards_local_repository_identity(zsh):
    result = zsh('REMOTE_HOSTS=(peer user@peer); '
                 'python3() { [[ $2 == __repo-identity ]] && print org/project; }; '
                 'ssh() { print -r -- "SSH ${(j:|:)@}"; }; t remote resume api 3 --host peer')
    assert result.returncode == 0, result.stderr
    worker = shlex.split(result.stdout.split('SSH user@peer|', 1)[1].splitlines()[0])[2]
    assert '_T_RESUME_REPO_IDENTITY=org/project' in shlex.split(worker)
    assert shlex.split(worker)[-3:] == ['_t_resume', '$t_resume_repo', '3']
    assert 'command t __resume-alias org/project' in worker


@pytest.mark.parametrize('path', ['relative', '/bad\npath', '/bad\x00path'])
def test_catalog_rejects_unsafe_transfer_paths(app, history, monkeypatch, path):
    monkeypatch.setattr(history, 'run', lambda *a, **kw: '')
    value = catalog()
    value['conversations'][0]['sources'][0]['path'] = path
    with pytest.raises(history.HistoryError, match='path'):
        app.import_catalog(value, 'remote')


@pytest.mark.parametrize('mode', ['catalog', 'identity', 'alias', 'missing-alias', 'invalid'])
def test_private_repository_bridge_closes_store(app, history, t_mod, monkeypatch, capsys, mode):
    calls = []
    monkeypatch.setattr(t_mod, 'Config', lambda: app.cfg)
    monkeypatch.setattr(t_mod.runpy, 'run_path', lambda path: {
        'HistoryCommand': lambda cfg, api: app, 'HistoryError': history.HistoryError,
        'REPO_IDENTITY': history.REPO_IDENTITY})
    monkeypatch.setattr(app.store, 'close', lambda: calls.append('closed'))
    monkeypatch.setattr(app, 'local_catalog', lambda: catalog())
    monkeypatch.setattr(app, 'repository_alias', lambda identity: 'repo' if identity == 'org/repo' else None)
    argv = {'catalog': ['__history-catalog'], 'identity': ['__repo-identity', 'repo'],
            'alias': ['__resume-alias', 'org/repo'], 'missing-alias': ['__resume-alias', 'unknown/repo'],
            'invalid': ['__repo-identity', 'unknown']}[mode]
    assert t_mod.main(argv) == (1 if mode == 'invalid' else 0)
    assert calls == ['closed']
    output = capsys.readouterr()
    if mode == 'catalog': assert json.loads(output.out) == catalog()
    elif mode == 'identity': assert output.out == 'org/repo\n'
    elif mode == 'alias': assert output.out == 'repo\n'
    elif mode == 'missing-alias': assert output.out == 'unknown/repo\n'
    else: assert 't resume:' in output.err


def test_exact_remoteonly_selection_bridge_uses_identity_not_label(app, history, t_mod, monkeypatch, capsys):
    monkeypatch.setattr(t_mod, 'Config', lambda: app.cfg)
    monkeypatch.setattr(t_mod.runpy, 'run_path', lambda path: {
        'HistoryCommand': lambda cfg, api: app, 'HistoryError': history.HistoryError,
        'HistoryCancelled': history.HistoryCancelled, 'REPO_IDENTITY': history.REPO_IDENTITY})
    monkeypatch.setattr(app.store, 'close', lambda: None)
    monkeypatch.setattr(app, 'records', lambda identity: [dict(sid='selected', agent='codex')] if identity == 'org/remote' else [])
    monkeypatch.setattr(app, 'resume', lambda label, row, **kwargs: 'dev-remote-3')
    assert t_mod.main(['__resume-saved', 'remote-display', 'selected', 'codex', 'launch', 'org/remote']) == 0
    assert capsys.readouterr().out == 'dev-remote-3\n'
    assert t_mod.main(['__resume-saved', 'remote-display', 'selected', 'codex', 'launch', '../invalid']) == 1


def test_exact_identity_scope_discovers_orphan_history_without_checkout(app, history, monkeypatch):
    app.cfg.repos = {}
    for repository, sid in [('org/project', 'wanted'), ('other/project', 'unrelated')]:
        rows = codex_rows('/gone/trees/project/project-3', sid=sid)
        rows[0]['payload']['git']['repository_url'] = f'https://github.com/{repository}.git'
        transcript_file(Path(os.environ['CODEX_HOME']) / f'sessions/rollout-{sid}.jsonl', rows)
    monkeypatch.setattr(history, 'run', lambda *a, **kw: '')
    rows = list(app.resume_candidates(['org/project']))
    assert len(rows) == 1
    fields = rows[0].split('\t')
    assert fields[3] == 'wanted' and fields[1] == fields[10] == 'org/project'
    assert not app.cfg.repos


def test_forwarded_unregistered_identity_preserves_slot_and_ignores_host_cwd(zsh):
    row = '\t'.join([str(int(time.time())), 'org/project', '3', 'wanted', '/gone/tree', 'Today', 'Exact work', 'local', 'codex', '-', 'org/project'])
    other_slot = row.replace('\t3\twanted\t', '\t4\twrong-slot\t')
    setup = ('_T_RESUME_REPO_IDENTITY=org/project; _T_RESUME_REQUIRE_PICKER=1; '
        'python3() { if [[ $2 == __resume-alias ]]; then print org/project; else command python3 "$@"; fi; }; '
        '_t_infer_repo() { print -u2 UNEXPECTED_INFER; print api; }; '
        '_t_resume_history_candidates() { print -u2 -- "SCOPE $*"; print -r -- ' + shlex.quote(row + '\n' + other_slot) + '; }; '
        '_t_fzf_filtered() { print -u2 -- "CANDIDATES $1"; return 130; }; '
        '_t_resume_history_launch() { print -u2 UNEXPECTED_LAUNCH; }; ')
    result = zsh(setup + '_t_resume controller-alias 3', _tty=True)
    output = result.stdout + result.stderr
    assert result.returncode == 130, output
    assert 'SCOPE org/project' in output and 'wanted' in output
    assert 'wrong-slot' not in output
    assert 'UNEXPECTED_' not in output


@pytest.mark.parametrize('disabled', ['0', 'no', 'off', 'false'])
def test_all_repository_candidates_respect_registered_worktree_opt_out(app, history, monkeypatch, disabled):
    app.cfg.worktree = {'repo': disabled}
    app.import_catalog(catalog('org/repo'), 'remote')
    monkeypatch.setattr(app, 'discover_catalog', lambda: {'org/repo': {}})
    monkeypatch.setattr(history, 'run', lambda *a, **kw: '')
    assert list(app.resume_candidates(['--all'])) == []


@pytest.mark.parametrize('registered', [True, False])
def test_disabled_global_worktrees_prevent_candidates_and_selected_materialization(app, history, monkeypatch, registered):
    app.cfg.worktree_default = '0'
    if not registered:
        app.cfg.repos = {}
    app.import_catalog(catalog('org/repo'), 'remote')
    monkeypatch.setattr(app, 'discover_catalog', lambda: {'org/repo': {}})
    monkeypatch.setattr(history, 'run', lambda *a, **kw: '')
    assert list(app.resume_candidates(['--all'])) == []
    with pytest.raises(history.HistoryError, match='opts out'):
        app.materialize_repository('org/repo')
    monkeypatch.setattr(app, 'owners', lambda *a: [])
    monkeypatch.setattr(app, 'source', lambda *a: pytest.fail('disabled resume must not fetch a transcript'))
    with pytest.raises(history.HistoryError, match='opts out'):
        app.resume('repo', conversation())


def test_repository_override_enables_candidates_under_disabled_default(app, history, monkeypatch):
    app.cfg.worktree_default = '0'
    app.cfg.worktree = {'repo': '1'}
    app.import_catalog(catalog('org/repo'), 'remote')
    monkeypatch.setattr(app, 'discover_catalog', lambda: {'org/repo': {}})
    monkeypatch.setattr(history, 'run', lambda *a, **kw: '')
    assert len(list(app.resume_candidates(['--all']))) == 1


def test_catalog_still_exports_history_for_locally_disabled_worktrees(app, history, store, monkeypatch):
    app.cfg.worktree = {'repo': '0'}
    store.merge(conversation(associations=[dict(workspace='/repo/repo-3', host='local', slot='3')],
                             sources=[dict(host='local', path='/repo/rollout-saved.jsonl')]))
    monkeypatch.setattr(app, 'discover', lambda *a, **kw: None)
    monkeypatch.setattr(history, 'run', lambda *a, **kw: '')
    assert len(app.local_catalog()['conversations']) == 1
    with pytest.raises(history.HistoryError, match='opts out'):
        app.destination('repo', conversation(), 'a' * 40, [])


@pytest.mark.parametrize('change', ['alias-conflict', 'basename-conflict', 'same-mapping', 'linked', 'symlink'])
def test_materialization_rechecks_registration_and_path_after_clone(app, history, t_mod, tmp_path, monkeypatch, change):
    destination, events = materialization_api(app, history, tmp_path, monkeypatch)
    local_rc = tmp_path / 'local.zsh'
    app.api['_local_entries'] = t_mod._local_entries
    app.api['_new_alias_check'] = t_mod._new_alias_check
    original = history.run
    def command(argv, **kw):
        result = original(argv, **kw)
        if argv[:2] == ['git', 'clone']:
            if change in ('alias-conflict', 'basename-conflict', 'same-mapping'):
                alias = 'different' if change == 'basename-conflict' else 'remote'
                path = destination if change == 'same-mapping' else tmp_path / 'elsewhere' / 'remote'
                local_rc.write_text(f'DEV_REPOS[{alias}]={shlex.quote(str(path))}\n')
            elif change == 'linked':
                (destination / '.git').rmdir()
                (destination / '.git').write_text('gitdir: /unrelated/repo\n')
            else:
                destination.rename(destination.with_name('retained-clone'))
                destination.symlink_to(destination.with_name('retained-clone'))
        return result
    monkeypatch.setattr(history, 'run', command)
    if change == 'same-mapping':
        assert app.materialize_repository('org/remote') == 'remote'
        assert app.cfg.repos['remote'] == str(destination)
    else:
        with pytest.raises(history.HistoryError):
            app.materialize_repository('org/remote')
        assert not app.cfg.repos
    assert not [event for event in events if event[0] == 'register']


def test_materialization_atomic_registration_refuses_intervening_config_edit(app, history, t_mod, tmp_path, monkeypatch):
    destination, events = materialization_api(app, history, tmp_path, monkeypatch)
    local_rc = tmp_path / 'local.zsh'
    original = '# existing preferences\n'
    changed = '# concurrent preferences; retain these\n'
    local_rc.write_text(original)
    def write(path, before, after):
        assert before == original
        local_rc.write_text(changed)
        t_mod._config_write(path, before, after)
    app.api['_config_write'] = write
    with pytest.raises(history.HistoryError, match='registration changed'):
        app.materialize_repository('org/remote')
    assert local_rc.read_text() == changed
    assert destination.is_dir() and not app.cfg.repos
    assert not [event for event in events if event[0] == 'sync']


@pytest.mark.parametrize('legacy', [True, False])
def test_remote_resume_identity_bridge_blocks_legacy_alias_collision(zsh, tmp_path, legacy):
    dispatch = zsh('REMOTE_HOSTS=(peer user@peer); '
                   'python3() { [[ $2 == __repo-identity ]] && print org/project; }; '
                   'ssh() { print -r -- "SSH ${(j:|:)@}"; }; t remote resume api 3 --host peer')
    assert dispatch.returncode == 0, dispatch.stderr
    worker = shlex.split(dispatch.stdout.split('SSH user@peer|', 1)[1].splitlines()[0])[2]
    stubdir = tmp_path / 'target-bin'
    stubdir.mkdir()
    binary = stubdir / 't'
    binary.write_text('#!/bin/sh\n' + ('exit 2\n' if legacy else
        '[ "$1" = __resume-alias ] && [ "$2" = org/project ] || exit 2\nprintf "host-key\\n"\n'))
    binary.chmod(0o755)
    target = zsh('PATH=' + shlex.quote(str(stubdir)) + ':$PATH; '
                 'DEV_REPOS=(api /unrelated/repository); '
                 '_t_resume() { print -r -- "TARGET_RESUME $*"; }; '
                 'eval ' + shlex.quote(worker))
    if legacy:
        assert target.returncode == 2
        assert 'TARGET_RESUME' not in target.stdout
    else:
        assert target.returncode == 0, target.stderr
        assert 'TARGET_RESUME host-key 3' in target.stdout
        assert 'TARGET_RESUME api' not in target.stdout
