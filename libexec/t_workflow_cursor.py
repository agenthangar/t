"""Prepare scoped workflow instructions only when t launches a Cursor client."""

import copy
import os
from pathlib import Path
import runpy
import subprocess
import sys

RULE = Path('.cursor/rules/t-agent-workflow.mdc')
EXCLUDE = '/' + RULE.as_posix()
FRONTMATTER = '---\ndescription: t managed Cursor workflow\nalwaysApply: true\n---\n\n'


def workflow():
    return runpy.run_path(str(Path(__file__).with_name('t_workflow.py')))


def synchronization():
    return runpy.run_path(str(Path(__file__).with_name('t_workflow_sync.py')))


def git(cwd, *args, allow_missing=False):
    result = subprocess.run(['git', '-C', str(cwd), *args], capture_output=True,
                            text=True, timeout=5,
                            env={key: value for key, value in os.environ.items()
                                 if not key.startswith('COV_CORE_')})
    if result.returncode and not (allow_missing and result.returncode == 1):
        raise ValueError('Cursor workflow: cannot verify Git workspace: ' + result.stderr.strip())
    return result


def safe_parents(path, root):
    """Do not follow a rule or Git-info directory supplied through a symlink."""
    current = root
    for part in path.relative_to(root).parts[:-1]:
        current /= part
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise ValueError(f'Cursor workflow: unsafe directory {current}')


def explicit_model(argv):
    for value in argv:
        if value == '--':
            break
        if value == '--model' or value.startswith('--model='):
            return True
    return False


def restore(w, path, before, published):
    if w['snapshot'](path) != published:
        raise ValueError(f'Cursor workflow: {path} changed during preparation; inspect it before retrying')
    if before is None:
        path.unlink()
    else:
        w['publish']([(path, published, before[0])])


def prepare(cwd, argv=()):
    """Return scoped model arguments and publish a private, always-active rule.

    Existing Cursor owners never call this merely for attachment. There are no
    changes for recommended but unsaved defaults, and no native account writes.
    """
    w = workflow()
    document, saved = w['load']()
    if saved is None:
        return []
    configured = 'cursor' in document.get('tools', {})
    try:
        workspace = Path(git(cwd, 'rev-parse', '--show-toplevel').stdout.strip()).resolve()
    except (ValueError, OSError, subprocess.TimeoutExpired):
        if not configured:
            return []
        sync = synchronization()
        selected = w['tool_document'](document, 'cursor')
        values = w['main_session_settings'](w['effective'](selected), 'cursor')
        print('Cursor workflow: instructions not installed outside a Git workspace; continuing with configured global model defaults.', file=sys.stderr)
        return [] if explicit_model(argv) else sync['setting_args'](values, 'cursor')
    if not workspace.is_dir():
        raise ValueError('Cursor workflow: destination workspace is unavailable')
    rule = workspace / RULE
    if not configured:
        # An unrelated saved Codex/Claude catalogue must not change Cursor.
        # Remove only our old block if the Cursor catalogue was explicitly removed.
        try:
            safe_parents(rule, workspace)
            before = w['snapshot'](rule)
        except (ValueError, OSError):
            return []
        if before is None or not before[0].startswith(FRONTMATTER) or w['BEGIN'] not in before[0]:
            return []
        if git(workspace, 'ls-files', '--error-unmatch', '--', RULE.as_posix(), allow_missing=True).returncode == 0:
            raise ValueError(f'Cursor workflow: refusing tracked managed rule {rule}')
        remainder = w['merge'](before[0][len(FRONTMATTER):], '')
        if remainder.strip():
            w['publish']([(rule, before, FRONTMATTER + remainder)])
        else:
            restore(w, rule, None, before)
        return []
    selected = w['tool_document'](document, 'cursor')
    sync = synchronization()
    policy = sync['launch_policy'](w, selected, str(cwd))
    if policy is None:
        raise ValueError('Cursor workflow: cannot resolve the destination repository scope')
    safe_parents(rule, workspace)
    if git(workspace, 'ls-files', '--error-unmatch', '--', RULE.as_posix(), allow_missing=True).returncode == 0:
        raise ValueError(f'Cursor workflow: refusing tracked managed rule {rule}')
    before = w['snapshot'](rule)
    if before is not None and (not before[0].startswith(FRONTMATTER) or
                               before[0].count(w['BEGIN']) != 1 or before[0].count(w['END']) != 1):
        raise ValueError(f'Cursor workflow: refusing an unmanaged rule at {rule}')
    scoped = copy.deepcopy(selected)
    scoped.update(default='selected', workflows={'selected': policy}, repositories={})
    rendered = w['render'](scoped, tool='cursor')
    contents = FRONTMATTER + w['merge'](before[0][len(FRONTMATTER):] if before else '', rendered)
    common = Path(git(workspace, 'rev-parse', '--path-format=absolute', '--git-common-dir').stdout.strip()).resolve()
    exclude = Path(git(workspace, 'rev-parse', '--path-format=absolute', '--git-path', 'info/exclude').stdout.strip())
    if not exclude.is_absolute() or not exclude.is_relative_to(common):
        raise ValueError('Cursor workflow: unsafe Git exclude destination')
    safe_parents(exclude, common)
    excluded_before = w['snapshot'](exclude)
    text = excluded_before[0] if excluded_before else ''
    excluded_text = text if EXCLUDE in text.splitlines() else text + ('' if not text or text.endswith('\n') else '\n') + EXCLUDE + '\n'
    w['publish']([(exclude, excluded_before, excluded_text)])
    excluded_after = w['snapshot'](exclude)
    try:
        if git(workspace, 'check-ignore', '--no-index', '--quiet', '--', RULE.as_posix(), allow_missing=True).returncode:
            raise ValueError('Cursor workflow: repository ignore rules expose the private managed rule; no instructions were written')
        safe_parents(rule, workspace)
        w['publish']([(rule, before, contents)])
    except BaseException:
        if excluded_after != excluded_before:
            restore(w, exclude, excluded_before, excluded_after)
        raise
    return [] if explicit_model(argv) else sync['launch_settings']('cursor', str(cwd))


if __name__ == '__main__':
    try:
        if len(sys.argv) < 2:
            raise ValueError('Cursor workflow: destination workspace required')
        print('\n'.join(prepare(sys.argv[1], sys.argv[2:])), end='')
    except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
