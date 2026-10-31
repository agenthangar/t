"""Saved workflow defaults apply only to the selected launch scope and tool."""
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


@pytest.mark.parametrize('condition', ['disabled', 'excluded', 'unknown'])
def test_inactive_or_unresolved_scope_does_not_supply_defaults(sync, tmp_path, monkeypatch, condition):
    w, document, repo, _ = scoped_document(sync, tmp_path, monkeypatch, codex={'model': 'repo-model'})
    policy = document['repositories'][str(repo)]['inline']
    if condition == 'disabled':
        policy['enabled'] = False
    elif condition == 'excluded':
        document['repositories'][str(repo)]['excluded'] = True
    elif condition == 'unknown':
        monkeypatch.setattr(sync, 'git', lambda *args: '')
    w['save'](document, w['snapshot'](w['config_path']()), propagate=False)
    assert sync.launch_settings('codex', str(repo)) == []
    assert sync.resume_settings(SID, str(repo)) == []


@pytest.mark.parametrize('effort,expected_effort', [('auto', 'medium'), ('low', 'low'), ('high', 'high')])
def test_saved_staged_main_defaults_replace_stale_model_and_effort(sync, tmp_path, monkeypatch, effort, expected_effort):
    w, document, repo, _ = scoped_document(sync, tmp_path, monkeypatch,
        codex={'model': 'ignored-single-model'}, claude={'model': 'ignored-claude'})
    policy = document['repositories'][str(repo)]['inline']
    policy['stages_enabled'] = True
    policy['implementation'] = {'model': 'scoped-sol', 'effort': effort}
    w['save'](document, w['snapshot'](w['config_path']()), propagate=False)
    sync.write({'revision': 1, 'workflow_hash': sync.digest(document), 'context': {},
                'main_settings': {'model': 'stale-astra', 'effort': 'ultra'}})
    before = {path: path.read_bytes() for path in (w['config_path'](), sync.state_path())}
    expected = ['--model', 'scoped-sol', '-c', 'model_reasoning_effort="' + expected_effort + '"']
    assert sync.launch_settings('codex', str(repo)) == expected
    assert sync.resume_settings(SID, str(repo)) == expected
    assert sync.launch_settings('claude', str(repo)) == []
    assert sync.resume_settings(SID, str(repo), 'claude') == []
    assert all(path.read_bytes() == contents for path, contents in before.items())
    with sqlite3.connect(Path(os.environ['CODEX_HOME']) / 'state_5.sqlite') as db:
        db.execute('create table threads(id text, source text)')
        db.execute('insert into threads values(?,?)', (SID, '{"subagent":{}}'))
    assert sync.resume_settings(SID, str(repo)) == []


def test_recommended_unsaved_workflow_does_not_override_launch(sync, tmp_path):
    assert sync.launch_settings('codex', str(tmp_path)) == []
    assert sync.resume_settings(SID, str(tmp_path)) == []
    assert not sync.workflow()['config_path']().exists()
    assert not sync.state_path().exists()


def test_saved_global_stages_apply_to_unregistered_repository(sync, tmp_path, monkeypatch):
    w, document, _, _ = scoped_document(sync, tmp_path, monkeypatch, codex={'model': 'repo-model'})
    other = tmp_path / 'other' / '.git'
    other.mkdir(parents=True)
    monkeypatch.setattr(sync, 'git', lambda *args: str(other))
    expected = ['--model', document['workflows']['standard']['implementation']['model'],
                '-c', 'model_reasoning_effort="medium"']
    assert sync.launch_settings('codex', str(other.parent)) == expected
    assert sync.resume_settings(SID, str(other.parent)) == expected


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


