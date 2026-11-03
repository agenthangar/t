"""Cursor workflows prepare private rules only at guarded launch boundaries."""
import copy
import os
from pathlib import Path
import shlex
import subprocess

import pytest

from conftest import load_script, REPO_ROOT
from test_workflow import workflow  # noqa: F401 -- isolated HOME/XDG/native paths
from test_recovery import recovery, target, result, SID  # noqa: F401 -- isolated recovery


def git(cwd, *args):
    env = {key: value for key, value in os.environ.items() if not key.startswith('COV_CORE_')}
    return subprocess.run(['git', '-C', str(cwd), *args], text=True, capture_output=True,
                          env=env, check=True)


@pytest.fixture
def cursor(workflow, monkeypatch):
    module = load_script(REPO_ROOT / 'libexec/t_workflow_cursor.py', 'workflow_cursor_test')
    monkeypatch.setattr(module, 'workflow', lambda: vars(workflow))
    return module


@pytest.fixture
def repo(tmp_path):
    path = (tmp_path / 'workspace').resolve()
    path.mkdir()
    git(path, 'init', '-q')
    return path


def save_cursor(workflow, repo, *, excluded=False, enabled=True):
    document = workflow.defaults()
    document['workflows']['standard']['instructions'] = 'CODEX ONLY'
    selected = workflow.tool_document(document, 'cursor')
    policy = selected['workflows']['standard']
    policy['instructions'] = 'CURSOR ONLY'
    policy['enabled'] = enabled
    policy['session_defaults'] = {'cursor': {'model': 'sonnet[effort=high]'}}
    selected['repositories'][str(repo)] = {'inline': copy.deepcopy(policy), **({'excluded': True} if excluded else {})}
    document = workflow.with_tool_document(document, 'cursor', selected)
    path = workflow.config_path()
    workflow.publish([(path, workflow.snapshot(path), workflow.encode(document))])
    return document


def test_unsaved_defaults_do_not_create_rules_or_model_arguments(cursor, repo):
    assert cursor.prepare(repo) == []
    assert not (repo / '.cursor').exists()
    assert cursor.EXCLUDE not in (repo / '.git/info/exclude').read_text()


def test_prepare_selects_cursor_model_and_private_instructions(cursor, workflow, repo):
    save_cursor(workflow, repo)
    raw = workflow.config_path().read_bytes()
    excluded = repo / '.git/info/exclude'
    previous = excluded.read_text()
    assert cursor.prepare(repo) == ['--model', 'sonnet[effort=high]']
    rule = repo / cursor.RULE
    text = rule.read_text()
    assert text.startswith(cursor.FRONTMATTER)
    assert text.count(workflow.BEGIN) == text.count(workflow.END) == 1
    assert 'CURSOR ONLY' in text and 'CODEX ONLY' not in text
    assert 'alwaysApply: true' in text
    assert rule.stat().st_mode & 0o777 == 0o600
    assert excluded.read_text().startswith(previous)
    assert cursor.EXCLUDE in excluded.read_text().splitlines()
    assert not git(repo, 'status', '--porcelain', '--', cursor.RULE.as_posix()).stdout
    assert workflow.config_path().read_bytes() == raw
    before = workflow.snapshot(rule)
    assert cursor.prepare(repo) == ['--model', 'sonnet[effort=high]']
    assert workflow.snapshot(rule) == before


def test_prepare_uses_actual_worktree_and_canonical_scope(cursor, workflow, repo, tmp_path):
    git(repo, '-c', 'user.email=test@example.invalid', '-c', 'user.name=Test', 'commit', '--allow-empty', '-qm', 'seed')
    linked = (tmp_path / 'linked').resolve()
    git(repo, 'worktree', 'add', '--detach', str(linked), 'HEAD')
    nested = linked / 'src'
    nested.mkdir()
    save_cursor(workflow, repo)
    assert cursor.prepare(nested) == ['--model', 'sonnet[effort=high]']
    assert (linked / cursor.RULE).is_file()
    assert not (repo / cursor.RULE).exists()
    assert cursor.EXCLUDE in (repo / '.git/info/exclude').read_text()


@pytest.mark.parametrize('condition', ['excluded', 'disabled'])
def test_inactive_cursor_scope_replaces_stale_rule_without_model_override(cursor, workflow, repo, condition):
    save_cursor(workflow, repo)
    cursor.prepare(repo)
    save_cursor(workflow, repo, excluded=condition == 'excluded', enabled=condition != 'disabled')
    assert cursor.prepare(repo) == []
    assert 'entire workflow is disabled for this scope' in (repo / cursor.RULE).read_text()


