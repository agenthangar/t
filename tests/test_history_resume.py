"""Exact-conversation history actions preserve workspaces and reject owner races."""
import json
import os
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from test_history_data import history, store, app, conversation, codex_rows, transcript_file

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def git_repo(app, history):
    directory = app.cfg.repos['repo']
    history.run(['git', 'init', '-b', 'main', directory])
    history.run(['git', '-C', directory, 'config', 'user.name', 'History test'])
    history.run(['git', '-C', directory, 'config', 'user.email', 'history@example.invalid'])
    Path(directory, 'tracked').write_text('original\n')
    history.run(['git', '-C', directory, 'add', 'tracked'])
    history.run(['git', '-C', directory, 'commit', '-m', 'Initial'])
    history.run(['git', '-C', directory, 'remote', 'add', 'origin', 'https://github.com/org/repo.git'])
    return history.run(['git', '-C', directory, 'rev-parse', 'HEAD']).strip()


def saved_row(app, revision, workspace, *, sid='selected', evidence='resume', observed=1, agent='codex'):
    gitfile = Path(workspace) / '.git'
    generation = ''
    if gitfile.exists():
        import hashlib
        info = gitfile.stat()
        gitdir = subprocess.run(['git', '-C', str(workspace), 'rev-parse', '--absolute-git-dir'], text=True, capture_output=True).stdout.strip()
        generation = hashlib.sha256(json.dumps([gitdir, info.st_dev, info.st_ino, info.st_ctime_ns]).encode()).hexdigest()
    return conversation(sid=sid, agent=agent, associations=[dict(workspace=str(workspace), slot=Path(workspace).name.removeprefix('repo-'), host='local', branch='dev/repo-1', commit=revision, evidence=evidence, observed=observed, generation=generation)])


@pytest.mark.parametrize("leaf", ["1", "repo-1"])
def test_destination_reuses_original_only_with_durable_identity(app, history, store, git_repo, leaf):
    path = Path(app.cfg.worktree_root) / "repo" / leaf
    history.run(['git', '-C', app.cfg.repos['repo'], 'worktree', 'add', '-b', 'dev/repo-1', str(path), git_repo])
    row = store.merge(saved_row(app, git_repo, path))
    assert app.destination('repo', row, git_repo, []) == (str(path), '1', False)


@pytest.mark.parametrize('replacement', ['newer-sid', 'live-owner', 'transcript-only', 'changed-branch', 'changed-head'])
def test_recycled_or_occupied_slot_allocates_historical_revision_without_copying_dirty(app, history, store, git_repo, replacement):
    path = Path(app.cfg.worktree_root) / 'repo/1'
    history.run(['git', '-C', app.cfg.repos['repo'], 'worktree', 'add', '-b', 'dev/repo-1', str(path), git_repo])
    row = store.merge(saved_row(app, git_repo, path, evidence='transcript' if replacement == 'transcript-only' else 'resume'))
    owners = []
    if replacement == 'newer-sid':
        store.merge(saved_row(app, git_repo, path, sid='replacement', observed=2))
    elif replacement == 'live-owner':
        owners = [dict(cwd=str(path), slot='repo-1')]
    elif replacement == 'changed-branch':
        history.run(['git', '-C', str(path), 'checkout', '-b', 'replacement'])
    elif replacement == 'changed-head':
        Path(path, 'tracked').write_text('different commit\n')
        history.run(['git', '-C', str(path), 'commit', '-am', 'New work'])
    Path(path, 'dirty-private').write_text('uncommitted work')
    destination, slot, created = app.destination('repo', row, git_repo, owners)
    try:
        assert created and slot != '1'
        assert history.run(['git', '-C', destination, 'rev-parse', 'HEAD']).strip() == git_repo
        assert history.run(['git', '-C', destination, 'branch', '--show-current']).strip() == f'dev/repo-{slot}'
        assert Path(path, 'dirty-private').read_text() == 'uncommitted work'
        assert not Path(destination, 'dirty-private').exists()
    finally:
        app.reservation.unlink(missing_ok=True)


def test_deleted_worktree_uses_verified_historical_commit_and_avoids_local_remote_refs(app, history, store, git_repo):
    row = saved_row(app, git_repo, Path(app.cfg.worktree_root) / 'repo/1')
    history.run(['git', '-C', app.cfg.repos['repo'], 'branch', 'dev/repo-1'])
    history.run(['git', '-C', app.cfg.repos['repo'], 'update-ref', 'refs/remotes/origin/dev/repo-2', git_repo])
    destination, slot, created = app.destination('repo', row, git_repo, [])
    assert slot == '3' and created
    assert Path(destination).is_dir()
    app.reservation.unlink()


def test_reservation_collision_skips_slot(app, history, git_repo):
    root = Path(app.cfg.worktree_root) / 'repo'
    (root / '.1.history-reservation').write_text('other')
    row = saved_row(app, git_repo, root / '9')
    destination, slot, created = app.destination('repo', row, git_repo, [])
    assert slot == '2' and created
    assert (root / '.1.history-reservation').read_text() == 'other'
    app.reservation.unlink()


@pytest.mark.parametrize('host,state,slot', [('local', 'app', 'repo-1'), ('remote', 'app', 'repo-1'), ('local', 'active', 'repo:foreground'), ('remote', 'active', 'repo:foreground')])
def test_nonterminal_owners_offer_handoff_without_launch(app, history, monkeypatch, host, state, slot):
    calls = []
    monkeypatch.setattr(history.subprocess, 'call', lambda *a, **k: calls.append(a))
    with pytest.raises(history.HistoryError, match='handoff'):
        app.attach(dict(host=host, state=state, slot=slot, sid='selected', cwd='/repo/1', agent='codex'))
    assert not calls


@pytest.mark.parametrize('host', ['local', 'remote'])
def test_terminal_attach_invokes_only_exact_owner_helper(app, history, monkeypatch, host):
    app.cfg.hosts = {'remote': 'test.example'}
    calls = []
    monkeypatch.setattr(history.subprocess, 'call', lambda argv: (calls.append(argv), 0)[1])
    assert app.attach(dict(host=host, state='active', slot='repo-1', sid='selected', cwd='/repo/1', agent='claude')) == 0
    assert '_t_history_attach' in calls[0][-1]
    assert '_t_open' not in calls[0][-1]
    if host == 'remote':
        assert calls[0][:3] == ['ssh', '-t', 'test.example']
        assert 'zsh -lic' in calls[0][-1]


@pytest.mark.parametrize('changed', ['sid', 'cwd', 'agent', 'exited'])
def test_shell_attach_rejects_exited_or_recycled_owner_without_opening(history, tmp_path, changed):
    log = tmp_path / 'calls'
    script = f'''
source {ROOT}/zsh/sessions.zsh
_dev_session_sid() {{ print {'other' if changed == 'sid' else 'selected'}; }}
_dev_agent_of_session() {{ print {'claude' if changed == 'agent' else 'codex'}; }}
_t_open() {{ print OPEN >> {log}; }}
tmux() {{
  case $1 in
    has-session) return {1 if changed == 'exited' else 0} ;;
    display-message) print '$7|{'/changed' if changed == 'cwd' else '/repo/1'}' ;;
    *) print "$*" >> {log} ;;
  esac
}}
_t_history_attach repo-1 selected /repo/1 codex
'''
    result = subprocess.run(['zsh', '-fc', script], text=True, capture_output=True, env=os.environ.copy())
    assert result.returncode == 1
    assert not log.exists()
    assert 'selected owner' in result.stderr


