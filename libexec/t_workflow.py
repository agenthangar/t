"""Account-local staged agent preferences and managed native instructions.

These instructions guide agents; t does not switch models or enforce execution.
"""
import copy
import fcntl
import json
import os
from pathlib import Path
import re
import shlex
import stat
import subprocess
import sys
import tempfile

BEGIN = '<!-- t agent-workflow: begin -->'
END = '<!-- t agent-workflow: end -->'
EFFORTS = ('minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra')
DEFAULT = {
    'enabled': True,
    'planning': {'model': 'gpt-6-astra', 'effort': 'auto'},
    'implementation': {'model': 'gpt-6.1-sol', 'effort': 'auto'},
    'review': {'model': 'gpt-6-astra', 'effort': 'one-level-lower'},
}


def config_path():
    return Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config').expanduser() / 't/workflow.json'


def defaults():
    return {'version': 1, 'global': copy.deepcopy(DEFAULT), 'repositories': {}}


def validate_policy(value):
    if not isinstance(value, dict) or set(value) != set(DEFAULT):
        raise ValueError('workflow requires enabled, planning, implementation and review')
    if type(value['enabled']) is not bool:
        raise ValueError('workflow enabled must be true or false')
    for stage in ('planning', 'implementation', 'review'):
        setting = value[stage]
        if not isinstance(setting, dict) or set(setting) != {'model', 'effort'}:
            raise ValueError(f'{stage} requires model and effort')
        model = setting['model']
        if not isinstance(model, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:/\[\]-]*', model):
            raise ValueError(f'{stage} model must be a nonempty model ID without spaces or control characters')
        allowed = EFFORTS + (('one-level-lower',) if stage == 'review' else ('auto',))
        if setting['effort'] not in allowed:
            raise ValueError(f'{stage} effort must be one of: {", ".join(allowed)}')
    return value


def validate(value):
    if not isinstance(value, dict) or set(value) != {'version', 'global', 'repositories'}:
        raise ValueError('workflow document requires version, global and repositories')
    if type(value['version']) is not int or value['version'] != 1:
        raise ValueError('unsupported workflow version; expected 1')
    validate_policy(value['global'])
    if not isinstance(value['repositories'], dict):
        raise ValueError('workflow repositories must be an object')
    for path, policy in value['repositories'].items():
        if not isinstance(path, str) or not os.path.isabs(path) or os.path.realpath(path) != path or any(ord(c) < 32 or ord(c) == 127 for c in path):
            raise ValueError('repository workflow keys must be canonical absolute checkout paths')
        validate_policy(policy)
    return value


def decode(text):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f'duplicate workflow key: {key}')
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=unique)


def encode(value):
    return json.dumps(value, indent=2, ensure_ascii=False) + '\n'


def snapshot(path):
    """Read a regular file without following a final symlink; retain its identity."""
    path = Path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f'{path}: refusing a symlink or nonregular file')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, encoding='utf-8') as stream:
        opened = os.fstat(stream.fileno())
        text = stream.read()
    identity = lambda entry: (entry.st_dev, entry.st_ino, entry.st_mode, entry.st_size, entry.st_mtime_ns, entry.st_ctime_ns)
    if identity(info) != identity(opened) or identity(path.lstat()) != identity(info):
        raise ValueError(f'{path}: changed while reading; retry')
    return (text, identity(info))


def load():
    before = snapshot(config_path())
    return (validate(decode(before[0])) if before else defaults()), before


def effective(document, repository=None):
    return copy.deepcopy(document['repositories'].get(repository, document['global']))


def scoped(document, repository=None):
    """A repository's null value is an explicit request to inherit the global policy."""
    return copy.deepcopy(document['repositories'].get(repository)) if repository else copy.deepcopy(document['global'])