@pytest.mark.parametrize('override', [['--model', 'explicit'], ['--model=explicit']])
def test_explicit_cursor_model_wins_but_scoped_instructions_still_prepare(cursor, workflow, repo, override):
    save_cursor(workflow, repo)
    assert cursor.prepare(repo, override) == []
    assert 'CURSOR ONLY' in (repo / cursor.RULE).read_text()
    assert cursor.explicit_model(['--', '--model=prompt-text']) is False


@pytest.mark.parametrize('unsafe', ['tracked', 'unmanaged', 'rule_symlink', 'directory_symlink', 'exclude_symlink'])
def test_prepare_refuses_tracked_unmanaged_and_symlink_destinations(cursor, workflow, repo, tmp_path, unsafe):
    save_cursor(workflow, repo)
    rule = repo / cursor.RULE
    rule.parent.mkdir(parents=True)
    outside = tmp_path / 'outside'
    outside.mkdir()
    if unsafe in ('tracked', 'unmanaged'):
        rule.write_text('USER RULE')
        if unsafe == 'tracked':
            git(repo, 'add', cursor.RULE.as_posix())
    elif unsafe == 'rule_symlink':
        target = outside / 'rule'
        target.write_text('PRIVATE USER RULE')
        rule.symlink_to(target)
    elif unsafe == 'directory_symlink':
        rule.parent.rmdir()
        rule.parent.symlink_to(outside, target_is_directory=True)
    else:
        exclude = repo / '.git/info/exclude'
        exclude.unlink()
        exclude.symlink_to(outside / 'exclude')
    with pytest.raises((ValueError, OSError)):
        cursor.prepare(repo)
    if unsafe in ('tracked', 'unmanaged'):
        assert rule.read_text() == 'USER RULE'
    elif unsafe == 'rule_symlink':
        assert target.read_text() == 'PRIVATE USER RULE'
    assert list(outside.iterdir()) == ([target] if unsafe == 'rule_symlink' else [])


def test_repository_negation_cannot_expose_private_rule(cursor, workflow, repo):
    save_cursor(workflow, repo)
    (repo / '.gitignore').write_text('!.cursor/rules/t-agent-workflow.mdc\n')
    exclude = repo / '.git/info/exclude'
    before = exclude.read_bytes()
    with pytest.raises(ValueError, match='expose the private managed rule'):
        cursor.prepare(repo)
    assert not (repo / cursor.RULE).exists()
    assert exclude.read_bytes() == before


def test_prepare_preserves_other_rules_and_managed_file_notes(cursor, workflow, repo):
    save_cursor(workflow, repo)
    rules = repo / '.cursor/rules'
    rules.mkdir(parents=True)
    other = rules / 'user.mdc'
    other.write_text('USER OWNED RULE')
    cursor.prepare(repo)
    managed = repo / cursor.RULE
    managed.write_text(managed.read_text() + '\nUser note outside managed markers.\n')
    cursor.prepare(repo)
    assert 'User note outside managed markers.' in managed.read_text()
    assert other.read_text() == 'USER OWNED RULE'


def test_rule_publish_failure_restores_exclusion(cursor, workflow, repo, monkeypatch):
    save_cursor(workflow, repo)
    exclude = repo / '.git/info/exclude'
    before = exclude.read_bytes()
    publish = workflow.publish
    def fail_rule(changes):
        changes = list(changes)
        if any(path == repo / cursor.RULE for path, _, _ in changes):
            raise OSError('publication failed')
        publish(changes)
    monkeypatch.setattr(workflow, 'publish', fail_rule)
    with pytest.raises(OSError, match='publication failed'):
        cursor.prepare(repo)
    assert exclude.read_bytes() == before
    assert not (repo / cursor.RULE).exists()


@pytest.mark.parametrize('owner', ['existing', 'unknown'])
def test_recovery_attach_or_unverified_owner_never_prepares_instructions(recovery, target, tmp_path, monkeypatch, owner):
    calls = []
    monkeypatch.setattr(recovery, 'cursor_launch', lambda *a: calls.append(a) or 'unexpected')
    monkeypatch.setattr(recovery.shutil, 'which', lambda n: '/fake/cursor-agent')
    monkeypatch.setattr(recovery, 'install_cursor_hook', lambda: None)
    def command(argv):
        if 'has-session' in argv:
            return result(rc=0 if owner == 'existing' else 1)
        if 'display-message' in argv:
            return result(target['socket'])
        if argv[0] == 'ps':
            return result('42 cursor-agent --resume=unverified')
        return result()
    monkeypatch.setattr(recovery, 'run', command)
    monkeypatch.setattr(recovery, 'panes', lambda s: [dict(target, session='t-cursor-' + SID)] if owner == 'existing' else [])
    monkeypatch.setattr(recovery, 'owner', lambda *a: dict(target, session='t-cursor-' + SID, cwd=str(tmp_path)))
    monkeypatch.setattr(recovery, 'start', lambda s: 0)
    monkeypatch.setattr(recovery.os, 'execvp', lambda *a: (_ for _ in ()).throw(SystemExit(0)))
    with pytest.raises(SystemExit if owner == 'existing' else ValueError):
        recovery.cursor_resume(SID, str(tmp_path))
    assert calls == []