@pytest.mark.parametrize('inside', [False, True])
def test_shell_attach_success_uses_exact_target_and_switches_existing_client(history, tmp_path, inside):
    log = tmp_path / 'calls'
    script = f'''
source {ROOT}/zsh/sessions.zsh
_dev_session_sid() {{ print selected; }}
_dev_agent_of_session() {{ print codex; }}
TMUX={'isolated' if inside else ''}
tmux() {{ case $1 in has-session) return 0 ;; display-message) print '$7|/repo/1' ;; *) print "$*" >> {log} ;; esac }}
_t_history_attach repo-1 selected /repo/1 codex
'''
    result = subprocess.run(['zsh', '-fc', script], text=True, capture_output=True, env=os.environ.copy())
    assert result.returncode == 0, result.stderr
    assert log.read_text().strip() == f'{"switch-client" if inside else "attach-session"} -t $7:'


def test_source_missing_and_identity_changed_preserves_record(app, history, store, tmp_path):
    row = store.merge(conversation(sources=[dict(host='local', path=str(tmp_path / 'missing'))]))
    with pytest.raises(history.HistoryError, match='missing'):
        app.source(row)
    path = transcript_file(tmp_path / 'rollout-other.jsonl', codex_rows(tmp_path, sid='other'))
    row['sources'] = [dict(host='local', path=str(path))]
    with pytest.raises(history.HistoryError, match='missing'):
        app.source(row)
    assert len(store.records()) == 1


def test_verified_local_and_selected_remote_transcript_only(app, history, tmp_path, monkeypatch):
    app.cfg.handoff_for = lambda agent: 't'  # Exercise custom transcript transport.
    workspace = Path(app.cfg.worktree_root) / 'repo/1'
    path = transcript_file(tmp_path / 'rollout-selected.jsonl', codex_rows(workspace, sid='selected'))
    row = saved_row(app, 'a' * 40, workspace)
    row['sources'] = [dict(host='local', path=str(path))]
    assert app.source(row) == path
    app.cfg.hosts = {'remote': 'remote.example'}
    row['sources'] = [dict(host='remote', path='/remote/rollout-selected.jsonl')]
    calls = []
    def fetch(argv, **kwargs):
        calls.append(argv)
        Path(argv[-1]).write_bytes(path.read_bytes())
        return ''
    monkeypatch.setattr(history, 'run', fetch)
    destination = app.source(row)
    assert destination.name == 'rollout-selected.jsonl'
    assert calls[0][0] == 'rsync' and len(calls) == 1


@pytest.mark.parametrize('name,wrong_sid', [('not-rollout.jsonl', False), ('rollout-selected.jsonl', True)])
def test_remote_transcript_filename_or_identity_change_fails(app, history, tmp_path, monkeypatch, name, wrong_sid):
    app.cfg.handoff_for = lambda agent: 't'  # Exercise custom transcript transport.
    workspace = Path(app.cfg.worktree_root) / 'repo/1'
    path = transcript_file(tmp_path / 'tx.jsonl', codex_rows(workspace, sid='other' if wrong_sid else 'selected'))
    row = saved_row(app, 'a' * 40, workspace)
    app.cfg.hosts = {'remote': 'remote.example'}
    row['sources'] = [dict(host='remote', path='/remote/' + name)]
    monkeypatch.setattr(history, 'run', lambda argv, **k: Path(argv[-1]).write_bytes(path.read_bytes()))
    with pytest.raises(history.HistoryError, match='identity'):
        app.source(row)


@pytest.mark.parametrize('agent', ['codex', 'claude'])
def test_transcript_install_preserves_source_and_rejects_overwrite(app, history, tmp_path, agent):
    if agent == 'codex':
        Path(os.environ['CODEX_HOME'], 'thread-writer-locks').mkdir()
    workspace = Path(app.cfg.worktree_root) / 'repo/1'
    rows = codex_rows(workspace, sid='selected') if agent == 'codex' else [dict(type='user', sessionId='selected', cwd=str(workspace), message=dict(content='Hello'))]
    source = transcript_file(tmp_path / 'rollout-selected.jsonl', rows)
    row = saved_row(app, 'a' * 40, workspace, agent=agent)
    original = source.read_bytes()
    target = app.install_transcript(row, source, '/destination/2')
    assert target.read_bytes() == original == source.read_bytes()
    assert app.install_transcript(row, source, '/destination/2') == target
    target.write_text('different')
    with pytest.raises(history.HistoryError, match='differs'):
        app.install_transcript(row, source, '/destination/2')
    assert target.read_text() == 'different' and source.read_bytes() == original


def test_transcript_replaced_after_selection_is_not_installed(app, history, tmp_path):
    workspace = Path(app.cfg.worktree_root) / 'repo/1'
    source = transcript_file(tmp_path / 'rollout-selected.jsonl', codex_rows(workspace, sid='selected'))
    row = saved_row(app, 'a' * 40, workspace)
    row['sources'] = [dict(host='local', path=str(source))]
    selected = app.source(row)
    transcript_file(source, codex_rows(workspace, sid='other'))
    with pytest.raises(history.HistoryError, match='changed'):
        app.install_transcript(row, selected, '/destination/2')
    assert not (Path(os.environ['CODEX_HOME']) / 'sessions/history').exists()


def test_missing_revision_requires_explicit_choice_and_verified_current_main(app, history, monkeypatch):
    calls = []
    monkeypatch.setattr(app, 'pick', lambda *a: (dict(sid='cancel'), '', ''))
    with pytest.raises(history.HistoryError, match='cancelled'):
        app.fallback('repo')
    monkeypatch.setattr(app, 'pick', lambda *a: (dict(sid='main'), '', ''))
    monkeypatch.setattr(history, 'run', lambda argv, **k: (calls.append(argv), 'a' * 40)[1])
    assert app.fallback('repo') == 'a' * 40
    assert calls[0][-3:] == ['fetch', 'origin', 'main']
    assert calls[1][-1] == 'origin/main^{commit}'


def test_resume_attaches_live_owner_before_any_transcript_or_workspace_change(app, history, monkeypatch):
    owner = dict(sid='selected', agent='codex', host='local')
    monkeypatch.setattr(app, 'owners', lambda *a: [owner])
    monkeypatch.setattr(app, 'attach', lambda value: 7)
    monkeypatch.setattr(app, 'source', lambda *a: pytest.fail('must attach only'))
    assert app.resume('repo', conversation(sid='selected')) == 7
    monkeypatch.setattr(app, 'owners', lambda *a: [owner, owner])
    with pytest.raises(history.HistoryError, match='multiple'):
        app.resume('repo', conversation(sid='selected'))


def test_resume_rechecks_ownership_race_before_allocation(app, history, git_repo, monkeypatch, tmp_path):
    row = saved_row(app, git_repo, Path(app.cfg.worktree_root) / 'repo/1')
    scans = iter([[], [dict(sid='selected', agent='codex')]])
    monkeypatch.setattr(app, 'owners', lambda *a: next(scans))
    monkeypatch.setattr(app, 'source', lambda *a: tmp_path / 'unused')
    monkeypatch.setattr(app, 'destination', lambda *a: pytest.fail('race must prevent allocation'))
    with pytest.raises(history.HistoryError, match='became live'):
        app.resume('repo', row)


def test_resume_launch_failure_keeps_worktree_and_releases_reservation(app, history, git_repo, monkeypatch, tmp_path):
    workspace = Path(app.cfg.worktree_root) / 'repo/9'
    source = transcript_file(tmp_path / 'rollout-selected.jsonl', codex_rows(workspace, sid='selected'))
    row = saved_row(app, git_repo, workspace)
    row['sources'] = [dict(host='local', path=str(source))]
    monkeypatch.setattr(app, 'owners', lambda *a: [])
    monkeypatch.setattr(app, 'codex_released', lambda *a, **kw: None)
    original = history.run
    def fail_launch(argv, **kwargs):
        if argv[0] == 'zsh':
            raise history.HistoryError('launch failed')
        return original(argv, **kwargs)
    monkeypatch.setattr(history, 'run', fail_launch)
    with pytest.raises(history.HistoryError, match='launch failed'):
        app.resume('repo', row)
    root = Path(app.cfg.worktree_root) / 'repo'
    assert (root / 'repo-1').is_dir()
    assert not list(root.glob('.*.history-reservation'))
    assert source.exists()