def update(document, value, repository=None):
    after = copy.deepcopy(document)
    if repository and value is None:
        after['repositories'].pop(repository, None)
    else:
        validate_policy(value)
        if repository:
            after['repositories'][repository] = value
        else:
            after['global'] = value
    return validate(after)


def edit(document, repository=None):
    """Edit only a scope, preserving editor status and never publishing on failure."""
    argv = shlex.split(os.environ.get('VISUAL') or os.environ.get('EDITOR') or 'vi')
    if not argv:
        raise ValueError('set VISUAL or EDITOR to an editor command')
    initial = scoped(document, repository)
    # New overrides start with the effective values so they are easy to customize.
    shown = effective(document, repository) if initial is None else initial
    with tempfile.TemporaryDirectory(prefix='t-workflow-') as directory:
        path = Path(directory) / 'workflow.json'
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(encode(shown))
        rc = subprocess.call([*argv, str(path)])
        if rc:
            return rc, document
        edited = snapshot(path)
        if edited is None:
            raise ValueError('editor removed workflow file; nothing saved')
        value = decode(edited[0])
        if value == shown:
            return 0, document
        return 0, update(document, value, repository)


def _stage_text(policy):
    if not policy['enabled']:
        return ('The staged model workflow is disabled for this scope. Do not require '
                'these stages or delegation solely because of t preferences; follow the task and repository rules.')
    plan, implement, review = (policy[s] for s in ('planning', 'implementation', 'review'))
    planning = ('medium for straightforward additions, high for substantial features, '
                'and xhigh or higher for complex architecture or new applications') if plan['effort'] == 'auto' else plan['effort']
    implementation = ('low for straightforward execution, medium for ordinary features, '
                      'and high or higher when implementation complexity warrants it') if implement['effort'] == 'auto' else implement['effort']
    reviewing = ('one supported reasoning-effort level below the planning stage, using the '
                 'lowest supported level if planning is already at the minimum') if review['effort'] == 'one-level-lower' else review['effort']
    return (f'For new features, updated designs and new buildouts, use this staged workflow. '
            f'Routine questions and small maintenance edits do not require all three stages.\n'
            f'1. Plan with {plan["model"]}; reasoning effort: {planning}. Produce an actionable '
            f'plan, acceptance criteria and validation before implementation.\n'
            f'2. Implement with {implement["model"]}; reasoning effort: {implementation}. Give '
            f'the implementation agent the plan, relevant repository instructions and acceptance '
            f'criteria; implement and run appropriate checks.\n'
            f'3. Review and finish with {review["model"]}; reasoning effort: {reviewing}. '
            f'Review the actual diff and validation against the plan, resolve findings, and recheck '
            f'fixes. The reviewer owns PR creation or updates, failing tests, merge conflicts and '
            f'required CI through completion under the repository merge policy and existing authorization.\n'
            f'This workflow authorizes sub-agent delegation with explicit model and reasoning settings. '
            f'Preserve the plan, acceptance criteria and implementation evidence in each handoff. '
            f'If a model or model switching is unavailable, disclose that limitation and the actual '
            f'model used; never claim a stage ran with a model it did not.')


def render(document):
    validate(document)
    text = ('Agent workflow preferences supplied by t. These managed defaults supersede older '
            'general staged-model defaults in personal instructions. Explicit task instructions '
            'and repository-specific rules remain authoritative. These are instructions for the '
            'agent, not runtime model switching or enforcement by t.\n\n'
            'Global default (unless a repository scope below matches):\n' + _stage_text(document['global']))
    for repository, policy in sorted(document['repositories'].items()):
        text += ('\n\nRepository scope: ' + json.dumps(repository, ensure_ascii=False) + '.\n'
                 'Apply this complete override in that canonical checkout and all its linked '
                 'worktrees, including worktrees created later. Resolve the current checkout with '
                 '`git rev-parse --show-toplevel` and its canonical repository with '
                 '`git rev-parse --path-format=absolute --git-common-dir`; the common Git directory '
                 'must belong to the canonical checkout above. Other repositories use the global default.\n' + _stage_text(policy))
    return BEGIN + '\n' + text + '\n' + END