def shell_workflow(zsh, *, model='repo-model', effort='high', staged=False):
    repo = zsh.home / 'code' / 'api'
    repo.mkdir(parents=True)
    subprocess.run(['git', '-C', str(repo), 'init', '-q'], check=True, capture_output=True)
    document = {'version': 3, 'default': 'standard', 'workflows': {'standard': {
        'enabled': True, 'planning': {'model': 'plan', 'effort': 'auto'},
        'implementation': {'model': 'implement', 'effort': 'auto'},
        'review': {'model': 'review', 'effort': 'one-level-lower'}}}, 'repositories': {str(repo): {'inline': {
        'enabled': True, 'stages_enabled': staged,
        'planning': {'model': 'plan', 'effort': 'auto'},
        'implementation': {'model': model, 'effort': effort or 'auto'},
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
@pytest.mark.parametrize('staged', [False, True])
def test_shell_resume_honors_explicit_arguments(zsh, agent, staged):
    repo = shell_workflow(zsh, staged=staged)
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


def test_shell_saved_stages_override_stale_account_main_defaults(zsh):
    repo = shell_workflow(zsh, model='implementation-sol', effort='', staged=True)
    result = zsh(f'DEV_MODEL[codex]=old-astra; DEV_EFFORT[codex]=ultra; '
                 f'_dev_agent_new_cmd codex "" "" {shlex.quote(str(repo))}')
    args = shlex.split(result.stdout)
    assert result.returncode == 0, result.stderr
    assert args[args.index('--model') + 1] == 'implementation-sol'
    assert 'model_reasoning_effort=medium' in args
    assert 'old-astra' not in args and 'ultra' not in args


@pytest.mark.parametrize('staged', [False, True])
def test_scoped_permissions_launch_and_resume_without_model_mode_dependency(sync, tmp_path, monkeypatch, staged):
    w, document, repo, _ = scoped_document(sync, tmp_path, monkeypatch)
    policy = document['repositories'][str(repo)]['inline']
    policy['stages_enabled'] = staged
    policy['permissions'] = {'codex': 'auto', 'claude': 'plan'}
    w['save'](document, w['snapshot'](w['config_path']()), propagate=False)
    before = w['config_path']().read_bytes()
    for agent in ('codex', 'claude'):
        expected = w['permission_args'](policy, agent)
        assert sync.launch_settings(agent, str(repo))[-len(expected):] == expected
        assert sync.resume_settings(SID, str(repo), agent)[-len(expected):] == expected
    assert w['config_path']().read_bytes() == before
    with sqlite3.connect(Path(os.environ['CODEX_HOME']) / 'state_5.sqlite') as db:
        db.execute('create table threads(id text, source text)')
        db.execute('insert into threads values(?,?)', (SID, '{"subagent":{}}'))
    assert sync.resume_settings(SID, str(repo)) == []
    document['repositories'][str(repo)]['excluded'] = True
    w['save'](document, w['snapshot'](w['config_path']()), propagate=False)
    assert sync.launch_settings('codex', str(repo)) == []
    assert sync.resume_settings(SID, str(repo), 'claude') == []


def test_unsaved_permissions_never_override_native_defaults(sync, tmp_path, monkeypatch):
    w = sync.workflow()
    document = w['defaults']()
    document['workflows']['standard']['permissions'] = {'codex': 'full', 'claude': 'auto'}
    monkeypatch.setattr(sync, 'workflow', lambda: {**w, 'load': lambda: (document, None)})
    assert sync.launch_settings('codex', str(tmp_path)) == []
    assert sync.resume_settings(SID, str(tmp_path)) == []
    assert sync.launch_settings('claude', str(tmp_path)) == []
    assert sync.resume_settings(SID, str(tmp_path), 'claude') == []
    assert not w['config_path']().exists()


def shell_permissions(zsh, agent, preset):
    repo = shell_workflow(zsh)
    config = zsh.home / '.config' / 't' / 'workflow.json'
    document = json.loads(config.read_text())
    document['repositories'][str(repo)]['inline']['permissions'] = {agent: preset}
    config.write_text(json.dumps(document))
    return repo


@pytest.mark.parametrize('agent,preset,expected', [
    ('codex', 'auto', ['--sandbox', 'workspace-write', '--ask-for-approval', 'on-request',
                      '-c', 'approvals_reviewer="auto_review"']),
    ('claude', 'acceptEdits', ['--permission-mode', 'acceptEdits']),
])
def test_shell_fresh_and_resume_preserve_permission_pairs_and_effort(zsh, agent, preset, expected):
    repo = shell_permissions(zsh, agent, preset)
    for command in (f'_dev_agent_new_cmd {agent} "" "" {shlex.quote(str(repo))}',
                    f'T_WORKFLOW_CWD={shlex.quote(str(repo))} _dev_agent_resume_cmd {agent} {SID}'):
        result = zsh(command)
        assert result.returncode == 0, result.stderr
        args = shlex.split(result.stdout)
        assert any(args[index:index + len(expected)] == expected for index in range(len(args)))
        assert ('model_reasoning_effort=high' in args or 'model_reasoning_effort="high"' in args
                if agent == 'codex' else '--effort' in args and 'high' in args)


@pytest.mark.parametrize('explicit', [
    '--sandbox read-only', '--sandbox=read-only', '-sread-only', '--ask-for-approval never',
    '--ask-for-approval=never', '-anever', '--full-auto', '--approve-for-me', '--yolo', '--not-so-yolo',
    '--dangerously-bypass-approvals-and-sandbox', '-c approval_policy=never',
    '--config sandbox_mode=read-only', '--config=approvals_reviewer=user',
    '-cdefault_permissions=read-only', '--profile restricted',
    '-c permissions.custom.allow=true', '--config=sandbox_workspace_write.network_access=false',
    '-cnetwork.enabled=false', '-c ' + shlex.quote('approval_policy = "never"'),
])
def test_explicit_codex_permission_policy_suppresses_entire_workflow_preset(zsh, explicit):
    repo = shell_permissions(zsh, 'codex', 'auto')
    result = zsh(f'T_WORKFLOW_CWD={shlex.quote(str(repo))} _dev_agent_resume_cmd codex {SID} {explicit}')
    assert result.returncode == 0, result.stderr
    args = shlex.split(result.stdout)
    assert 'approvals_reviewer="auto_review"' not in args
    assert 'workspace-write' not in args and 'on-request' not in args
    assert all(arg in args for arg in shlex.split(explicit))
    assert '--model' in args and 'repo-model' in args
    assert 'model_reasoning_effort="high"' in args


@pytest.mark.parametrize('explicit', ['--permission-mode plan', '--permission-mode=plan',
    '--dangerously-skip-permissions', '--allow-dangerously-skip-permissions',
    '--settings \'' + '{"permissions":{"defaultMode":"manual"}}' + '\''])
def test_explicit_claude_permission_policy_suppresses_workflow_preset(zsh, explicit):
    repo = shell_permissions(zsh, 'claude', 'acceptEdits')
    result = zsh(f'T_WORKFLOW_CWD={shlex.quote(str(repo))} _dev_agent_resume_cmd claude {SID} {explicit}')
    assert result.returncode == 0, result.stderr
    args = shlex.split(result.stdout)
    assert 'acceptEdits' not in args
    assert all(arg in args for arg in shlex.split(explicit))
    assert '--model' in args and '--effort' in args


def test_explicit_effort_does_not_drop_codex_permission_reviewer(zsh):
    repo = shell_permissions(zsh, 'codex', 'auto')
    result = zsh(f'T_WORKFLOW_CWD={shlex.quote(str(repo))} _dev_agent_resume_cmd codex {SID} '
                 '-c model_reasoning_effort=low')
    args = shlex.split(result.stdout)
    assert result.returncode == 0, result.stderr
    assert 'approvals_reviewer="auto_review"' in args
    assert 'model_reasoning_effort=low' in args
    assert 'model_reasoning_effort="high"' not in args


def test_prompt_after_separator_cannot_suppress_workflow_permission_defaults(zsh):
    repo = shell_permissions(zsh, 'codex', 'auto')
    result = zsh(f'T_WORKFLOW_CWD={shlex.quote(str(repo))} _dev_agent_resume_cmd codex {SID} '
                 '-- --sandbox --yolo -c approval_policy=never')
    args = shlex.split(result.stdout)
    assert result.returncode == 0, result.stderr
    assert 'approvals_reviewer="auto_review"' in args
    assert args.index('workspace-write') < args.index('--')
    assert args.index('--no-daemon') < args.index('--')
    assert args[args.index('--') + 1:] == [
        '--sandbox', '--yolo', '-c', 'approval_policy=never']