def test_resume_success_records_exact_agent_destination_and_revision(app, history, git_repo, store, monkeypatch, tmp_path, capsys):
    workspace = Path(app.cfg.worktree_root) / 'repo/9'
    source = transcript_file(tmp_path / 'rollout-selected.jsonl', codex_rows(workspace, sid='selected'))
    row = store.merge(saved_row(app, git_repo, workspace))
    row['sources'] = [dict(host='local', path=str(source))]
    monkeypatch.setattr(app, 'owners', lambda *a: [])
    monkeypatch.setattr(app, 'attach', lambda owner: 0)
    monkeypatch.setattr(app, 'codex_released', lambda *a, **kw: None)
    original, launches = history.run, []
    def launch(argv, **kwargs):
        if argv[0] == 'zsh':
            launches.append(argv)
            return ''
        return original(argv, **kwargs)
    monkeypatch.setattr(history, 'run', launch)
    assert app.resume('repo', row) == 0
    assert '_dev_resume_session_claimed dev-repo-1' in launches[0][-1]
    assert 'selected codex' in launches[0][-1]
    assert git_repo in capsys.readouterr().err
    assert any(a['slot'] == '1' and a['commit'] == git_repo for a in store.records()[0]['associations'])


def test_history_cli_forms_inference_json_and_readonly(t_mod, app, monkeypatch, capsys):
    parser = t_mod.build_parser()
    for argv in [['history', 'repo', '5'], ['session', 'history', 'repo', '5']]:
        args = parser.parse_args(t_mod._canonical_argv(argv))
        assert args.target == ['repo', '5'] and args.limit == 50 and args.page == 1
    monkeypatch.setattr(app.cfg, 'repo_of_dir', lambda cwd: ('repo', '5'))
    assert app.scope([]) == ('repo', '5')
    assert app.scope(['5']) == ('repo', '5')
    assert app.scope(['repo', '5']) == ('repo', '5')
    for target in [['unknown'], ['repo', '0'], ['repo', '5', 'extra']]:
        with pytest.raises(ValueError):
            app.scope(target)
    monkeypatch.setattr(app, 'discover', lambda *a: None)
    monkeypatch.setattr(app, 'owners', lambda *a: [])
    monkeypatch.setattr(app, 'branch_prs', lambda *a, **k: None)
    monkeypatch.setattr(app, 'pr_states', lambda *a, **k: None)
    monkeypatch.setattr(app, 'records', lambda *a: [conversation()])
    monkeypatch.setattr(app, 'resume', lambda *a: pytest.fail('non TTY must not launch'))
    assert app.execute(SimpleNamespace(target=['repo', '5'], json=True, refresh=False, page=1, limit=50)) == 0
    output = json.loads(capsys.readouterr().out)
    assert output['total'] == 1 and output['limit'] == 50
    assert app.execute(SimpleNamespace(target=['repo', '5'], json=False, refresh=False, page=1, limit=50)) == 0
    listing = capsys.readouterr().out
    assert 'history   repo slot 5' in listing
    assert all(heading in listing for heading in ('STATUS', 'UI', 'MODEL', 'HOST', 'SESSION', 'WORKING ON', 'DATE', 'BRANCH', 'PRS', 'ID'))
    assert 'same-sid' in listing and 'Implement history' in listing and 'Page 1' in listing
    assert '\x1b' not in listing


def test_deleted_and_recreated_same_branch_head_has_different_generation(app, history, store, git_repo):
    path = Path(app.cfg.worktree_root) / 'repo/1'
    history.run(['git', '-C', app.cfg.repos['repo'], 'worktree', 'add', '-b', 'dev/repo-1', str(path), git_repo])
    row = store.merge(saved_row(app, git_repo, path))
    history.run(['git', '-C', app.cfg.repos['repo'], 'worktree', 'remove', str(path)])
    history.run(['git', '-C', app.cfg.repos['repo'], 'branch', '-D', 'dev/repo-1'])
    history.run(['git', '-C', app.cfg.repos['repo'], 'worktree', 'add', '-b', 'dev/repo-1', str(path), git_repo])
    (path / 'private-uncommitted').write_bytes(b'preserve me')
    destination, slot, created = app.destination('repo', row, git_repo, [])
    assert created and slot == '2'
    assert (path / 'private-uncommitted').read_bytes() == b'preserve me'
    assert not Path(destination, 'private-uncommitted').exists()
    app.reservation.unlink()


def test_python_claims_contend_with_zsh_launch_lock_protocol(app, history):
    destination, sid = '/sandbox/workspace', 'selected'
    import hashlib
    key = hashlib.sha256(sid.encode()).hexdigest()
    path = Path(os.environ.get('XDG_CACHE_HOME') or Path.home() / '.cache') / 'claude-sessions/launch-sids' / (key + '.lock')
    script = 'zmodload zsh/system; zsystem flock -t 1 -f fd ' + str(path)
    with app.launch_claims(destination, sid):
        result = subprocess.run(['zsh', '-fc', script], text=True, capture_output=True)
        assert result.returncode != 0
    assert subprocess.run(['zsh', '-fc', script], capture_output=True).returncode == 0


def test_claim_timeout_releases_workspace_claim(app, history, monkeypatch):
    attempts = []
    def busy(*args):
        attempts.append(args)
        raise BlockingIOError
    monkeypatch.setattr(history.fcntl, 'lockf', busy)
    values = iter([0, 16])
    monkeypatch.setattr(history.time, 'monotonic', lambda: next(values))
    with pytest.raises(history.HistoryError, match='timed out'):
        with app.launch_claims('/sandbox/workspace', 'selected'):
            pytest.fail('cannot enter claim')
    assert len(attempts) == 1


def test_final_owner_race_is_blocked_before_transcript_restore(app, history, git_repo, monkeypatch, tmp_path):
    workspace = Path(app.cfg.worktree_root) / 'repo/9'
    row = saved_row(app, git_repo, workspace)
    monkeypatch.setattr(app, 'source', lambda *a: tmp_path / 'unused')
    owners = iter([[], [], [dict(sid='selected', agent='codex', cwd='/elsewhere')]])
    monkeypatch.setattr(app, 'owners', lambda *a: next(owners))
    monkeypatch.setattr(app, 'install_transcript', lambda *a: pytest.fail('restore before final ownership check'))
    with pytest.raises(history.HistoryError, match='became live before restore'):
        app.resume('repo', row)
    assert not list((Path(app.cfg.worktree_root) / 'repo').glob('.*.history-reservation'))


def test_destination_revision_race_is_blocked_under_claim(app, history, git_repo, monkeypatch, tmp_path):
    workspace = Path(app.cfg.worktree_root) / 'repo/9'
    row = saved_row(app, git_repo, workspace)
    monkeypatch.setattr(app, 'source', lambda *a: tmp_path / 'unused')
    monkeypatch.setattr(app, 'owners', lambda *a: [])
    original = app.destination
    def modified(*args):
        result = original(*args)
        path = result[0]
        Path(path, 'tracked').write_text('raced commit\n')
        history.run(['git', '-C', path, 'commit', '-am', 'Race'])
        return result
    monkeypatch.setattr(app, 'destination', modified)
    monkeypatch.setattr(app, 'install_transcript', lambda *a: pytest.fail('wrong revision cannot restore'))
    with pytest.raises(history.HistoryError, match='revision changed'):
        app.resume('repo', row)