def merge(before, block):
    starts, ends = before.count(BEGIN), before.count(END)
    if (starts, ends) not in ((0, 0), (1, 1)) or (starts and before.index(END) < before.index(BEGIN)):
        raise ValueError('malformed t agent-workflow markers; repair them before saving')
    if starts:
        return before[:before.index(BEGIN)] + block + before[before.index(END) + len(END):]
    return before + ('\n\n' if before else '') + block + '\n'


def native_changes(document):
    home = Path.home()
    codex = Path(os.environ.get('CODEX_HOME') or home / '.codex').expanduser()
    claude = Path(os.environ.get('CLAUDE_CONFIG_DIR') or home / '.claude').expanduser()
    paths = [codex / 'AGENTS.md', claude / 'CLAUDE.md']
    override = codex / 'AGENTS.override.md'
    if override.exists() or override.is_symlink():
        paths.append(override)
    changes = []
    for path in paths:
        before = snapshot(path)
        if path == override and before and not before[0].strip():
            continue  # An empty override must not begin masking the base file.
        changes.append((path, before, merge(before[0] if before else '', render(document))))
    return changes


def publish(changes):
    """Preflight every destination, then publish; restore prior files on a failure.

    Atomic replaces protect each file. Identity checks avoid overwriting edits made
    by other processes; rollback never overwrites a subsequent external change.
    """
    changes = [(Path(p), b, a) for p, b, a in changes if b is None or b[0] != a]
    prepared, applied = [], []
    try:
        for path, before, after in changes:
            if snapshot(path) != before:
                raise ValueError(f'{path}: changed while updating; retry')
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                             prefix='.t-workflow-', delete=False) as stream:
                pending = Path(stream.name)
                prepared.append((path, before, pending))
                stream.write(after)
                stream.flush()
                os.fchmod(stream.fileno(), stat.S_IMODE(before[1][2]) if before else 0o600)
        for path, before, pending in prepared:
            if snapshot(path) != before:
                raise ValueError(f'{path}: changed while updating; retry')
        for path, before, pending in prepared:
            if snapshot(path) != before:
                raise ValueError(f'{path}: changed while updating; retry')
            os.replace(pending, path)
            applied.append((path, before, snapshot(path)))
    except BaseException:
        for path, before, published in reversed(applied):
            if snapshot(path) != published:
                raise ValueError(f'{path}: changed during rollback; inspect workflow and native instructions')
            if before is None:
                path.unlink()
            else:
                with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                                 prefix='.t-workflow-', delete=False) as stream:
                    rollback = Path(stream.name)
                    try:
                        stream.write(before[0])
                        os.fchmod(stream.fileno(), stat.S_IMODE(before[1][2]))
                        stream.flush()
                        os.replace(rollback, path)
                    finally:
                        rollback.unlink(missing_ok=True)
        raise
    finally:
        for _, _, pending in prepared:
            pending.unlink(missing_ok=True)


def save(document, before, extra=()):
    """Publish explicit preferences and native instruction updates under one lock."""
    validate(document)
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_suffix('.lock')
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    with os.fdopen(fd, 'r+') as stream:
        fcntl.flock(stream, fcntl.LOCK_EX)
        if snapshot(path) != before:
            raise ValueError('workflow configuration changed while editing; retry')
        changes = native_changes(document)
        publish([*changes, *extra, (path, before, encode(document))])


def sync():
    """Installer refreshes only accounts with explicitly saved preferences."""
    document, before = load()
    if before is not None:
        save(document, before)
    return 0


if __name__ == '__main__':
    try:
        sys.exit(sync())
    except (OSError, ValueError, UnicodeError) as error:
        print(f't install: agent workflow: {error}', file=sys.stderr)
        sys.exit(1)
