"""Single-model workflow defaults apply only to the selected launch scope."""
import copy
import json
import os
from pathlib import Path
import shlex
import sqlite3
import subprocess

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