def test_generation_race_after_destination_is_blocked_under_claim(app, history, store, git_repo, monkeypatch, tmp_path):
    workspace = Path(app.cfg.worktree_root) / 'repo/1'
    history.run(['git', '-C', app.cfg.repos['repo'], 'worktree', 'add', '-b', 'dev/repo-1', str(workspace), git_repo])
    row = store.merge(saved_row(app, git_repo, workspace))
    monkeypatch.setattr(app, 'source', lambda *a: tmp_path / 'unused')
    monkeypatch.setattr(app, 'owners', lambda *a: [])
    original = app.destination
    def modified(*args):
        result = original(*args)
        Path(result[0], '.git').touch()
        return result
    monkeypatch.setattr(app, 'destination', modified)
    with pytest.raises(history.HistoryError, match='generation changed'):
        app.resume('repo', row)


def test_codex_history_launch_sets_destination_cd_and_private_backend(history, tmp_path):
    commands = tmp_path / 'commands'
    script = f'''
source {ROOT}/zsh/agent.zsh
source {ROOT}/zsh/sessions.zsh
source {ROOT}/zsh/resume.zsh
_dev_issue_preparing() {{ return 1; }}
_dev_launch_preflight() {{ return 0; }}
_dev_auto_trust() {{ return 0; }}
_dev_launch_startup_check() {{ return 0; }}
_dev_history_record() {{ return 0; }}
_dev_recovery_watch() {{ return 0; }}
tmux() {{ [[ $1 == respawn-pane ]] && print -r -- "$*" > {commands}; return 0; }}
_dev_resume_session_claimed dev-repo-2 '{tmp_path}/destination 2' selected codex history
'''
    env = {key: value for key, value in os.environ.items() if not key.startswith('COV_CORE_')}
    env.update(HOME=str(tmp_path), ZDOTDIR=str(tmp_path),
               XDG_CACHE_HOME=str(tmp_path / 'cache'), XDG_CONFIG_HOME=str(tmp_path / 'config'),
               XDG_STATE_HOME=str(tmp_path / 'state'), XDG_DATA_HOME=str(tmp_path / 'data'))
    result = subprocess.run(['zsh', '-fc', script], text=True, capture_output=True, env=env)
    assert result.returncode == 0, result.stderr
    assert 'codex resume selected --cd' in commands.read_text()
    assert 'destination\\ 2 --no-daemon' in commands.read_text()


def test_newer_remote_source_wins_stale_local_and_updates_exact_indexed_rollout(app, history, tmp_path, monkeypatch):
    app.cfg.handoff_for = lambda agent: 't'
    import sqlite3
    sid = 'selected'
    workspace = Path(app.cfg.worktree_root) / 'repo/1'
    local = transcript_file(Path(os.environ['CODEX_HOME']) / 'sessions/rollout-selected.jsonl', codex_rows(workspace, sid=sid))
    remote = tmp_path / 'remote.jsonl'
    remote.write_bytes(local.read_bytes() + b'{"timestamp": 200, "type": "event_msg"}\n')
    db = sqlite3.connect(Path(os.environ['CODEX_HOME']) / 'state_5.sqlite')
    db.execute('create table threads(id text, rollout_path text)')
    db.execute('insert into threads values (?,?)', (sid, str(local)))
    db.commit(); db.close()
    Path(os.environ['CODEX_HOME'], 'thread-writer-locks').mkdir()
    row = saved_row(app, 'a' * 40, workspace)
    row['sources'] = [dict(host='local', path=str(local), updated=10), dict(host='remote', path='/remote/rollout-selected.jsonl', updated=20)]
    app.cfg.hosts = {'remote': 'remote.example'}
    calls = []
    def fetch(argv, **kwargs):
        calls.append(argv)
        Path(argv[-1]).write_bytes(remote.read_bytes())
        return ''
    monkeypatch.setattr(history, 'run', fetch)
    source = app.source(row)
    assert calls and source != local
    original = local.read_bytes()
    assert app.install_transcript(row, source, '/destination/2') == local
    assert local.read_bytes() == remote.read_bytes()
    assert next((app.store.directory / 'transcript-backups').glob('*.jsonl')).read_bytes() == original


def test_archive_restore_supports_missing_original_workspace_and_safe_remote_import(app, history, tmp_path, monkeypatch):
    workspace = Path(app.cfg.worktree_root) / 'repo/1'
    sid = '01234567-89ab-cdef-0123-456789abcdef'
    source = transcript_file(tmp_path / ('rollout-' + sid + '.jsonl'), codex_rows(workspace, sid=sid))
    row = saved_row(app, 'a' * 40, workspace, sid=sid)
    row['archived'] = True
    calls = []
    original = history.runpy.run_path
    def helpers(path):
        result = original(path)
        result['unarchive_thread'] = lambda *a, **k: calls.append((a, k))
        return result
    monkeypatch.setattr(history.runpy, 'run_path', helpers)
    target = app.install_transcript(row, source, str(tmp_path / 'new-workspace'))
    assert target.is_relative_to(Path(os.environ['CODEX_HOME']) / 'archived_sessions')
    assert target.read_bytes() == source.read_bytes()
    assert calls[0][0] == (sid, str(workspace))
    assert calls[0][1]['process_cwd'] == str(tmp_path / 'new-workspace')
    assert not workspace.exists()


def test_remote_archive_release_check_uses_exact_identity_without_wrong_archive_root(app, history, tmp_path):
    import importlib.util
    workspace = Path(app.cfg.worktree_root) / 'repo/1'
    sid = '01234567-89ab-cdef-0123-456789abcdef'
    rows = codex_rows(workspace, sid=sid)
    rows[0]['payload']['cli_version'] = '0.160.0'
    source = transcript_file(tmp_path / ('rollout-' + sid + '.jsonl'), rows)
    row = saved_row(app, 'a' * 40, workspace, sid=sid)
    row['archived'] = True
    Path(os.environ['CODEX_HOME'], 'thread-writer-locks').mkdir()
    app.api['_run'] = lambda *a, **k: SimpleNamespace(returncode=0, stdout='', stderr='')
    app.codex_released(row, source)
    import fcntl
    lock = Path(os.environ['CODEX_HOME'], 'thread-writer-locks', sid + '.lock')
    with lock.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        with pytest.raises(history.HistoryError, match='writer lock is held') as error:
            app.codex_released(row, source)
        assert isinstance(error.value.__cause__, ValueError)
        assert 'Retry: t session history' in str(error.value)


def test_branch_pr_attribution_requires_matching_commit_on_reused_branch(app, history, store, monkeypatch):
    row = store.merge(conversation(associations=[dict(workspace='/repo/1', slot='1', host='local', branch='dev/repo-1', commit='a' * 40)]))
    other = store.merge(conversation(sid='replacement', associations=[dict(workspace='/repo/1', slot='1', host='local', branch='dev/repo-1', commit='b' * 40)]))
    calls = []
    response = {'data': {'repository': {'b0': {'nodes': [dict(number=1, url='https://github.com/org/repo/pull/1', headRefName='dev/repo-1', headRefOid='a' * 40, state='MERGED'), dict(number=2, url='https://github.com/org/repo/pull/2', headRefName='dev/repo-1', headRefOid='b' * 40, state='OPEN')]}}}}
    monkeypatch.setattr(history, 'run', lambda argv, **k: (calls.append(argv), json.dumps(response))[1])
    app.branch_prs([row, other], refresh=True)
    assert [p['number'] for p in row['prs']] == [1]
    assert [p['number'] for p in other['prs']] == [2]
    app.branch_prs([row, other])
    assert len(calls) == 1
    monkeypatch.setattr(history, 'run', lambda *a, **k: (_ for _ in ()).throw(history.HistoryError('offline')))
    app.branch_prs([row, other], refresh=True)
    assert row['prs'][0]['state'] == 'MERGED'


