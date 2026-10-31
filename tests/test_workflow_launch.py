"""Single-model workflow defaults apply only to the selected launch scope."""
import copy
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess
import sys

import pytest

from test_agent_seam import zsh  # noqa: F401 -- isolated HOME and tmux
from test_workflow_sync import sync  # noqa: F401 -- isolated workflow storage


SID = '11111111-1111-1111-1111-111111111111'


def single_policy(w, **settings):
    policy = copy.deepcopy(w['DEFAULT'])
    policy.update(stages_enabled=False, session_defaults=settings)
    return policy


def scoped_document(sync, tmp_path, monkeypatch, **settings):
    w = sync.workflow()
    document = w['defaults']()
    repo = tmp_path / 'repo'
    (repo / '.git').mkdir(parents=True)
    document['repositories'][str(repo)] = {'inline': single_policy(w, **settings)}
    w['save'](document, None, propagate=False)
    calls = []
    def common(cwd, *args):
        calls.append(cwd)
        return str(repo / '.git')
    monkeypatch.setattr(sync, 'git', common)
    return w, document, repo, calls


def test_fresh_defaults_resolve_destination_and_are_read_only(sync, tmp_path, monkeypatch):
    w, document, repo, calls = scoped_document(sync, tmp_path, monkeypatch,
        codex={'model': 'repo-codex', 'effort': 'high'},
        claude={'model': 'repo-claude', 'effort': 'medium'})
    before = w['config_path']().read_bytes()
    assert sync.launch_settings('codex', str(tmp_path / 'linked-worktree' / 'src')) == [
        '--model', 'repo-codex', '-c', 'model_reasoning_effort="high"']
    assert sync.launch_settings('claude', str(repo)) == ['--model', 'repo-claude', '--effort', 'medium']
    assert calls == [str(tmp_path / 'linked-worktree' / 'src'), str(repo)]
    assert sync.launch_settings('unknown', str(repo)) == []
    assert w['config_path']().read_bytes() == before
    assert not sync.state_path().exists()


@pytest.mark.parametrize('condition', ['stages', 'disabled', 'excluded', 'unknown', 'unrelated'])
def test_inactive_or_unresolved_scope_does_not_supply_defaults(sync, tmp_path, monkeypatch, condition):
    w, document, repo, _ = scoped_document(sync, tmp_path, monkeypatch, codex={'model': 'repo-model'})
    policy = document['repositories'][str(repo)]['inline']
    if condition == 'stages':
        policy['stages_enabled'] = True
    elif condition == 'disabled':
        policy['enabled'] = False
    elif condition == 'excluded':
        document['repositories'][str(repo)]['excluded'] = True
    elif condition == 'unknown':
        monkeypatch.setattr(sync, 'git', lambda *args: '')
    else:
        other = tmp_path / 'other' / '.git'
        other.mkdir(parents=True)
        monkeypatch.setattr(sync, 'git', lambda *args: str(other))
    w['save'](document, w['snapshot'](w['config_path']()), propagate=False)
    assert sync.launch_settings('codex', str(repo)) == []
    assert sync.resume_settings(SID, str(repo)) == []


def test_named_workflow_and_blank_fields(sync, tmp_path, monkeypatch):
    w, document, repo, _ = scoped_document(sync, tmp_path, monkeypatch, codex={'model': 'inline'})
    document['workflows']['single'] = single_policy(w, codex={'model': '', 'effort': 'low'})
    document['repositories'][str(repo)] = {'workflow': 'single'}
    w['save'](document, w['snapshot'](w['config_path']()), propagate=False)
    assert sync.launch_settings('codex', str(repo)) == ['-c', 'model_reasoning_effort="low"']
    assert sync.launch_settings('claude', str(repo)) == []


def test_linked_worktree_subdirectory_uses_canonical_repository(sync, tmp_path):
    repo, worktree = tmp_path / 'canonical', tmp_path / 'linked'
    repo.mkdir()
    def git(*args):
        subprocess.run(['git', '-C', str(repo), *args], check=True, capture_output=True)
    git('init', '-q')
    git('config', 'user.name', 'Workflow Test')
    git('config', 'user.email', 'workflow@example.invalid')
    git('config', 'core.hooksPath', str(tmp_path / 'no-hooks'))
    (repo / 'tracked').write_text('initial')
    git('add', 'tracked')
    git('commit', '-qm', 'Initial')
    git('worktree', 'add', '--detach', str(worktree))
    (worktree / 'src').mkdir()
    w = sync.workflow()
    document = w['defaults']()
    document['repositories'][str(repo)] = {'inline': single_policy(w, codex={'model': 'canonical-model'})}
    w['save'](document, None, propagate=False)
    assert sync.launch_settings('codex', str(worktree / 'src')) == ['--model', 'canonical-model']


@pytest.mark.parametrize('settings,expected', [
    ({'model': 'repo-model'}, ['--model', 'repo-model']),
    ({'effort': 'low'}, ['--model', 'global-model', '-c', 'model_reasoning_effort="low"']),
    ({'model': 'repo-model', 'effort': 'high'}, ['--model', 'repo-model', '-c', 'model_reasoning_effort="high"']),
    ({'model': '', 'effort': ''}, ['--model', 'global-model', '-c', 'model_reasoning_effort="medium"']),
])
def test_resume_scoped_defaults_override_guarded_global_intent(sync, tmp_path, monkeypatch, settings, expected):
    _, document, repo, _ = scoped_document(sync, tmp_path, monkeypatch, codex=settings)
    sync.write({'revision': 1, 'workflow_hash': sync.digest(document), 'context': {},
                'main_settings': {'model': 'global-model', 'effort': 'medium'}})
    assert sync.resume_settings(SID, str(repo)) == expected
    state = sync.status()
    state['workflow_hash'] = 'stale'
    sync.write(state)
    assert sync.resume_settings(SID, str(repo)) == sync.launch_settings('codex', str(repo))