def test_restart_workflow_failure_or_owner_change_does_not_stop_client(recovery, target, monkeypatch):
    monkeypatch.setattr(recovery, 'current', lambda t: True)
    monkeypatch.setattr(recovery, 'descendant', lambda *a: False)
    monkeypatch.setattr(recovery, 'tmux', lambda *a: result('draft'))
    stopped = []
    monkeypatch.setattr(recovery.os, 'kill', lambda *a: stopped.append(a))
    monkeypatch.setattr(recovery, 'cursor_launch', lambda *a: (_ for _ in ()).throw(ValueError('unsafe rule')))
    with pytest.raises(ValueError, match='unsafe rule'):
        recovery._cursor_restart(target)
    assert stopped == []
    def changed(*args):
        monkeypatch.setattr(recovery, 'current', lambda t: False)
        return shlex.join(['cursor-agent', '--model', 'selected'])
    monkeypatch.setattr(recovery, 'cursor_launch', changed)
    with pytest.raises(ValueError, match='owner changed while preparing'):
        recovery._cursor_restart(target)
    assert stopped == []


def test_saved_codex_only_settings_leave_cursor_untouched(cursor, workflow, repo, tmp_path):
    document = workflow.defaults()
    path = workflow.config_path()
    workflow.publish([(path, None, workflow.encode(document))])
    raw = path.read_bytes()
    exclude = (repo / '.git/info/exclude').read_bytes()
    assert cursor.prepare(repo) == []
    assert cursor.prepare(tmp_path) == []
    assert not (repo / '.cursor').exists()
    assert (repo / '.git/info/exclude').read_bytes() == exclude
    assert path.read_bytes() == raw


def test_configured_non_git_cursor_continues_with_model_and_reports_instruction_limit(cursor, workflow, repo, tmp_path, capsys):
    save_cursor(workflow, repo)
    raw = workflow.config_path().read_bytes()
    outside = tmp_path / 'not-a-repository'
    outside.mkdir()
    assert cursor.prepare(outside) == ['--model', 'sonnet[effort=high]']
    assert 'instructions not installed outside a Git workspace' in capsys.readouterr().err
    assert not (outside / '.cursor').exists()
    assert workflow.config_path().read_bytes() == raw
    assert cursor.prepare(outside, ['--model=explicit']) == []


def test_workspace_rule_contains_only_its_resolved_cursor_policy(cursor, workflow, repo, tmp_path):
    document = save_cursor(workflow, repo)
    selected = workflow.tool_document(document, 'cursor')
    unrelated = (tmp_path / 'unrelated-private-project').resolve()
    selected['repositories'][str(unrelated)] = {'inline': {
        **copy.deepcopy(selected['workflows']['standard']), 'instructions': 'UNRELATED PRIVATE POLICY'}}
    document = workflow.with_tool_document(document, 'cursor', selected)
    workflow.publish([(workflow.config_path(), workflow.snapshot(workflow.config_path()), workflow.encode(document))])
    cursor.prepare(repo)
    text = (repo / cursor.RULE).read_text()
    assert 'CURSOR ONLY' in text
    assert 'UNRELATED PRIVATE POLICY' not in text
    assert str(unrelated) not in text


def test_removing_cursor_catalogue_cleans_managed_rule_but_preserves_user_notes(cursor, workflow, repo):
    save_cursor(workflow, repo)
    cursor.prepare(repo)
    document, _ = workflow.load()
    document['tools'].pop('cursor')
    workflow.publish([(workflow.config_path(), workflow.snapshot(workflow.config_path()), workflow.encode(document))])
    managed = repo / cursor.RULE
    managed.write_text(managed.read_text() + '\nUSER NOTE\n')
    assert cursor.prepare(repo) == []
    assert 'USER NOTE' in managed.read_text()
    assert workflow.BEGIN not in managed.read_text()
    assert 'CURSOR ONLY' not in managed.read_text()


def test_removing_cursor_catalogue_removes_empty_managed_file(cursor, workflow, repo):
    save_cursor(workflow, repo)
    cursor.prepare(repo)
    document, _ = workflow.load()
    document['tools'].pop('cursor')
    workflow.publish([(workflow.config_path(), workflow.snapshot(workflow.config_path()), workflow.encode(document))])
    assert cursor.prepare(repo) == []
    assert not (repo / cursor.RULE).exists()