def test_index_merge_retains_source_freshness(app, history, store):
    first = conversation(sources=[dict(host='local', path='/rollout.jsonl', updated=20, size=100)])
    store.merge(first)
    merged = store.merge(conversation(sources=[dict(host='local', path='/rollout.jsonl', available=True)]))
    assert merged['sources'] == [dict(host='local', path='/rollout.jsonl', updated=20, size=100, available=True)]


def test_history_failure_reopens_picker_with_query_and_success_can_retry(app, history, monkeypatch, capsys):
    row = conversation()
    for method in ('discover', 'pr_states', 'branch_prs'):
        monkeypatch.setattr(app, method, lambda *a, **k: None)
    monkeypatch.setattr(app, 'owners', lambda *a: [])
    monkeypatch.setattr(app, 'records', lambda *a: [row])
    monkeypatch.setattr(history.sys.stdin, 'isatty', lambda: True)
    monkeypatch.setattr(history.sys.stdout, 'isatty', lambda: True)
    picks = iter([(row, 'enter', 'keep query'), (row, 'enter', 'keep query')])
    monkeypatch.setattr(app, 'pick', lambda *a: next(picks))
    attempts = []
    def resume(*a):
        attempts.append(a)
        if len(attempts) == 1:
            raise history.HistoryError('retryable failure')
        return 0
    monkeypatch.setattr(app, 'resume', resume)
    assert app.execute(SimpleNamespace(target=['repo', '1'], json=False, refresh=False, page=1, limit=50)) == 0
    assert len(attempts) == 2
    assert 'retryable failure' in capsys.readouterr().err


def test_competing_native_codex_writer_blocks_canonical_extension(app, history, tmp_path):
    import fcntl
    sid = 'selected'
    folder = Path(os.environ['CODEX_HOME'], 'thread-writer-locks')
    folder.mkdir()
    target = tmp_path / 'rollout-selected.jsonl'
    target.write_bytes(b'original\n')
    row = conversation(sid=sid)
    with (folder / (sid + '.lock')).open('a') as owner:
        fcntl.flock(owner, fcntl.LOCK_EX)
        with pytest.raises(history.HistoryError, match='claimed the selected writer'):
            app.publish_transcript(target, b'original\nmore\n', row)
        assert target.read_bytes() == b'original\n'
        assert not (app.store.directory / 'transcript-backups').exists()
    app.publish_transcript(target, b'original\nmore\n', row)
    assert target.read_bytes() == b'original\nmore\n'


def test_canonical_extension_requires_native_contract_and_refuses_changed_lock(app, history, tmp_path, monkeypatch):
    target = tmp_path / 'canonical.jsonl'
    target.write_bytes(b'original\n')
    row = conversation(sid='selected')
    with pytest.raises(history.HistoryError, match='native writer-lock directory'):
        app.publish_transcript(target, b'original\nmore\n', row)
    folder = Path(os.environ['CODEX_HOME'], 'thread-writer-locks')
    folder.mkdir()
    original = history.fcntl.flock
    def changed(fd, operation):
        original(fd, operation)
        lock = folder / 'selected.lock'
        lock.unlink()
        lock.write_text('replacement')
    monkeypatch.setattr(history.fcntl, 'flock', changed)
    with pytest.raises(history.HistoryError, match='writer lock changed'):
        app.publish_transcript(target, b'original\nmore\n', row)
    assert target.read_bytes() == b'original\n'