def test_indexed_child_cannot_receive_scoped_resume_defaults(sync, tmp_path, monkeypatch):
    _, _, repo, _ = scoped_document(sync, tmp_path, monkeypatch, codex={'model': 'root-only'})
    with sqlite3.connect(Path(os.environ['CODEX_HOME']) / 'state_5.sqlite') as db:
        db.execute('create table threads(id text, source text)')
        db.execute('insert into threads values(?,?)', (SID, '{"subagent":{}}'))
    assert sync.resume_settings(SID, str(repo)) == []


@pytest.mark.parametrize('contents', ['{bad json', '[]', '{"revision":1}'])
def test_corrupt_journal_cannot_supply_global_resume_settings(sync, tmp_path, monkeypatch, contents):
    _, _, repo, _ = scoped_document(sync, tmp_path, monkeypatch, codex={'model': 'repo-model'})
    sync.state_path().write_text(contents)
    assert sync.resume_settings(SID, str(repo)) == ['--model', 'repo-model']


@pytest.mark.parametrize('contents', ['{bad json', '[]', '{"version":3}'])
def test_corrupt_workflow_helper_never_emits_launch_arguments(sync, contents):
    config = sync.workflow()['config_path']()
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_text(contents)
    result = subprocess.run([sys.executable, sync.__file__, '--launch-settings', 'codex', str(config.parent)],
                            capture_output=True, text=True)
    assert result.returncode == 0 and not result.stdout and not result.stderr


def shell_workflow(zsh, *, model='repo-model', effort='high'):
    repo = zsh.home / 'code' / 'api'
    repo.mkdir(parents=True)
    subprocess.run(['git', '-C', str(repo), 'init', '-q'], check=True, capture_output=True)
    document = {'version': 3, 'default': 'standard', 'workflows': {'standard': {
        'enabled': True, 'planning': {'model': 'plan', 'effort': 'auto'},
        'implementation': {'model': 'implement', 'effort': 'auto'},
        'review': {'model': 'review', 'effort': 'one-level-lower'}}}, 'repositories': {str(repo): {'inline': {
        'enabled': True, 'stages_enabled': False,
        'planning': {'model': 'plan', 'effort': 'auto'},
        'implementation': {'model': 'implement', 'effort': 'auto'},
        'review': {'model': 'review', 'effort': 'one-level-lower'},
        'session_defaults': {agent: {'model': model, 'effort': effort} for agent in ('codex', 'claude')},
    }}}}
    config = zsh.home / '.config' / 't' / 'workflow.json'
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps(document))
    return repo


@pytest.mark.parametrize('agent', ['codex', 'claude'])
@pytest.mark.parametrize('effort', ['', 'high'])
def test_shell_fresh_destination_defaults_replace_account_settings(zsh, agent, effort):
    repo = shell_workflow(zsh, effort=effort)
    result = zsh(f'DEV_MODEL[{agent}]=global-model; DEV_EFFORT[{agent}]=medium; '
                 f'_dev_agent_new_cmd {agent} "" "" {shlex.quote(str(repo))}')
    args = shlex.split(result.stdout)
    assert result.returncode == 0, result.stderr
    assert args[args.index('--model') + 1] == 'repo-model'
    assert 'global-model' not in args and 'medium' not in args
    if effort:
        assert ('model_reasoning_effort=high' if agent == 'codex' else 'high') in args
    else:
        assert '-c' not in args and '--effort' not in args


@pytest.mark.parametrize('agent', ['codex', 'claude'])
def test_shell_resume_honors_explicit_arguments(zsh, agent):
    repo = shell_workflow(zsh)
    effort = '-c model_reasoning_effort=low' if agent == 'codex' else '--effort low'
    result = zsh(f'T_WORKFLOW_CWD={shlex.quote(str(repo))} _dev_agent_resume_cmd {agent} {SID} '
                 f'--model explicit {effort}')
    args = shlex.split(result.stdout)
    assert result.returncode == 0, result.stderr
    assert args.count('--model') == 1 and 'explicit' in args
    assert 'repo-model' not in args and 'high' not in args and 'model_reasoning_effort="high"' not in args


def test_shell_foreground_uses_repository_defaults(zsh):
    shell_workflow(zsh)
    result = zsh('DEV_AGENT_DEFAULT=codex; DEV_WORKTREE[api]=0; '
                 '_dev_repo_prepare() { :; }; codex() { print -rl -- "$@"; }; '
                 '_t_dev api new --fg')
    assert result.returncode == 0, result.stderr
    assert result.stdout.endswith('--model\nrepo-model\n-c\nmodel_reasoning_effort=high\n--no-daemon\n')


def test_shell_tmux_new_and_resume_use_destination_not_caller(zsh):
    repo = shell_workflow(zsh)
    result = zsh(f'_dev_new_session dev-api-3 {shlex.quote(str(repo))} dev/x 1 codex; '
                 f'_dev_resume_session dev-api-4 {shlex.quote(str(repo))} {SID} codex')
    assert result.returncode == 0, result.stderr
    commands = [line for line in zsh.log.read_text().splitlines() if line.startswith('respawn-pane ')]
    assert len(commands) == 2
    assert all('--model repo-model' in command for command in commands)
    assert all('model_reasoning_effort=' in command and 'high' in command for command in commands)