def test_publish_refuses_symlink_and_target_race(app, history, tmp_path, monkeypatch):
    folder = Path(os.environ['CODEX_HOME'], 'thread-writer-locks')
    folder.mkdir()
    actual = tmp_path / 'actual'
    actual.write_bytes(b'original\n')
    symlink = tmp_path / 'symlink'
    symlink.symlink_to(actual)
    with pytest.raises(history.HistoryError, match='symlink'):
        app.publish_transcript(symlink, b'original\nmore\n', conversation())
    target = tmp_path / 'target'
    target.write_bytes(b'original\n')
    original_open = history.Path.open
    backups, replacements = [], []
    backup_directory = app.store.directory / 'transcript-backups'
    def raced(path, mode='r', *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        if path.parent == backup_directory and mode == 'xb':
            # Backup creation follows the original-byte/prefix validation and
            # precedes the final target check, independent of pathlib internals.
            backups.append(path)
            target.write_bytes(b'other owner\n')
        return stream
    monkeypatch.setattr(history.Path, 'open', raced)
    monkeypatch.setattr(history.os, 'replace', lambda *args: replacements.append(args))
    with pytest.raises(history.HistoryError, match='changed during restore'):
        app.publish_transcript(target, b'original\nmore\n', conversation())
    assert target.read_bytes() == b'other owner\n'
    assert len(backups) == 1 and backups[0].read_bytes() == b'original\n'
    assert replacements == []


def test_source_mutation_during_final_verification_cannot_publish(app, history, tmp_path, monkeypatch):
    workspace = Path(app.cfg.worktree_root) / 'repo/1'
    source = transcript_file(tmp_path / 'rollout-selected.jsonl', codex_rows(workspace, sid='selected'))
    row = saved_row(app, 'a' * 40, workspace)
    original = history.transcript
    def replaced(path, agent, *a):
        value = original(path, agent, *a)
        path.write_bytes(path.read_bytes() + b'{"type":"event_msg"}\n')
        return value
    monkeypatch.setattr(history, 'transcript', replaced)
    with pytest.raises(history.HistoryError, match='changed during verification'):
        app.install_transcript(row, source, '/destination/2')
    assert not Path(os.environ['CODEX_HOME'], 'sessions/history').exists()


def test_source_wrong_repository_and_corrupt_index_cannot_change_identity(app, history, tmp_path):
    workspace = Path(app.cfg.worktree_root) / 'repo/1'
    row = saved_row(app, 'a' * 40, workspace)
    value = dict(agent='codex', sid='selected', recorded_repo='other/repo', associations=[dict(workspace=str(workspace))])
    assert not app.verify_source(value, row)
    Path(os.environ['CODEX_HOME'], 'state_5.sqlite').write_bytes(b'corrupt')
    source = transcript_file(tmp_path / 'rollout-selected.jsonl', codex_rows(workspace, sid='selected'))
    row['sources'] = [dict(host='local', path=str(source))]
    assert app.codex_target(row, tmp_path / 'fallback') == source
    app.codex_released(dict(agent='claude'), tmp_path / 'missing')


def test_workspace_reservation_collision_is_preserved(app, history):
    root = Path(app.cfg.worktree_root) / 'repo'
    marker = root / '.1.history-reservation'
    marker.write_text('other')
    with pytest.raises(history.HistoryError, match='another history action'):
        app.reserve(root / '1')
    assert marker.read_text() == 'other'


def test_transient_claim_contention_retries_compatible_lock(app, history, monkeypatch):
    original = history.fcntl.lockf
    calls, sleeps = [], []
    def lock(*args):
        calls.append(args)
        if len(calls) == 1:
            raise BlockingIOError
        return original(*args)
    monkeypatch.setattr(history.fcntl, 'lockf', lock)
    monkeypatch.setattr(history.time, 'sleep', lambda delay: sleeps.append(delay))
    with app.launch_claims('/sandbox/destination', 'selected'):
        assert len(calls) == 3
    assert sleeps == [.05]


def test_missing_revision_resume_uses_explicit_fallback_and_changed_repo_refuses(app, history, git_repo, monkeypatch, tmp_path):
    workspace = Path(app.cfg.worktree_root) / 'repo/9'
    row = saved_row(app, git_repo, workspace)
    row['associations'][0]['commit'] = ''
    choices = []
    monkeypatch.setattr(app, 'fallback', lambda alias: (choices.append(alias), git_repo)[1])
    monkeypatch.setattr(app, 'source', lambda *a: tmp_path / 'unused')
    monkeypatch.setattr(app, 'owners', lambda *a: [])
    monkeypatch.setattr(app, 'identity', lambda alias: 'other/repo')
    with pytest.raises(history.HistoryError, match='repository identity changed'):
        app.resume('repo', row)
    assert choices == []


@pytest.mark.parametrize('mode', ['cancel-slot', 'choose-slot', 'retry-refresh-cancel', 'empty', 'owners-unavailable'])
def test_interactive_history_controls_preserve_records(app, history, monkeypatch, capsys, mode):
    row = conversation(associations=[dict(workspace='/repo/1', slot='1', host='local')])
    calls = []
    for method in ('discover', 'pr_states', 'branch_prs', 'refresh'):
        monkeypatch.setattr(app, method, lambda *a, _method=method, **k: calls.append((_method, a, k)))
    if mode == 'owners-unavailable':
        monkeypatch.setattr(app, 'owners', lambda *a: (_ for _ in ()).throw(history.HistoryError('unavailable')))
    else:
        monkeypatch.setattr(app, 'owners', lambda *a: [])
    monkeypatch.setattr(app, 'records', lambda *a: [] if mode == 'empty' else [row])
    monkeypatch.setattr(history.sys.stdin, 'isatty', lambda: mode != 'owners-unavailable')
    monkeypatch.setattr(history.sys.stdout, 'isatty', lambda: mode != 'owners-unavailable')
    monkeypatch.setattr(app.cfg, 'repo_of_dir', lambda cwd: ('repo', ''))
    monkeypatch.setattr(app, 'choose_slot', lambda *a: None if mode == 'cancel-slot' else '1')
    selections = iter([(None, 'retry', 'q'), (None, 'ctrl-r', 'q'), (None, '', 'q')]) if mode == 'retry-refresh-cancel' else iter([(None, '', '')])
    monkeypatch.setattr(app, 'pick', lambda *a: next(selections))
    monkeypatch.setattr(app, 'resume', lambda *a: pytest.fail('control must not launch'))
    target = ['repo'] if mode in ('cancel-slot', 'choose-slot') else ['repo', '1']
    expected = 0 if mode in ('empty', 'owners-unavailable') else 130
    assert app.execute(SimpleNamespace(target=target, json=False, refresh=True, page=1, limit=50)) == expected
    if mode == 'retry-refresh-cancel':
        assert len([call for call in calls if call[0] == 'refresh']) == 2
    if mode == 'empty':
        assert 'No retained conversations' in capsys.readouterr().out



def tool_event(call_id, source):
    return dict(type='response_item', payload=dict(type='custom_tool_call', name='exec', call_id=call_id, input=source))


def tool_output(call_id, **result):
    return dict(type='response_item', payload=dict(type='custom_tool_call_output', call_id=call_id, output=json.dumps(result)))


def test_real_shaped_async_create_and_exact_poll_backfill_across_cache_append(app, history, tmp_path):
    workspace = Path(app.cfg.worktree_root) / 'repo/1'
    source = transcript_file(tmp_path / 'rollout-selected.jsonl', codex_rows(workspace, sid='selected') + [
        tool_event('create', 'text(await tools.exec_command({cmd: ' + json.dumps('gh pr create --title "History \"quoted\" title"') + '}));'),
        tool_output('create', session_id=42, exit_code=None, output=''),
    ])
    value = app.store.scan(source, 'codex')
    assert value['prs'] == []
    with source.open('a') as stream:
        for row in [tool_event('unknown-poll', 'text(await tools.write_stdin({session_id: 99}));'),
                    tool_output('unknown-poll', exit_code=0, output='https://github.com/other/repo/pull/9'),
                    tool_event('poll', 'text(await tools.write_stdin({session_id: 42, chars: ""}));'),
                    tool_output('poll', exit_code=0, output='https://github.com/org/repo/pull/2')]:
            stream.write(json.dumps(row) + '\n')
    value = app.store.scan(source, 'codex')
    assert [(p['repo'], p['number']) for p in value['prs']] == [('org/repo', 2)]
    app.ingest(value, 'repo', source)
    assert app.records('repo')[0]['prs'][0]['number'] == 2


@pytest.mark.parametrize('mode', ['reuse', 'failed-negative', 'failed-positive', 'no-terminal-success', 'quoted-poll'])
def test_async_lineage_never_promotes_reused_failed_or_unproved_poll_output(app, history, tmp_path, mode):
    workspace = Path(app.cfg.worktree_root) / 'repo/1'
    rows = codex_rows(workspace, sid='selected') + [
        tool_event('create', 'text(await tools.exec_command({cmd: "gh pr create"}));'),
        tool_output('create', session_id=42, exit_code=None, output=''),
    ]
    if mode == 'reuse':
        rows += [tool_event('unrelated', 'text(await tools.exec_command({cmd: "printf unrelated"}));'),
                 tool_output('unrelated', session_id=42, exit_code=None, output='')]
    poll = 'text("tools.write_stdin({session_id: 42})");' if mode == 'quoted-poll' else 'text(await tools.write_stdin({session_id: 42}));'
    rows.append(tool_event('poll', poll))
    result = dict(output='https://github.com/org/repo/pull/9')
    if mode != 'no-terminal-success':
        result['exit_code'] = -15 if mode == 'failed-negative' else 1 if mode == 'failed-positive' else 0
    rows.append(tool_output('poll', **result))
    value = history.transcript(transcript_file(tmp_path / 'rollout-selected.jsonl', rows), 'codex')
    assert value['prs'] == []


def test_async_cell_wait_preserves_known_invocation_and_requires_success(app, history, tmp_path):
    workspace = Path(app.cfg.worktree_root) / 'repo/1'
    rows = codex_rows(workspace, sid='selected') + [
        tool_event('create', 'text(await tools.exec_command({cmd: "gh pr create"}));'),
        dict(type='response_item', payload=dict(type='custom_tool_call_output', call_id='create', output='Script running with cell ID known-cell')),
        dict(type='response_item', payload=dict(type='function_call', name='wait', call_id='wait', arguments=json.dumps(dict(cell_id='known-cell')))),
        dict(type='response_item', payload=dict(type='function_call_output', call_id='wait', output=json.dumps(dict(exit_code=0, output='https://github.com/org/repo/pull/2')))),
    ]
    value = history.transcript(transcript_file(tmp_path / 'rollout-selected.jsonl', rows), 'codex')
    assert value['prs'][0]['number'] == 2


@pytest.mark.parametrize('source', [
    'text({cmd:"gh pr create"}); text("https://github.com/org/repo/pull/9");',
    'text("tools.exec_command({cmd: \"gh pr create\"})");',
    '// tools.exec_command({cmd:"gh pr create"})\ntext("unrelated");',
    'text(await tools.exec_command({cmd: "printf unrelated", nested: {cmd:"gh pr create"}}));',
    'text(await tools.exec_command({cmd: "gh pr create"})); text(await tools.exec_command({cmd:"printf unrelated"}));',
])
def test_custom_exec_pr_evidence_requires_actual_unambiguous_invocation(app, history, tmp_path, source):
    workspace = Path(app.cfg.worktree_root) / 'repo/1'
    rows = codex_rows(workspace, sid='selected') + [tool_event('candidate', source),
             tool_output('candidate', exit_code=0, output='https://github.com/org/repo/pull/9')]
    assert history.transcript(transcript_file(tmp_path / 'rollout-selected.jsonl', rows), 'codex')['prs'] == []


@pytest.mark.parametrize('command', [
    "cat <<'EOF'\ngh pr create\nEOF\n",
    'false && gh pr create; printf "https://github.com/org/repo/pull/9\\n"',
    'false || gh pr create',
    'printf unrelated\ngh pr create',
])
def test_shell_branches_and_heredoc_prose_do_not_prove_a_created_pr(app, history, tmp_path, command):
    workspace = Path(app.cfg.worktree_root) / 'repo/1'
    rows = codex_rows(workspace, sid='selected') + [tool_event('candidate', 'text(await tools.exec_command({cmd:' + json.dumps(command) + '}));'),
             tool_output('candidate', exit_code=0, output='https://github.com/org/repo/pull/9')]
    assert history.transcript(transcript_file(tmp_path / 'rollout-selected.jsonl', rows), 'codex')['prs'] == []


def test_parser_upgrade_rebackfills_unchanged_cached_transcript(app, history, tmp_path):
    workspace = Path(app.cfg.worktree_root) / 'repo/1'
    source = transcript_file(tmp_path / 'rollout-selected.jsonl', codex_rows(workspace, sid='selected') + [
        tool_event('create', 'text(await tools.exec_command({cmd:"gh pr create"}));'),
        tool_output('create', session_id=42, exit_code=None, output=''),
        tool_event('poll', 'text(await tools.write_stdin({session_id:42}));'),
        tool_output('poll', exit_code=0, output='https://github.com/org/repo/pull/2')])
    value = app.store.scan(source, 'codex')
    value['prs'] = []
    value['_scan'].pop('parser')
    app.store.db.execute('update scans set record=? where path=?', (json.dumps(value), str(source)))
    app.store.db.commit()
    rescanned = app.store.scan(source, 'codex')
    assert rescanned['prs'][0]['number'] == 2
    assert rescanned['_scan']['parser'] == history.PARSER_VERSION


def test_history_does_not_reuse_slot_with_two_layouts(app, history, store, git_repo):
    root = Path(app.cfg.worktree_root) / 'repo'
    old, named = root / '1', root / 'repo-1'
    history.run(['git', '-C', app.cfg.repos['repo'], 'worktree', 'add', '-b', 'dev/repo-1', str(old), git_repo])
    history.run(['git', '-C', app.cfg.repos['repo'], 'worktree', 'add', '-b', 'other-conversation', str(named), git_repo])
    row = store.merge(saved_row(app, git_repo, old))
    (named / 'keep').write_text('other conversation work')
    destination, slot, created = app.destination('repo', row, git_repo, [])
    assert created and slot == '2' and Path(destination).name == 'repo-2'
    assert old.is_dir() and (named / 'keep').read_text() == 'other conversation work'
    assert row['sid'] == 'selected'
    app.reservation.unlink()


@pytest.mark.parametrize('prepare_only', [False, True])
def test_saved_resume_returns_actual_fresh_owner_without_attaching(app, history, git_repo, store, monkeypatch, tmp_path, prepare_only):
    workspace = Path('/different/home/trees/old/old-9')
    source = transcript_file(tmp_path / 'rollout-selected.jsonl', codex_rows(workspace, sid='selected'))
    value = saved_row(app, git_repo, workspace)
    value['associations'][0]['slot'] = '9'
    row = store.merge(value)
    row['sources'] = [dict(host='local', path=str(source))]
    monkeypatch.setattr(app, 'owners', lambda *a: [])
    monkeypatch.setattr(app, 'attach', lambda owner: pytest.fail('detached resume must not attach'))
    monkeypatch.setattr(app, 'codex_released', lambda *a, **kw: None)
    original, launches = history.run, []
    def launch(argv, **kwargs):
        if argv[0] == 'zsh':
            launches.append(argv)
            return ''
        return original(argv, **kwargs)
    monkeypatch.setattr(history, 'run', launch)
    result = app.resume('repo', row, detached=True, prepare_only=prepare_only)
    assert result.split('\t')[0] == 'dev-repo-1'
    assert bool(launches) is not prepare_only
    if prepare_only:
        assert Path(result.split('\t')[1]).is_dir()
    assert not list(Path(app.cfg.worktree_root).glob('repo/.*history-reservation'))


def test_detached_saved_resume_refuses_new_live_owner(app, history, monkeypatch):
    monkeypatch.setattr(app, 'owners', lambda *a: [dict(sid='selected', agent='codex')])
    with pytest.raises(history.HistoryError, match='became live'):
        app.resume('repo', conversation(sid='selected'), detached=True)


@pytest.mark.parametrize('mode', ['candidates', 'slot-candidates', 'invalid-slot', 'missing-slot',
                                  'launch', 'prepare', 'missing', 'invalid', 'cancel'])
def test_private_resume_bridge_dispatches_exact_identity_and_closes_store(
        app, history, t_mod, monkeypatch, capsys, mode):
    calls = []
    monkeypatch.setattr(t_mod, 'Config', lambda: app.cfg)
    monkeypatch.setattr(t_mod.runpy, 'run_path', lambda path: {
        'HistoryCommand': lambda cfg, api: app,
        'HistoryError': history.HistoryError, 'HistoryCancelled': history.HistoryCancelled})
    monkeypatch.setattr(app.store, 'close', lambda: calls.append('closed'))
    monkeypatch.setattr(app, 'records', lambda alias: [] if mode == 'missing' else
                        [dict(sid='selected', agent='codex')])
    def candidates(aliases, slot=None):
        assert aliases == ['repo']
        assert slot == ('3' if mode == 'slot-candidates' else None)
        if mode == 'cancel':
            raise history.HistoryCancelled('cancel')
        return ['saved metadata']
    monkeypatch.setattr(app, 'resume_candidates', candidates)
    def resume(alias, row, **kwargs):
        calls.append((alias, row, kwargs))
        return 'dev-repo-7'
    monkeypatch.setattr(app, 'resume', resume)
    if mode in ('candidates', 'cancel'):
        argv = ['__resume-candidates', 'repo']
    elif mode in ('slot-candidates', 'invalid-slot', 'missing-slot'):
        argv = ['__resume-candidates', 'repo', '--slot']
        if mode != 'missing-slot':
            argv.append('3' if mode == 'slot-candidates' else 'bad')
    else:
        argv = ['__resume-saved', 'repo', 'selected', 'codex',
                mode if mode in ('prepare', 'invalid') else 'launch']
    assert t_mod.main(argv) == (130 if mode == 'cancel' else 1 if mode in ('missing', 'invalid', 'invalid-slot', 'missing-slot') else 0)
    assert calls[-1] == 'closed'
    out = capsys.readouterr()
    if mode in ('launch', 'prepare'):
        assert calls[0] == ('repo', dict(sid='selected', agent='codex'),
                            dict(detached=True, prepare_only=mode == 'prepare'))
        assert 'dev-repo-7' in out.out
    elif mode in ('candidates', 'slot-candidates'):
        assert out.out == 'saved metadata\n'
    elif mode in ('missing', 'invalid', 'invalid-slot', 'missing-slot'):
        assert out.err.startswith('t resume: ')


@pytest.mark.parametrize('prepare_only', [False, True])
def test_private_saved_resume_reports_ownership_refusal_and_cleans_up(
        app, history, git_repo, store, t_mod, monkeypatch, tmp_path, capsys, prepare_only):
    workspace = tmp_path / '9'
    source = transcript_file(tmp_path / 'rollout-selected.jsonl', codex_rows(workspace, sid='selected'))
    row = store.merge(saved_row(app, git_repo, workspace))
    row['sources'] = [dict(host='local', path=str(source))]
    monkeypatch.setattr(app, 'owners', lambda *a: [])
    monkeypatch.setattr(app, 'records', lambda alias: [row])
    monkeypatch.setattr(app, 'source', lambda row: source)
    monkeypatch.setattr(app, 'install_transcript', lambda *a: pytest.fail('refused restore must not write a transcript'))
    original_run = history.run
    def run(argv, **kwargs):
        assert argv[0] != 'zsh', 'refused restore must not launch'
        return original_run(argv, **kwargs)
    monkeypatch.setattr(history, 'run', run)
    app.api['_run'] = lambda *a, **k: pytest.fail('refused restore must not inspect processes')
    refusal = ValueError('another loaded Codex conversation could not be matched to its worktree. Finish its turn and close the owning app, then retry resume.')
    def refuse(*a, **k):
        assert k['retry'] == 'Retry: t session history, then select the conversation again.'
        raise refusal
    def helpers(path):
        if str(path).endswith('t_app_handoff.py'):
            return {'assert_released': refuse}
        return {'HistoryCommand': lambda cfg, api: app,
                'HistoryError': history.HistoryError, 'HistoryCancelled': history.HistoryCancelled}
    monkeypatch.setattr(history.runpy, 'run_path', helpers)
    monkeypatch.setattr(t_mod, 'Config', lambda: app.cfg)
    closed = []
    original_close = app.store.close
    def close():
        closed.append(True)
        original_close()
    monkeypatch.setattr(app.store, 'close', close)
    assert t_mod.main(['__resume-saved', 'repo', 'selected', 'codex',
                       'prepare' if prepare_only else 'launch']) == 1
    out = capsys.readouterr()
    assert out.out == ''
    assert 't resume: ' + str(refusal) in out.err
    assert 'Traceback' not in out.err
    assert closed == [True]
    assert app.reservation is None
    assert not list(Path(app.cfg.worktree_root).glob('repo/.*history-reservation'))


def test_moved_source_checks_current_workspace_owner_and_original_rollout(app, history, tmp_path):
    import fcntl
    original = tmp_path / 'original-workspace'
    current = tmp_path / 'current-workspace'
    sid = '01234567-89ab-cdef-0123-456789abcdef'
    other = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
    rows = codex_rows(original, sid=sid)
    rows[0]['payload']['cli_version'] = '0.160.0'
    source = transcript_file(tmp_path / ('rollout-' + sid + '.jsonl'), rows)
    row = saved_row(app, 'a' * 40, current, sid=sid)
    home = Path(os.environ['CODEX_HOME'])
    locks = home / 'thread-writer-locks'
    locks.mkdir()
    app.api['_run'] = lambda *a, **k: SimpleNamespace(returncode=0, stdout='', stderr='')
    # A lifecycle move does not rewrite the historical session_meta cwd.
    app.codex_released(row, source, workspace=str(current))
    sibling_rows = codex_rows(current, sid=other)
    sibling = home / 'sessions' / ('rollout-' + other + '.jsonl')
    sibling.parent.mkdir(parents=True, exist_ok=True)
    transcript_file(sibling, sibling_rows)
    with (locks / (other + '.lock')).open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        with pytest.raises(history.HistoryError, match='another Codex conversation'):
            app.codex_released(row, source, workspace=str(current))


@pytest.mark.parametrize('selection', ['missing-local', 'timestamp-tie'])
def test_native_policy_checked_at_actual_remote_source(app, history, tmp_path, monkeypatch, selection):
    app.cfg.hosts = {'remote': 'remote.example'}
    workspace = Path(app.cfg.worktree_root) / 'repo/1'
    row = saved_row(app, 'a' * 40, workspace)
    local = tmp_path / 'rollout-selected.jsonl'
    if selection == 'timestamp-tie':
        transcript_file(local, codex_rows(workspace, sid='selected'))
    row['sources'] = [dict(host='local', path=str(local), updated=20, size=1),
                      dict(host='remote', path='/remote/rollout-selected.jsonl',
                           updated=20 if selection == 'timestamp-tie' else 10, size=100)]
    monkeypatch.setattr(history, 'run', lambda *a, **kw: pytest.fail('native must not rsync'))
    with pytest.raises(history.HistoryError, match='native desktop app'):
        app.source(row)
    assert not (app.store.directory / 'transcripts').exists()


def test_native_policy_allows_verified_local_source_with_incidental_remote_record(app, history, tmp_path):
    workspace = Path(app.cfg.worktree_root) / 'repo/1'
    path = transcript_file(tmp_path / 'rollout-selected.jsonl', codex_rows(workspace, sid='selected'))
    row = saved_row(app, 'a' * 40, workspace)
    app.cfg.hosts = {'remote': 'remote.example'}
    row['sources'] = [dict(host='local', path=str(path), updated=20),
                      dict(host='remote', path='/remote/rollout-selected.jsonl', updated=10)]
    assert app.source(row) == path


@pytest.mark.parametrize('prepare_only', [False, True])
@pytest.mark.parametrize('writer', ['original', 'destination', 'selected'])
def test_relocated_resume_checks_destination_and_selected_writer(
        app, history, git_repo, store, monkeypatch, tmp_path, prepare_only, writer):
    import fcntl
    original = Path(app.cfg.worktree_root) / 'repo/9'
    destination = Path(app.cfg.worktree_root) / 'repo/repo-1'
    sid = '01234567-89ab-cdef-0123-456789abcdef'
    other = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
    rows = codex_rows(original, sid=sid)
    rows[0]['payload']['cli_version'] = '0.160.0'
    home = Path(os.environ['CODEX_HOME'])
    source = transcript_file(home / 'sessions' / ('rollout-' + sid + '.jsonl'), rows)
    source_bytes = source.read_bytes()
    row = store.merge(saved_row(app, git_repo, original, sid=sid))
    row['sources'] = [dict(host='local', path=str(source))]
    locks = home / 'thread-writer-locks'
    locks.mkdir()
    sibling = transcript_file(home / 'sessions' / ('rollout-' + other + '.jsonl'),
                              codex_rows(destination if writer == 'destination' else original, sid=other))
    sibling_bytes = sibling.read_bytes()
    monkeypatch.setattr(app, 'owners', lambda *a: [])
    app.api['_run'] = lambda *a, **kw: SimpleNamespace(returncode=1, stdout='', stderr='')
    run, launches = history.run, []
    def launch(argv, **kwargs):
        if argv[0] == 'zsh':
            launches.append(argv)
            return ''
        return run(argv, **kwargs)
    monkeypatch.setattr(history, 'run', launch)
    held = locks / ((sid if writer == 'selected' else other) + '.lock')
    with held.open('a') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        if writer == 'original':
            result = app.resume('repo', row, detached=True, prepare_only=prepare_only)
            assert result == 'dev-repo-1' + ('\t' + str(destination) if prepare_only else '')
            assert bool(launches) == (not prepare_only)
            assert any(a['workspace'] == str(destination) and a['evidence'] == 'resume'
                       for a in store.records()[0]['associations'])
        else:
            message = 'writer lock is held' if writer == 'selected' else 'another Codex conversation'
            with pytest.raises(history.HistoryError, match=message):
                app.resume('repo', row, detached=True, prepare_only=prepare_only)
            assert launches == []
            assert not any(a['workspace'] == str(destination) for a in store.records()[0]['associations'])
        with held.open() as probe:
            with pytest.raises(BlockingIOError):
                fcntl.flock(probe, fcntl.LOCK_EX | fcntl.LOCK_NB)
    assert source.read_bytes() == source_bytes
    assert sibling.read_bytes() == sibling_bytes
    assert app.reservation is None
    assert not list(destination.parent.glob('.*.history-reservation'))
